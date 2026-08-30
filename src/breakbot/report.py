"""Report rendering: terminal text, JSON, and the static/web HTML view."""
from __future__ import annotations

import html
import json
from collections import Counter

from .models import Report, Severity, Verdict


# --------------------------------------------------------------------------- #
# Terminal
# --------------------------------------------------------------------------- #

def render_text(report: Report) -> str:
    lines: list[str] = []
    lines.append(f"BreakBot impact report — {report.upgrade_title}")
    lines.append(f"repo: {report.root}")
    lines.append(
        f"files scanned: {report.files_scanned} | "
        f"✗ {len(report.breaks)} breaks | ⚠ {len(report.warnings)} warnings | "
        f"✓ {report.unaffected_count} usages unaffected"
    )
    fixable = len(report.fixable)
    if fixable:
        lines.append(f"{fixable} finding(s) have an auto-fix "
                     f"(`breakbot fix <repo> --upgrade {report.upgrade_id}`)")
    lines.append("")
    for fr in report.file_reports:
        if not fr.verdicts:
            continue
        lines.append(f"{fr.path}")
        for v in sorted(fr.verdicts, key=lambda x: (x.severity != Severity.BREAK,
                                                    x.call_site.line if x.call_site else 0)):
            loc = v.short_location
            sym = v.call_site.symbol if v.call_site else v.title
            lines.append(f"  {v.glyph} {loc}  {v.title}")
            lines.append(f"      {v.detail}")
            for c in v.citations:
                lines.append(f"      ↳ cited: {c.as_text()} — {c.url}")
            if v.auto_fixable:
                lines.append(f"      🔧 {v.fix_summary}")
        lines.append("")
    manual = [v for v in report.all_verdicts if not v.auto_fixable
              and v.severity == Severity.BREAK]
    if manual:
        lines.append("Manual migration required:")
        for v in manual:
            lines.append(f"  • {v.short_location}: {v.title}")
    return "\n".join(lines)


def render_json(report: Report) -> str:
    def verdict_dict(v: Verdict):
        return {
            "severity": v.severity.value,
            "rule_id": v.rule_id,
            "title": v.title,
            "detail": v.detail,
            "file": v.file,
            "line": v.call_site.line if v.call_site else None,
            "col": v.call_site.col if v.call_site else None,
            "snippet": v.call_site.snippet if v.call_site else None,
            "symbol": v.call_site.symbol if v.call_site else None,
            "context": v.call_site.context if v.call_site else None,
            "auto_fixable": v.auto_fixable,
            "fix_summary": v.fix_summary,
            "confidence": v.confidence.value,
            "citations": [
                {"source": c.source, "section": c.section, "line": c.line,
                 "quote": c.quote, "url": c.url}
                for c in v.citations
            ],
        }
    return json.dumps({
        "upgrade_id": report.upgrade_id,
        "upgrade_title": report.upgrade_title,
        "root": report.root,
        "files_scanned": report.files_scanned,
        "breaks": len(report.breaks),
        "warnings": len(report.warnings),
        "unaffected": report.unaffected_count,
        "fixable": len(report.fixable),
        "files": [
            {"path": fr.path,
             "verdicts": [verdict_dict(v) for v in fr.verdicts]}
            for fr in report.file_reports if fr.verdicts
        ],
        "notes": report.notes,
    }, indent=2)


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

_COLORS = {
    Severity.BREAK: ("#ff5c5c", "#3a1414", "BREAKS"),
    Severity.WARNING: ("#ffb84d", "#3a2e14", "WARNING"),
    Severity.OK: ("#4dd68b", "#123020", "OK"),
}


def _esc(s: str) -> str:
    return html.escape(s or "")


def render_html(report: Report, *, fix_applied: bool = False,
                diff: str = "", test_output: dict | None = None) -> str:
    n_break, n_warn = len(report.breaks), len(report.warnings)
    n_ok = report.unaffected_count
    fixable = len(report.fixable)

    rows = []
    for fr in report.file_reports:
        verdicts = sorted(
            fr.verdicts,
            key=lambda v: (v.severity != Severity.BREAK,
                           v.call_site.line if v.call_site else 0),
        )
        for v in verdicts:
            color, bg, label = _COLORS[v.severity]
            cs = v.call_site
            cites = "".join(
                f'<div class="cite">↳ cited: <b>{_esc(c.as_text())}</b> — '
                f'<a href="{_esc(c.url)}" target="_blank" rel="noopener">{_esc(c.quote)}</a></div>'
                for c in v.citations
            )
            fix = (
                f'<div class="fix">🔧 <b>Auto-fix:</b> {_esc(v.fix_summary)}</div>'
                if v.auto_fixable else
                '<div class="fix manual">✋ manual migration required</div>'
            )
            rows.append(f"""
            <div class="card" style="border-left:4px solid {color};background:{bg}">
              <div class="card-head">
                <span class="badge" style="background:{color}">{v.glyph} {label}</span>
                <span class="loc">{_esc(fr.path)}:{cs.line if cs else 0}</span>
                <span class="sym">{_esc(cs.symbol if cs else v.title)}</span>
                <span class="conf">confidence: {v.confidence.value}</span>
              </div>
              <div class="title">{_esc(v.title)}</div>
              <div class="detail">{_esc(v.detail)}</div>
              {cites}
              {fix}
            </div>""")

    diff_block = ""
    if diff:
        diff_html = _render_diff(diff)
        diff_block = f"""
        <h2>Generated fix patch</h2>
        <pre class="diff">{diff_html}</pre>"""

    test_block = ""
    if test_output is not None:
        color = "#4dd68b" if test_output.get("returncode") == 0 else "#ff5c5c"
        status = "GREEN ✓" if test_output.get("returncode") == 0 else "RED ✗"
        out = _esc(test_output.get("stdout", "") + test_output.get("stderr", ""))
        test_block = f"""
        <h2>Test suite after patch</h2>
        <div class="test-status" style="color:{color}">tests: {status}</div>
        <pre class="test-out">{out}</pre>"""

    button = ""
    if not fix_applied and fixable:
        button = """
        <form method="post" action="/fix" style="display:inline">
          <button class="big-btn" type="submit">⚙ Generate fix PR</button>
        </form>"""
    elif fix_applied:
        button = """
        <div class="patched-note">✅ Patch applied &nbsp;
        <form method="post" action="/tests" style="display:inline">
          <button class="big-btn" type="submit">▶ Run test suite</button>
        </form></div>"""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>BreakBot — {_esc(report.upgrade_title)}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ font-family: 'SF Mono', Menlo, Consolas, monospace; background:#0d1117;
          color:#e6edf3; margin:0; padding:32px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  h2 {{ font-size:16px; margin-top:32px; border-bottom:1px solid #30363d; padding-bottom:6px;}}
  .sub {{ color:#8b949e; margin-bottom:24px; font-size:13px; }}
  .stats {{ display:flex; gap:16px; margin:20px 0; flex-wrap:wrap; }}
  .stat {{ padding:14px 22px; border-radius:10px; font-size:15px; font-weight:700;
           border:1px solid #30363d; min-width:120px; text-align:center;}}
  .stat small {{ display:block; font-weight:400; color:#8b949e; font-size:11px; margin-top:4px;}}
  .card {{ border-radius:8px; padding:12px 16px; margin:10px 0; }}
  .card-head {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; font-size:12px;}}
  .badge {{ padding:2px 10px; border-radius:10px; color:#0d1117; font-weight:700;}}
  .loc {{ color:#79c0ff; }}
  .sym {{ color:#d2a8ff; }}
  .conf {{ color:#8b949e; margin-left:auto;}}
  .title {{ font-weight:700; margin:8px 0 4px; }}
  .detail {{ color:#c9d1d9; font-size:13px; }}
  .cite {{ font-size:12px; color:#8b949e; margin-top:6px; }}
  .cite a {{ color:#58a6ff; text-decoration:none; }}
  .fix {{ font-size:12px; margin-top:8px; color:#7ee787; }}
  .fix.manual {{ color:#ffa657; }}
  .big-btn {{ background:#238636; color:#fff; border:0; border-radius:8px;
             padding:12px 24px; font-size:15px; font-weight:700; cursor:pointer;
             font-family:inherit; margin:8px 0;}}
  .big-btn:hover {{ background:#2ea043; }}
  .patched-note {{ color:#7ee787; font-size:14px; margin:8px 0; font-weight:700;}}
  .patched-note form {{ display:inline; }}
  .patched-note .big-btn {{ background:#1f6feb; margin-left:12px;}}
  .patched-note .big-btn:hover {{ background:#388bfd; }}
  pre.diff, pre.test-out {{ background:#161b22; border:1px solid #30363d; border-radius:8px;
           padding:14px; overflow-x:auto; font-size:12px; line-height:1.5; white-space:pre;}}
  .test-status {{ font-size:18px; font-weight:700; margin:10px 0; }}
  .files {{ color:#8b949e; font-size:13px; margin-top:16px;}}
</style></head>
<body>
  <h1>🤖 BreakBot — dependency-upgrade impact report</h1>
  <div class="sub">upgrade: <b>{_esc(report.upgrade_title)}</b> &nbsp;|&nbsp; repo: {_esc(report.root)}</div>
  <div class="stats">
    <div class="stat" style="color:#ff5c5c">{n_break}<small>BREAKS</small></div>
    <div class="stat" style="color:#ffb84d">{n_warn}<small>WARNINGS</small></div>
    <div class="stat" style="color:#4dd68b">{n_ok}<small>UNAFFECTED USAGES</small></div>
    <div class="stat" style="color:#79c0ff">{report.files_scanned}<small>FILES SCANNED</small></div>
  </div>
  {button}
  <div class="files">{_esc("Every verdict below cites the exact changelog line it is based on.")}</div>
  <h2>Verdicts ({n_break + n_warn})</h2>
  {''.join(rows)}
  {diff_block}
  {test_block}
</body></html>"""


def _render_diff(diff: str) -> str:
    out = []
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            out.append(f'<span style="color:#8b949e">{_esc(line)}</span>')
        elif line.startswith("@@"):
            out.append(f'<span style="color:#d2a8ff">{_esc(line)}</span>')
        elif line.startswith("+"):
            out.append(f'<span style="color:#7ee787">{_esc(line)}</span>')
        elif line.startswith("-"):
            out.append(f'<span style="color:#ff7b72">{_esc(line)}</span>')
        else:
            out.append(_esc(line))
    return "\n".join(out)
