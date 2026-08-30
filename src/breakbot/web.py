"""Flask web UI — the interactive demo arc.

    /                 analyze form + report
    /fix   (POST)     apply the auto-fix patch, show the unified diff
    /tests (POST)     run the demo's pytest suite and show red/green
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from flask import Flask, Response, redirect, request, session, url_for

from .codemod import apply_fixes
from .engine import analyze
from .knowledge import UPGRADES
from .report import render_html


def create_app(initial_repo: str | None = None, initial_upgrade: str | None = None):
    app = Flask(__name__)
    app.secret_key = os.environ.get("BREAKBOT_SECRET", "breakbot-dev-key")
    state: dict = {}

    def _current_workdir() -> Path | None:
        wd = state.get("workdir")
        return Path(wd) if wd else None

    def _analyze():
        wd = _current_workdir()
        upgrade = state.get("upgrade", "pydantic1-to-2")
        if wd is None:
            return None
        return analyze(wd, upgrade), upgrade

    @app.get("/")
    def index():
        if state.get("workdir"):
            fixed = state.get("fixed", False)
            # always show the original red report (what was detected);
            # the patch + test status prove the fixes work.
            report = state.get("report") if fixed else None
            if report is None:
                result = _analyze()
                if result is None:
                    return "bad state", 500
                report, upgrade = result
                if not fixed:
                    state["report"] = report
            return render_html(report, fix_applied=fixed,
                               diff=state.get("diff", ""),
                               test_output=state.get("tests"))
        options = "".join(
            f'<option value="{uid}"{" selected" if uid == (initial_upgrade or "pydantic1-to-2") else ""}>{up.title}</option>'
            for uid, up in UPGRADES.items()
        )
        return f"""<!doctype html><html><head><meta charset="utf-8">
<title>BreakBot</title>
<style>
body {{ font-family: 'SF Mono', Menlo, Consolas, monospace; background:#0d1117;
       color:#e6edf3; margin:0; padding:48px; max-width:900px;}}
h1 {{ font-size:26px; }}
.box {{ background:#161b22; border:1px solid #30363d; border-radius:10px;
       padding:24px; margin:24px 0; }}
input, select, button {{ font-family:inherit; font-size:14px; padding:10px 14px;
       border-radius:6px; border:1px solid #30363d; background:#0d1117;
       color:#e6edf3; margin:6px 0; width:100%; box-sizing:border-box;}}
button {{ background:#238636; border:0; font-weight:700; cursor:pointer; width:auto;}}
button:hover {{ background:#2ea043; }}
label {{ color:#8b949e; font-size:12px; }}
.tag {{ display:inline-block; background:#21262d; border-radius:4px; padding:2px 8px;
       font-size:12px; margin:2px; color:#79c0ff;}}
</style></head><body>
<h1>🤖 BreakBot</h1>
<p style="color:#8b949e">“npm audit tells you what’s vulnerable. Nothing tells you what breaks.”<br>
Point BreakBot at a Python repo and an upgrade — it finds every call site the
changelog affects, cites the changelog lines, and generates the fix patch.</p>
<form method="post" action="/analyze">
  <div class="box">
    <label>REPO PATH (leave blank to use the bundled demo app — a pydantic-v1-era service)</label>
    <input name="repo" value="{initial_repo or ''}" placeholder="(bundled demo app)">
    <label>UPGRADE</label>
    <select name="upgrade">{options}</select>
    <div style="margin-top:14px">
      <button type="submit">🔍 Analyze impact</button>
    </div>
  </div>
</form>
<p style="color:#8b949e">Supported upgrades:
  {"".join(f'<span class="tag">{uid}</span>' for uid in UPGRADES)}</p>
</body></html>"""

    @app.post("/analyze")
    def do_analyze():
        repo = (request.form.get("repo") or "").strip()
        upgrade = request.form.get("upgrade", "pydantic1-to-2")
        if repo:
            work = Path(tempfile.mkdtemp(prefix="breakbot-web-"))
            shutil.copytree(repo, work, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(
                                ".venv", "venv", "__pycache__", ".git",
                                "node_modules", ".breakbot-work"))
            state["workdir"] = str(work)
            state["is_demo"] = False
        else:
            from .demo import legacy_app_path
            work = Path(tempfile.mkdtemp(prefix="breakbot-web-demo-"))
            shutil.copytree(legacy_app_path(), work, dirs_exist_ok=True)
            state["workdir"] = str(work)
            state["is_demo"] = True
        state["upgrade"] = upgrade
        state["fixed"] = False
        state["diff"] = ""
        state["tests"] = None
        state["report"] = analyze(state["workdir"], upgrade)
        return redirect(url_for("index"))

    @app.post("/fix")
    def do_fix():
        wd = _current_workdir()
        upgrade = state.get("upgrade", "pydantic1-to-2")
        if wd is None:
            return redirect(url_for("index"))
        diffs: list[str] = []
        skipped: list[str] = []
        applied = [upgrade]
        result = apply_fixes(wd, upgrade)
        diffs.append(result.diff)
        skipped.extend(result.skipped)
        # If the repo also uses the other famous upgrade, offer/apply it
        # automatically so the whole demo arc reaches green in one click.
        follow_up = {"pydantic1-to-2": "sqlalchemy1.4-to-2.0",
                     "sqlalchemy1.4-to-2.0": "pydantic1-to-2"}[upgrade]
        other = analyze(wd, follow_up)
        if other.file_reports and (other.breaks or other.warnings):
            r2 = apply_fixes(wd, follow_up)
            if r2.changed_files:
                applied.append(follow_up)
                diffs.append(r2.diff)
                skipped.extend(r2.skipped)
        state["fixed"] = True
        state["diff"] = "".join(diffs)
        state["test_skipped"] = skipped
        state["applied"] = applied
        # keep the pre-fix red report for the HTML; do not overwrite it
        return redirect(url_for("index"))

    @app.post("/tests")
    def do_tests():
        wd = _current_workdir()
        if wd is None:
            return redirect(url_for("index"))
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=wd, capture_output=True, text=True)
        state["tests"] = {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
        return redirect(url_for("index"))

    @app.get("/diff")
    def show_diff():
        return Response(state.get("diff", ""), mimetype="text/plain")

    return app
