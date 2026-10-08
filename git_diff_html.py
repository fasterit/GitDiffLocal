#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Copyright (C) 2026 上海先道智觉科技有限责任公司 (Symthosim)
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program. If not, see <https://www.gnu.org/licenses/>.
# ---------------------------------------------------------------------------
"""
git-diff-html — Generate a Gerrit/BeyondCompare-style side-by-side HTML diff report.
Uses flex dual-pane layout with draggable splitter and synchronized horizontal scroll.

Usage:
    gdhtm [ref] [file ...]     or     gdhtm -r REF1 REF2 [file ...]

    Examples:
        gdhtm                 working tree vs HEAD (opens browser)
        gdhtm abc123          working tree vs commit abc123
        gdhtm abc123 file.c   working tree vs abc123, only file.c
        gdhtm -r aaa bbb      commit aaa vs commit bbb (explicit two-commit compare)
        gdhtm -s              staged changes (git diff --cached)
        gdhtm old.c new.c     outside a git repo: plain old-vs-new file compare
                              (exactly two file arguments; works outside a git repo, too)

Options:
    -o, --output FILE   Output HTML file (default: a temp file in the system temp dir, e.g. /tmp)
    -c, --commit REF    Compare against a specific commit/ref (default: HEAD)
    -s, --staged        Show staged changes (git diff --cached)
    --no-open           Do not auto-open the report (opening the browser is the default)
    --no-syntax         Disable basic C/C++ syntax highlighting on context lines
    -C, --context N     Number of context lines (default: 3)
"""

import argparse
import datetime
import html
import os
import re
import subprocess
import sys
import tempfile
from difflib import SequenceMatcher


# ---------------------------------------------------------------------------
# C/C++ syntax highlighting (lightweight, no external deps)
# ---------------------------------------------------------------------------

_CPP_KEYWORDS = {
    "alignas", "alignof", "asm", "auto", "bool", "break", "case", "catch",
    "char", "class", "const", "constexpr", "const_cast", "continue",
    "decltype", "default", "delete", "do", "double", "dynamic_cast", "else",
    "enum", "explicit", "export", "extern", "false", "float", "for", "friend",
    "goto", "if", "inline", "int", "long", "mutable", "namespace", "new",
    "noexcept", "nullptr", "operator", "private", "protected", "public",
    "register", "reinterpret_cast", "return", "short", "signed", "sizeof",
    "static", "static_assert", "static_cast", "struct", "switch", "template",
    "this", "thread_local", "throw", "true", "try", "typedef", "typeid",
    "typename", "union", "unsigned", "using", "virtual", "void", "volatile",
    "wchar_t", "while", "override", "final",
}

_CPP_TYPES = {
    "size_t", "ssize_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    "int8_t", "int16_t", "int32_t", "int64_t", "intptr_t", "uintptr_t",
    "ptrdiff_t", "string", "vector", "map", "set", "list", "queue", "stack",
    "shared_ptr", "unique_ptr", "weak_ptr", "mutex", "lock_guard", "thread",
    "function", "pair", "tuple", "optional", "variant", "any",
}


def _highlight_cpp(line: str) -> str:
    if not line.strip():
        return "&nbsp;"
    # Protect strings / comments / preprocessor lines as placeholders FIRST,
    # so the keyword/type/number regexes below never run over already-inserted
    # <span> tags. Otherwise words like `class` inside `<span class="tok-...">`
    # get re-wrapped and corrupt the tag (visible as literal `class="tok-com">`).
    # Note: `line` arrives html.escaped, so string quotes are &quot;.
    held = []

    def hold(fragment: str) -> str:
        idx = len(held)
        held.append(fragment)
        return f"\x00S{idx}\x00"

    s = re.sub(
        r'(?<!\\)&quot;(.*?)(?<!\\)&quot;',
        lambda m: hold(f'<span class="tok-str">&quot;{m.group(1)}&quot;</span>'),
        line,
    )
    s = re.sub(
        r"(?<!\\)'(.*?)(?<!\\)'",
        lambda m: hold(f"<span class='tok-str'>'{m.group(1)}'</span>"),
        s,
    )
    if "//" in s:
        idx = s.find("//")
        s = s[:idx] + hold(f'<span class="tok-com">{s[idx:]}</span>')
    if s.lstrip().startswith("#"):
        s = hold(f'<span class="tok-pre">{s}</span>')
    else:
        def _rw(m):
            w = m.group(0)
            if w in _CPP_KEYWORDS:
                return f'<span class="tok-kw">{w}</span>'
            if w in _CPP_TYPES:
                return f'<span class="tok-type">{w}</span>'
            return w
        s = re.sub(r"\b[A-Za-z_]\w*\b", _rw, s)
        s = re.sub(
            r"\b(\d+\.?\d*(?:[eE][+-]?\d+)?[uUlL]*)\b",
            r'<span class="tok-num">\1</span>',
            s,
        )
    for i, fragment in enumerate(held):
        s = s.replace(f"\x00S{i}\x00", fragment)
    return s


def _is_code_file(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in {
        ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".hxx", ".inl",
    }


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def run_git(args, cwd):
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    return r.stdout, r.returncode


def get_changed_files(cwd, base, target, staged, files_filter):
    # target is None -> compare <base> against the working tree
    args = ["diff", "--name-status"]
    if staged:
        args.append("--cached")
    elif target:
        args.extend([base, target])
    else:
        args.append(base)
    if files_filter:
        args.append("--")
        args.extend(files_filter)
    stdout, rc = run_git(args, cwd)
    if rc != 0:
        print(f"Error: {stdout}", file=sys.stderr)
        sys.exit(1)
    files = []
    for line in stdout.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0][0]
        if status == "R":
            files.append((status, parts[1], parts[2]))
        else:
            path = parts[1] if len(parts) > 1 else parts[0][1:]
            files.append((status, path, path))
    return files


def get_file_content(cwd, base, path, staged, target=None):
    """Return (old, new) content of a file.

    staged:            old=HEAD:path, new=index(:path)
    target is None:    old=<base>:path, new=working-tree file
    target not None:   old=<base>:path, new=<target>:path  (commit vs commit)
    """
    old = ""
    ref = f"HEAD:{path}" if staged else f"{base}:{path}"
    stdout, rc = run_git(["show", ref], cwd)
    if rc == 0:
        old = stdout
    new = ""
    if staged:
        stdout, rc = run_git(["show", f":{path}"], cwd)
        if rc == 0:
            new = stdout
    elif target is not None:
        stdout, rc = run_git(["show", f"{target}:{path}"], cwd)
        if rc == 0:
            new = stdout
    else:
        full = os.path.join(cwd, path)
        if os.path.isfile(full):
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                new = f.read()
    return old, new


# ---------------------------------------------------------------------------
# Inline character-level diff (Gerrit-style word highlighting)
# ---------------------------------------------------------------------------

def inline_diff(old_line, new_line):
    sm = SequenceMatcher(None, old_line, new_line, autojunk=False)
    old_parts, new_parts = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        os_ = html.escape(old_line[i1:i2])
        ns = html.escape(new_line[j1:j2])
        if tag == "equal":
            old_parts.append(os_)
            new_parts.append(ns)
        elif tag == "delete":
            old_parts.append(f'<span class="inline-del">{os_}</span>')
        elif tag == "insert":
            new_parts.append(f'<span class="inline-add">{ns}</span>')
        elif tag == "replace":
            old_parts.append(f'<span class="inline-del">{os_}</span>')
            new_parts.append(f'<span class="inline-add">{ns}</span>')
    return "".join(old_parts) or "&nbsp;", "".join(new_parts) or "&nbsp;"


# ---------------------------------------------------------------------------
# Diff rendering — flex dual-pane layout
# ---------------------------------------------------------------------------

def _row_html(rtype, num, text, is_code, side, dtype=None):
    """Render a single row for one pane. side: 'old' or 'new'.
    dtype marks the row kind (ctx/del/add/rep) so the client-side "diff / full"
    toggle can decide which rows belong to a change without a server round-trip."""
    att = f' data-r="{dtype}"' if dtype else ""
    if rtype == "empty":
        return (
            f'<div class="drow empty"{att}><span class="lnum"></span>'
            f'<span class="code">&nbsp;</span></div>'
        )
    if rtype == "gap":
        return (
            f'<div class="drow gap"><span class="lnum">⋯</span>'
            f'<span class="code"></span></div>'
        )
    if rtype == "ctx":
        code = _highlight_cpp(html.escape(text)) if is_code else html.escape(text) or "&nbsp;"
    elif rtype in ("del", "add"):
        code = html.escape(text) or "&nbsp;"
    elif rtype == "rep":
        # text is already inline-diffed HTML
        code = text
    else:
        code = html.escape(text) or "&nbsp;"

    cls = rtype
    return (
        f'<div class="drow {cls}"{att}><span class="lnum">{num or ""}</span>'
        f'<span class="code">{code}</span></div>'
    )


def make_diff_viewer(old_lines, new_lines, filename, syntax=True, context=3, idx=0,
                     old_label="HEAD (old)", new_label="WORKING (new)"):
    """Generate a flex dual-pane diff viewer with draggable splitter."""
    sm = SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    opcodes = sm.get_opcodes()

    # Build unified rows: (rtype, onum, otext, nnum, ntext)
    rows = []
    on, nn = 1, 1
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            # new_lines must be indexed by j1 + (k - i1), not k:
            # old[i1:i2] == new[j1:j2], so i1 and j1 can differ after an insert/delete.
            for k in range(i1, i2):
                rows.append(("ctx", on, old_lines[k], nn, new_lines[j1 + (k - i1)]))
                on += 1; nn += 1
        elif tag == "delete":
            for k in range(i1, i2):
                rows.append(("del", on, old_lines[k], None, ""))
                on += 1
        elif tag == "insert":
            for k in range(j1, j2):
                rows.append(("add", None, "", nn, new_lines[k]))
                nn += 1
        elif tag == "replace":
            os_ = old_lines[i1:i2]
            ns = new_lines[j1:j2]
            mx = max(len(os_), len(ns))
            for xi in range(mx):
                ol = os_[xi] if xi < len(os_) else None
                nl = ns[xi] if xi < len(ns) else None
                onum = on if ol is not None else None
                nnum = nn if nl is not None else None
                rows.append(("rep", onum, ol or "", nnum, nl or ""))
                if ol is not None: on += 1
                if nl is not None: nn += 1

    is_code = syntax and _is_code_file(filename)

    # Render EVERY line of both files - no collapsing at generation time. The DOM
    # holds the full side-by-side so the browser can flip between "diff only" and
    # "full compare". Collapsing is done client-side by JS (it hides unchanged
    # lines that are far from any change and inserts a "gap" marker).
    # data-r marks the row kind (ctx/del/add/rep); both panes mirror it 1:1, and
    # the pane-body carries data-ctx so JS knows the context-line count.
    old_rows, new_rows = [], []
    for i, (rtype, onum, otext, nnum, ntext) in enumerate(rows):
        if rtype == "ctx":
            old_rows.append(_row_html("ctx", onum, otext, is_code, "old", dtype="ctx"))
            new_rows.append(_row_html("ctx", nnum, ntext, is_code, "new", dtype="ctx"))
        elif rtype == "del":
            old_rows.append(_row_html("del", onum, otext, is_code, "old", dtype="del"))
            new_rows.append(_row_html("empty", "", "", is_code, "new", dtype="del"))
        elif rtype == "add":
            old_rows.append(_row_html("empty", "", "", is_code, "old", dtype="add"))
            new_rows.append(_row_html("add", nnum, ntext, is_code, "new", dtype="add"))
        elif rtype == "rep":
            if otext and ntext:
                oi, ni = inline_diff(otext, ntext)
            elif otext:
                oi = f'<span class="inline-del">{html.escape(otext)}</span>'
                ni = "&nbsp;"
            elif ntext:
                oi = "&nbsp;"
                ni = f'<span class="inline-add">{html.escape(ntext)}</span>'
            else:
                oi = ni = "&nbsp;"
            old_rows.append(_row_html("rep", onum, oi, is_code, "old", dtype="rep"))
            new_rows.append(_row_html("rep", nnum, ni, is_code, "new", dtype="rep"))

    vid = f"diffv-{idx}"
    # Each pane owns a horizontal scrollbar strip (.pane-xbar) at the bottom of
    # its box. The JS keeps the two panes' scroll positions in sync so both
    # columns stay aligned.
    return f"""<div class="diffv" id="{vid}">
    <div class="pane old-pane"><div class="pane-head">{old_label}</div><div class="pane-body" data-ctx="{context}">{os.linesep.join(old_rows)}</div><div class="pane-xbar"><div class="pane-xbar-inner"></div></div></div>
    <div class="splitter" title="Drag to resize columns · double-click to reset"></div>
    <div class="pane new-pane"><div class="pane-head">{new_label}</div><div class="pane-body" data-ctx="{context}">{os.linesep.join(new_rows)}</div><div class="pane-xbar"><div class="pane-xbar-inner"></div></div></div>
</div>"""


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Git Diff Report — {title}</title>
<style>
  :root {{
    --bg: #fafafa; --fg: #24292f; --border: #d0d7de;
    --header-bg: #f6f8fa; --add-bg: #e6ffec; --add-intense: #abf2bc;
    --del-bg: #ffebe9; --del-intense: #ffc7c2; --link: #0969da;
    --sidebar-bg: #f6f8fa; --sidebar-hover: #eaeef2;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; overflow-x:clip; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; font-size:14px; color:var(--fg); background:var(--bg); }}

  /* Top bar */
  .topbar {{ position:sticky; top:0; z-index:100; background:#24292f; color:#fff; padding:10px 20px; display:flex; flex-wrap:wrap; align-items:center; gap:8px 16px; box-shadow:0 1px 3px rgba(0,0,0,.2); }}
  .topbar h1 {{ margin:0; font-size:16px; font-weight:600; }}
  .topbar .meta {{ font-size:12px; opacity:.75; }}
  .topbar .nav {{ margin-left:auto; display:flex; gap:8px; }}
  .topbar .nav button {{ background:#373e47; color:#fff; border:1px solid #444c56; padding:4px 12px; border-radius:6px; cursor:pointer; font-size:12px; }}
  .topbar .nav button:hover {{ background:#444c56; }}

  /* Layout */
  .layout {{ display:flex; min-height:calc(100vh - 48px); }}

  /* Sidebar */
  .sidebar {{ width:clamp(180px, 15vw, 340px); flex-shrink:0; background:var(--sidebar-bg); border-right:1px solid var(--border); padding:12px 0; overflow-y:auto; max-height:calc(100vh - 48px); position:sticky; top:48px; }}
  .sidebar h2 {{ font-size:12px; text-transform:uppercase; letter-spacing:.5px; color:#656d76; padding:0 16px 8px; margin:0; }}
  .sidebar ul {{ list-style:none; margin:0; padding:0; }}
  .sidebar li a {{ display:block; padding:6px 16px; color:var(--fg); text-decoration:none; font-family:ui-monospace,Menlo,monospace; font-size:12px; border-left:3px solid transparent; word-break:break-all; }}
  .sidebar li a:hover {{ background:var(--sidebar-hover); }}
  .sidebar li a.active {{ border-left-color:var(--link); background:#ddf4ff; }}
  .badge {{ display:inline-block; padding:1px 6px; border-radius:10px; font-size:10px; font-weight:600; margin-right:6px; }}
  .badge-M {{ background:#ddf4ff; color:#0969da; }}
  .badge-A {{ background:#dafbe1; color:#1a7f37; }}
  .badge-D {{ background:#ffebe9; color:#cf222e; }}
  .badge-R {{ background:#fff8c5; color:#9a6700; }}

  /* Sidebar resizer */
  .sb-resizer {{ width:6px; flex-shrink:0; cursor:col-resize; background:transparent; position:relative; }}
  .sb-resizer::after {{ content:''; position:absolute; top:0; bottom:0; left:50%; width:1px; background:var(--border); transform:translateX(-50%); }}
  .sb-resizer:hover::after, .sb-resizer.resizing::after {{ background:var(--link); width:2px; }}

  /* Main */
  .main {{ flex:1; padding:20px 24px; min-width:0; }}
  .file-section {{ margin-bottom:40px; }}
  .file-header {{ background:var(--header-bg); border:1px solid var(--border); border-bottom:none; border-radius:6px 6px 0 0; padding:8px 16px; display:flex; align-items:center; gap:10px; }}
  .file-header .path {{ font-family:ui-monospace,Menlo,monospace; font-size:13px; font-weight:600; }}
  .file-header .stats {{ margin-left:auto; font-size:12px; color:#656d76; }}
  .file-header .stats .add {{ color:#1a7f37; font-weight:600; }}
  .file-header .stats .del {{ color:#cf222e; font-weight:600; }}

  /* === Diff viewer: flex dual-pane === */
  /* overflow must stay visible (not hidden): an overflow:hidden ancestor would
     become a non-scrolling scroll container and break the sticky scrollbars
     below (they would no longer follow the page/viewport). */
  .diffv {{ display:flex; border:1px solid var(--border); border-radius:0 0 6px 6px; overflow:visible; background:#fff; }}
  .pane {{ flex:1; min-width:0; display:flex; flex-direction:column; }}
  .pane-head {{ background:var(--header-bg); padding:4px 12px; font-size:11px; font-weight:600; color:#656d76; border-bottom:1px solid var(--border); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .old-pane .pane-head {{ border-right:1px solid var(--border); }}
  /* Each pane's code area is a plain clipped viewport (no native bars here), so
     the two panes' rows always stay perfectly aligned. The horizontal scrollbar
     lives in the .pane-xbar strip pinned to the bottom of each pane box. */
  .pane-body {{ overflow:hidden; flex:1; }}
  /* Always-visible horizontal scrollbar per pane.
     - overflow-x: scroll  keeps the track on both panes at all times (auto would
       only draw it when that column overflows).
     - position: sticky; bottom:0 makes the bar follow the bottom of the current
       viewport while this diff box is on screen, instead of sitting at the very
       bottom of a tall box. Only when the box's own bottom has reached the bottom
       of the screen does the bar rest at the box bottom. */
  .pane-xbar {{ position:sticky; bottom:0; z-index:2; height:16px; overflow-x:scroll; overflow-y:hidden; background:var(--header-bg); border-top:1px solid var(--border); scrollbar-width:auto; }}
  .pane-xbar::-webkit-scrollbar {{ height:10px; }}
  .pane-xbar::-webkit-scrollbar-track {{ background:var(--header-bg); }}
  .pane-xbar::-webkit-scrollbar-thumb {{ background:#c5cbd3; border-radius:5px; }}
  .pane-xbar::-webkit-scrollbar-thumb:hover {{ background:#9aa4af; }}
  .pane-xbar-inner {{ height:1px; min-width:100%; }}

  /* Splitter between panes */
  .splitter {{ width:8px; flex-shrink:0; cursor:col-resize; background:#c5cbd3; position:relative; transition:background .15s; }}
  .splitter:hover, .splitter.resizing {{ background:var(--link); }}
  .splitter::before {{ content:'⇔'; position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); font-size:10px; color:#fff; opacity:0; transition:opacity .15s; pointer-events:none; }}
  .splitter:hover::before {{ opacity:1; }}

  /* Diff rows */
  .drow {{ display:flex; white-space:pre; font-family:ui-monospace,Menlo,monospace; font-size:12px; line-height:1.6; min-height:1.6em; width:max-content; min-width:100%; }}
  .drow .lnum {{ width:48px; flex-shrink:0; text-align:right; padding:0 6px; background:var(--header-bg); color:#8c959f; user-select:none; }}
  .drow .code {{ padding:0 10px; flex:1; min-width:max-content; }}
  .drow.ctx .code {{ background:#fff; }}
  .drow.del .code, .drow.rep .code {{ background:var(--del-bg); }}
  .drow.add .code {{ background:var(--add-bg); }}
  .drow.empty .code {{ background:#f6f8fa; }}
  .drow.empty .lnum {{ background:#f6f8fa; }}
  .drow.gap {{ background:#f6f8fa; border-top:1px solid var(--border); border-bottom:1px solid var(--border); }}
  .drow.gap .lnum {{ color:#8c959f; }}
  .old-pane .drow.rep .code {{ background:var(--del-bg); }}
  .new-pane .drow.rep .code {{ background:var(--add-bg); }}

  /* Inline change highlighting */
  .inline-del {{ background:var(--del-intense); border-radius:2px; padding:0 1px; font-weight:500; }}
  .inline-add {{ background:var(--add-intense); border-radius:2px; padding:0 1px; font-weight:500; }}

  /* Syntax tokens (context lines only) */
  .tok-kw {{ color:#cf222e; font-weight:600; }}
  .tok-type {{ color:#953800; }}
  .tok-str {{ color:#0a3069; }}
  .tok-com {{ color:#6e7781; font-style:italic; }}
  .tok-pre {{ color:#8250df; }}
  .tok-num {{ color:#0550ae; }}

  /* Summary */
  .summary {{ background:#fff; border:1px solid var(--border); border-radius:6px; padding:16px 20px; margin-bottom:24px; }}
  .summary h2 {{ margin:0 0 12px; font-size:16px; }}
  .summary table {{ border-collapse:collapse; width:100%; font-size:13px; }}
  .summary th, .summary td {{ text-align:left; padding:6px 12px; border-bottom:1px solid var(--border); }}
  .summary th {{ background:var(--header-bg); font-weight:600; }}
  .summary td.num {{ text-align:right; font-family:monospace; }}
  .legend {{ display:flex; gap:16px; margin-top:12px; font-size:12px; font-family:ui-monospace,monospace; flex-wrap:wrap; }}
  .legend span {{ display:inline-flex; align-items:center; gap:4px; }}
  .legend .sw {{ display:inline-block; width:14px; height:14px; border-radius:2px; border:1px solid var(--border); }}

  .back-top {{ position:fixed; bottom:20px; right:20px; background:#24292f; color:#fff; border:none; width:40px; height:40px; border-radius:50%; cursor:pointer; font-size:18px; display:none; box-shadow:0 2px 8px rgba(0,0,0,.2); }}
  .back-top:hover {{ background:#373e47; }}
</style>
</head>
<body>

<div class="topbar">
  <h1>Git Diff Report</h1>
  <span class="meta">{repo} · {commit_label} · {gen_time}</span>
  <div class="nav">
    <button id="viewModeBtn" onclick="toggleViewMode()">Full view</button>
    <button onclick="toggleSidebar()">Toggle Files</button>
    <button onclick="window.scrollTo({{top:0}})">Top</button>
  </div>
</div>

<div class="layout">
  <div class="sidebar" id="sidebar" {sidebar_extra}>
    <h2>Changed Files ({file_count})</h2>
    <ul id="fileList">
{sidebar_items}
    </ul>
  </div>
  <div class="sb-resizer" id="sbResizer" {sidebar_extra} title="Drag to resize sidebar"></div>

  <div class="main" id="mainContent">
    <div class="summary">
      <h2>Summary</h2>
      <table>
        <thead><tr><th>File</th><th>Status</th><th class="num">+ Added</th><th class="num">- Removed</th></tr></thead>
        <tbody>
{summary_rows}
        </tbody>
      </table>
      <div class="legend">
        <span><span class="sw" style="background:var(--del-bg)"></span> removed line</span>
        <span><span class="sw" style="background:var(--add-bg)"></span> added line</span>
        <span><span class="sw" style="background:var(--del-intense)"></span> removed text (inline)</span>
        <span><span class="sw" style="background:var(--add-intense)"></span> added text (inline)</span>
        <span><span class="sw" style="background:#c5cbd3"></span> drag to resize</span>
      </div>
    </div>

{file_sections}
  </div>
</div>

<button class="back-top" id="backTop" onclick="window.scrollTo({{top:0}})">↑</button>

<script>
// Scroll tracking
const sections = document.querySelectorAll('.file-section');
const sidebarLinks = document.querySelectorAll('#fileList a');
const backTop = document.getElementById('backTop');
window.addEventListener('scroll', () => {{
  let cur = '';
  sections.forEach(s => {{ if (s.getBoundingClientRect().top <= 120) cur = s.id; }});
  sidebarLinks.forEach(l => {{ l.classList.remove('active'); if (l.getAttribute('href') === '#'+cur) l.classList.add('active'); }});
  backTop.style.display = window.scrollY > 300 ? 'block' : 'none';
}});
function toggleSidebar() {{ ['sidebar','sbResizer'].forEach(id => {{ const el=document.getElementById(id); el.style.display=el.style.display==='none'?'':'none'; }}); }}

// ===== Sidebar resize =====
const sbResizer = document.getElementById('sbResizer');
const sidebar = document.getElementById('sidebar');
let sbDrag = null;
sbResizer.addEventListener('mousedown', e => {{
  sbDrag = {{ startX:e.clientX, startW:sidebar.getBoundingClientRect().width }};
  sbResizer.classList.add('resizing');
  document.body.style.cursor='col-resize'; document.body.style.userSelect='none';
  e.preventDefault();
}});
document.addEventListener('mousemove', e => {{
  if (!sbDrag) return;
  let w = sbDrag.startW + (e.clientX - sbDrag.startX);
  sidebar.style.width = Math.max(160, Math.min(600, w)) + 'px';
}});
document.addEventListener('mouseup', () => {{
  if (sbDrag) {{ sbDrag=null; sbResizer.classList.remove('resizing'); document.body.style.cursor=''; document.body.style.userSelect=''; }}
}});
sbResizer.addEventListener('dblclick', () => {{ sidebar.style.width=''; }});

// ===== Pane splitter resize + synchronized horizontal scroll =====
// Each pane has its own horizontal scrollbar strip (.pane-xbar) at the bottom
// of its box. The strip is a real scroll container; JS sizes its inner content
// to the pane's code width so a native thumb appears only when code overflows.
// Dragging either bar scrolls its pane; all four elements (2 pane bodies + 2
// bars) are kept at the same scrollLeft so the two columns stay aligned.
// `drag` is shared across all viewers: set in each splitter's mousedown,
// read by the document-level mousemove/mouseup handlers below.
let drag = null;
document.querySelectorAll('.diffv').forEach(viewer => {{
  const bodies = [viewer.querySelector('.old-pane .pane-body'),
                  viewer.querySelector('.new-pane .pane-body')];
  const bars = [viewer.querySelector('.old-pane .pane-xbar'),
                viewer.querySelector('.new-pane .pane-xbar')];
  const scrollers = [].concat(bodies, bars);
  const oldP = viewer.querySelector('.old-pane');
  const newP = viewer.querySelector('.new-pane');
  const splitter = viewer.querySelector('.splitter');
  let lock = false;

  // Keep each bar's scrollable width in step with its pane's code width.
  function size() {{
    bars.forEach((bar, i) => {{
      bar.querySelector('.pane-xbar-inner').style.width =
        Math.max(bodies[i].scrollWidth, bar.clientWidth) + 'px';
    }});
  }}

  // One scroll drives all: both pane bodies and both scrollbar strips.
  function drive(src) {{
    if (lock) return;
    lock = true;
    scrollers.forEach(el => {{ el.scrollLeft = src.scrollLeft; }});
    lock = false;
  }}

  scrollers.forEach(el => el.addEventListener('scroll', () => drive(el)));

  // Splitter drag
  splitter.addEventListener('mousedown', e => {{
    const vw = viewer.getBoundingClientRect().width;
    const ow = oldP.getBoundingClientRect().width;
    drag = {{ viewer, oldP, newP, splitter, startX:e.clientX, startOW:ow, vw:vw, size }};
    splitter.classList.add('resizing');
    document.body.style.cursor='col-resize'; document.body.style.userSelect='none';
    e.preventDefault(); e.stopPropagation();
  }});

  // Double-click reset
  splitter.addEventListener('dblclick', e => {{
    oldP.style.flex=''; oldP.style.width='';
    newP.style.flex=''; newP.style.width='';
    size();
    e.stopPropagation();
  }});

  window.addEventListener('resize', size);
  size();
}});

document.addEventListener('mousemove', e => {{
  if (!drag) return;
  let ow = drag.startOW + (e.clientX - drag.startX);
  const splitterW = 8;
  const minW = 120;
  const maxW = drag.vw - splitterW - minW;
  ow = Math.max(minW, Math.min(maxW, ow));
  drag.oldP.style.flex = 'none';
  drag.oldP.style.width = ow + 'px';
  // newPane keeps flex:1, takes remaining
  drag.size();
}});

document.addEventListener('mouseup', () => {{
  if (drag) {{ drag.splitter.classList.remove('resizing'); drag=null; document.body.style.cursor=''; document.body.style.userSelect=''; }}
}});

// ===== Diff-only vs full compare =====
// The DOM always holds every line of both files; this toggles between:
//   'diff' - show only changed lines plus a small context, insert gap markers;
//   'full' - show the whole file side by side.
let viewMode = 'diff';

// Recompute the width of each pane's horizontal scrollbar strip (after rows are
// shown/hidden the content width may change).
function syncXbars() {{
  document.querySelectorAll('.diffv').forEach(viewer => {{
    const bodies = [viewer.querySelector('.old-pane .pane-body'),
                    viewer.querySelector('.new-pane .pane-body')];
    const bars = [viewer.querySelector('.old-pane .pane-xbar'),
                  viewer.querySelector('.new-pane .pane-xbar')];
    bars.forEach((bar, i) => {{
      bar.querySelector('.pane-xbar-inner').style.width =
        Math.max(bodies[i].scrollWidth, bar.clientWidth) + 'px';
    }});
  }});
}}

function setViewMode(mode) {{
  viewMode = mode;
  const btn = document.getElementById('viewModeBtn');
  if (btn) btn.textContent = (mode === 'diff') ? 'Full view' : 'Diff only';

  document.querySelectorAll('.diffv').forEach(viewer => {{
    const bodies = [viewer.querySelector('.old-pane .pane-body'),
                    viewer.querySelector('.new-pane .pane-body')];
    const ctx = parseInt(bodies[0].getAttribute('data-ctx') || '3', 10);

    // Drop previously inserted gap markers so the children list is the original
    // row sequence again (both panes mirror it 1:1).
    bodies.forEach(body => body.querySelectorAll('.drow.gap').forEach(g => g.remove()));

    const rows = [...bodies[0].children];
    const N = rows.length;
    const visible = new Array(N).fill(mode === 'full');

    if (mode === 'diff') {{
      let hasChange = false;
      for (let i = 0; i < N; i++) {{
        const r = rows[i].getAttribute('data-r');
        if (r && r !== 'ctx') {{
          hasChange = true;
          for (let j = Math.max(0, i - ctx); j <= Math.min(N - 1, i + ctx); j++) visible[j] = true;
        }}
      }}
      if (!hasChange) visible.fill(true);
    }}

    bodies.forEach(body => {{
      [...body.children].forEach((el, i) => {{ el.style.display = visible[i] ? '' : 'none'; }});
      if (mode === 'diff') {{
        // For every hidden run insert one "gap" row before it.
        const runs = []; let start = -1;
        for (let i = 0; i <= N; i++) {{
          const hidden = i < N && !visible[i];
          if (hidden && start < 0) start = i;
          if (!hidden && start >= 0) {{ runs.push([start, i - 1]); start = -1; }}
        }}
        for (let k = runs.length - 1; k >= 0; k--) {{
          const gap = document.createElement('div');
          gap.className = 'drow gap';
          gap.innerHTML = '<span class="lnum">⋯</span><span class="code"></span>';
          body.insertBefore(gap, body.children[runs[k][0]]);
        }}
      }}
    }});
  }});
  syncXbars();
}}

function toggleViewMode() {{
  setViewMode(viewMode === 'diff' ? 'full' : 'diff');
}}

// Default to the compact "diff-only" view; the button switches to full compare.
setViewMode('diff');
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Report assembly (shared by the git path and the plain two-file path)
# ---------------------------------------------------------------------------

def build_html(entries, repo_name, commit_label, old_label, new_label,
               syntax=True, context=3, sidebar_collapsed=False):
    """Render the full report.

    entries: list of (status, display_path, old_content, new_content)
    sidebar_collapsed: start with the file-list column hidden (the topbar
    "Toggle Files" button expands it again).
    Returns (html_output, total_add, total_del).
    """
    file_sections, sidebar_items, summary_rows = [], [], []
    total_add = total_del = 0

    for idx, (status, display_path, old_content, new_content) in enumerate(entries):
        anchor = f"file-{idx}"
        status_label = {"M": "Modified", "A": "Added", "D": "Deleted", "R": "Renamed"}.get(status, status)

        old_lines = old_content.splitlines() if old_content else []
        new_lines = new_content.splitlines() if new_content else []

        sm = SequenceMatcher(None, old_lines, new_lines, autojunk=False)
        add_c = del_c = 0
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "insert": add_c += j2 - j1
            elif tag == "delete": del_c += i2 - i1
            elif tag == "replace": del_c += i2 - i1; add_c += j2 - j1
        total_add += add_c; total_del += del_c

        try:
            viewer = make_diff_viewer(
                old_lines, new_lines, display_path,
                syntax=syntax, context=context, idx=idx,
                old_label=old_label, new_label=new_label,
            )
        except Exception as e:
            viewer = f"<p style='color:#cf222e;padding:16px'>Error: {html.escape(str(e))}</p>"

        file_sections.append(f"""    <div class="file-section" id="{anchor}">
      <div class="file-header">
        <span class="badge badge-{status}">{status_label}</span>
        <span class="path">{html.escape(display_path)}</span>
        <span class="stats"><span class="add">+{add_c}</span> / <span class="del">-{del_c}</span></span>
      </div>
      {viewer}
    </div>
""")
        sidebar_items.append(
            f'      <li><a href="#{anchor}"><span class="badge badge-{status}">{status}</span>{html.escape(display_path)}</a></li>'
        )
        summary_rows.append(
            f'          <tr><td><a href="#{anchor}" style="color:var(--link);text-decoration:none">{html.escape(display_path)}</a></td>'
            f'<td><span class="badge badge-{status}">{status_label}</span></td>'
            f'<td class="num" style="color:#1a7f37">+{add_c}</td>'
            f'<td class="num" style="color:#cf222e">-{del_c}</td></tr>'
        )

    summary_rows.append(
        f'          <tr style="font-weight:600;background:var(--header-bg)">'
        f'<td>Total ({len(entries)} files)</td><td></td>'
        f'<td class="num" style="color:#1a7f37">+{total_add}</td>'
        f'<td class="num" style="color:#cf222e">-{total_del}</td></tr>'
    )

    html_output = HTML_TEMPLATE.format(
        title=repo_name, repo=repo_name, commit_label=commit_label,
        gen_time=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        file_count=len(entries),
        sidebar_extra='style="display:none"' if sidebar_collapsed else "",
        sidebar_items="\n".join(sidebar_items),
        summary_rows="\n".join(summary_rows),
        file_sections="\n".join(file_sections),
    )
    return html_output, total_add, total_del


def write_report(html_output, output=None):
    """Write the report to `output` (or a secure temp file) and return its path."""
    if output:
        out_path = os.path.abspath(output)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html_output)
    else:
        # Produce the report in the system temp dir (TMPDIR or /tmp) under a
        # secure, randomly-generated name via mkstemp, then keep the file open
        # for writing through the same descriptor (avoids chmod/rename races).
        fd, out_path = tempfile.mkstemp(prefix="git_diff_", suffix=".html")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(html_output)
    return out_path


def _finish(html_output, out_files, total_add, total_del, args):
    out_path = write_report(html_output, args.output)
    print(f"\nDiff report generated: {out_path}")
    print(f"  Files: {out_files}  (+{total_add} / -{total_del} lines)")

    if not args.no_open:
        import webbrowser
        webbrowser.open(f"file://{out_path}")
        print(f"  Opened in default browser: file://{out_path}")
    else:
        print(f"  Open in browser: file://{out_path}")


def _plain_file_diff(args, cwd):
    """No git work tree available: compare exactly two files side by side."""
    if args.range or args.staged or args.commit:
        print("Error: -r/--range, -s/--staged and -c/--commit need a git repository.",
              file=sys.stderr)
        sys.exit(1)
    paths = list(args.files)
    if len(paths) != 2:
        print("Error: not a git repository.", file=sys.stderr)
        print(f"  Usage: {os.path.basename(sys.argv[0])} FILE_OLD FILE_NEW   "
              "(plain compare outside a repo)", file=sys.stderr)
        sys.exit(1)

    contents = []
    for p in paths:
        full = p if os.path.isabs(p) else os.path.join(cwd, p)
        if not os.path.isfile(full):
            print(f"Error: '{p}' is not a readable file.", file=sys.stderr)
            sys.exit(1)
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            contents.append(f.read())
    old_path, new_path = paths

    repo_name = os.path.basename(os.path.abspath(cwd)) or os.path.abspath(cwd)
    display = f"{old_path} → {new_path}"
    entries = [("M", display, contents[0], contents[1])]
    html_output, total_add, total_del = build_html(
        entries, repo_name, commit_label="file compare",
        old_label=f"{old_path} (old)", new_label=f"{new_path} (new)",
        syntax=not args.no_syntax, context=args.context, sidebar_collapsed=True,
    )
    _finish(html_output, 1, total_add, total_del, args)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate a Gerrit-style side-by-side HTML diff with inline highlighting.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-o", "--output", default=None,
                        help="Output HTML file (default: securely-named temp file in the system temp dir)")
    parser.add_argument("-c", "--commit", default=None,
                        help="compare against a specific commit/ref (default: HEAD)")
    parser.add_argument("-r", "--range", nargs=2, metavar=("REF1", "REF2"),
                        help="compare two commits directly: gdhtm -r REF1 REF2")
    parser.add_argument("-s", "--staged", action="store_true")
    parser.add_argument("--no-open", action="store_true",
                        help="do not auto-open the report in the default browser (open is default)")
    parser.add_argument("--no-syntax", action="store_true")
    parser.add_argument("-C", "--context", type=int, default=3)
    parser.add_argument(
        "files", nargs="*", metavar="[ref] [file ...]",
        help="first arg that git resolves to a revision (hash/branch/HEAD~N) is the "
             "compare ref; the rest are file filters",
    )
    args = parser.parse_args()

    cwd = os.getcwd()
    stdout, rc = run_git(["rev-parse", "--show-toplevel"], cwd)
    if rc != 0:
        # Outside any git work tree the only thing we can produce is a plain
        # side-by-side compare of two files given as arguments.
        _plain_file_diff(args, cwd)
        return
    repo_root = stdout.strip()
    os.chdir(repo_root)

    staged = args.staged

    if args.range:
        # Explicit two-commit compare: pass both refs straight to `git diff`.
        # All positionals are file filters here.
        base_ref, target_ref = args.range
        old_label, new_label = f"{base_ref} (old)", f"{target_ref} (new)"
        commit_label = f"{base_ref} .. {target_ref}"
        file_filter = list(args.files)
    else:
        # Positional: at most ONE ref — the first arg that git resolves to a
        # revision (hash/branch/tag/HEAD~N) is the base; the rest are files.
        # Two-commit compare is only via `-r REF1 REF2`, so no ambiguity
        # between "ref + file" and "two commits".
        base_ref = None
        file_filter = []
        for a in args.files:
            if base_ref is None:
                chk, _ = run_git(["rev-parse", "--verify", "--quiet", a], repo_root)
                if chk.strip():
                    base_ref = a
                    continue
            file_filter.append(a)

        base_ref = base_ref or args.commit or "HEAD"
        target_ref = None
        if staged:
            old_label, new_label = "HEAD (old)", "STAGED (new)"
            commit_label = "staged vs HEAD"
        else:
            old_label, new_label = f"{base_ref} (old)", "WORKING (new)"
            commit_label = f"vs {base_ref}"

    # Guard against silent confusion instead of a bare "No changed files":
    # (a) comparing a commit against itself, (b) a typo'd file filter path.
    if target_ref is not None:
        b, _ = run_git(["rev-parse", base_ref], repo_root)
        t, _ = run_git(["rev-parse", target_ref], repo_root)
        if b.strip() and b.strip() == t.strip():
            print(f"WARNING: '{base_ref}' and '{target_ref}' are the same commit — nothing to compare",
                  file=sys.stderr)
    for p in file_filter:
        if not os.path.exists(os.path.join(repo_root, p)):
            in_index = run_git(["ls-files", "--error-unmatch", "--", p], repo_root)[1] == 0
            in_refs = False
            for ref in (base_ref, target_ref):
                if ref and run_git(["cat-file", "-e", f"{ref}:{p}"], repo_root)[1] == 0:
                    in_refs = True
                    break
            if not in_index and not in_refs:
                print(f"WARNING: '{p}' not found in worktree, git index, or the compared "
                      f"refs — check for typos", file=sys.stderr)

    files = get_changed_files(repo_root, base_ref, target_ref, staged, file_filter)
    if not files:
        print("No changed files found.")
        print("  Tip: verify the refs and paths above exist; identical versions produce an empty diff.")
        sys.exit(0)

    repo_name = os.path.basename(repo_root)

    entries = []
    for status, old_path, new_path in files:
        display_path = new_path if status != "D" else old_path
        old_content, new_content = get_file_content(
            repo_root, base_ref, old_path if status != "A" else new_path, staged, target_ref
        )
        if status == "R":
            _, new_content = get_file_content(repo_root, base_ref, new_path, staged, target_ref)
        entries.append((status, display_path, old_content, new_content))

    html_output, total_add, total_del = build_html(
        entries, repo_name, commit_label, old_label, new_label,
        syntax=not args.no_syntax, context=args.context,
    )
    _finish(html_output, len(files), total_add, total_del, args)


if __name__ == "__main__":
    main()
