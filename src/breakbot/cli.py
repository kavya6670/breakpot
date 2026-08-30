"""BreakBot command-line interface.

    breakbot list
    breakbot analyze <repo> --upgrade pydantic1-to-2 [--html report.html | --json]
    breakbot fix     <repo> --upgrade pydantic1-to-2 [--test] [--web]
    breakbot web     [--repo <path>] [--upgrade <id>] [--port 8000]
    breakbot demo    [--workdir /tmp/breakbot-demo]   # full canned demo arc
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__
from .codemod import apply_fixes
from .engine import analyze
from .knowledge import UPGRADES
from .report import render_html, render_json, render_text


def _print(text: str) -> None:
    print(text)


def cmd_list(_args) -> int:
    _print("Supported upgrades:\n")
    for uid, up in UPGRADES.items():
        _print(f"  {uid:24s} {up.title}  ({up.package_from} → {up.package_to})")
        _print(f"      {up.summary}\n")
    return 0


def cmd_analyze(args) -> int:
    report = analyze(args.repo, args.upgrade)
    if getattr(args, "llm", False):
        from .llm import llm_configured, verify_report
        if llm_configured():
            verify_report(report)
        else:
            _print("(LLM verifier requested but BREAKBOT_LLM_API_KEY not set; "
                   "using rules-engine verdicts)")
    if args.json:
        _print(render_json(report))
    elif args.html:
        Path(args.html).write_text(render_html(report), encoding="utf-8")
        _print(f"Wrote {args.html} — "
               f"{len(report.breaks)} breaks, {len(report.warnings)} warnings, "
               f"{report.unaffected_count} unaffected.")
    else:
        _print(render_text(report))
    # exit code reflects breakage so CI can gate on it
    return 1 if report.breaks else 0


def cmd_fix(args) -> int:
    result = apply_fixes(args.repo, args.upgrade)
    if result.changed_files:
        _print("Applied auto-fixes to:")
        for f in result.changed_files:
            _print(f"  ✓ {f}")
        _print("\nUnified diff (originals backed up under .breakbot-work/backup/):\n")
        _print(result.diff)
    else:
        _print("No auto-fixable findings.")
    if result.skipped:
        _print("\nManual / skipped:")
        for note in result.skipped:
            _print(f"  • {note}")
    if args.test:
        tr = run_tests(args.repo)
        _print(f"\nTests: {'GREEN ✓' if tr.green else 'RED ✗'}  ({tr.command})")
        _print(tr.stdout[-4000:])
        if not tr.green:
            _print(tr.stderr[-2000:])
            return 2
    return 0


def run_tests(repo: str | Path) -> "object":
    from .models import TestResult
    repo = Path(repo)
    cmd = [sys.executable, "-m", "pytest", "-q"]
    proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    return TestResult(command=" ".join(cmd), returncode=proc.returncode,
                      stdout=proc.stdout, stderr=proc.stderr)


def cmd_web(args) -> int:
    from .web import create_app
    app = create_app(initial_repo=args.repo, initial_upgrade=args.upgrade)
    app.run(host="0.0.0.0", port=args.port, debug=False)
    return 0


def cmd_demo(args) -> int:
    """Full canned demo arc: copy legacy app → red report → fix → green tests."""
    from . import demo as demo_pkg
    work = Path(args.workdir or tempfile.mkdtemp(prefix="breakbot-demo-"))
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(demo_pkg.legacy_app_path(), work, dirs_exist_ok=True)
    # The bundled demo app uses both pydantic and SQLAlchemy; run both passes.
    upgrades = [args.upgrade or "pydantic1-to-2"]
    if "pydantic1-to-2" in upgrades:
        upgrades.append("sqlalchemy1.4-to-2.0")

    _print("=" * 72)
    _print(f"STEP 1  Analyzing legacy app at {work}")
    _print("=" * 72)
    reports = []
    for up in upgrades:
        report = analyze(work, up)
        reports.append(report)
        _print(f"\n--- {report.upgrade_title} ---")
        _print(render_text(report))

    _print("\n" + "=" * 72)
    _print("STEP 2  Test suite BEFORE the patch (expect RED)")
    _print("=" * 72)
    before = run_tests(work)
    _print(f"tests: {'GREEN ✓' if before.green else 'RED ✗'} (as expected before fix)")
    _print(before.stdout[-800:])

    _print("\n" + "=" * 72)
    _print("STEP 3  Applying BreakBot fix patches")
    _print("=" * 72)
    diffs = []
    for up in upgrades:
        _print(f"\n--- {UPGRADES[up].title} ---")
        result = apply_fixes(work, up)
        diffs.append(result.diff)
        _print(result.diff or "(no auto-fixable findings)")
        for note in result.skipped:
            _print(f"  manual: {note}")

    _print("\n" + "=" * 72)
    _print("STEP 4  Test suite AFTER the patch (expect GREEN)")
    _print("=" * 72)
    after = run_tests(work)
    _print(f"tests: {'GREEN ✓' if after.green else 'RED ✗'}")
    _print(after.stdout[-1500:])

    # Combined report (pydantic report first) for the HTML artifact.
    primary = reports[0]
    html_path = work / "breakbot-report.html"
    html_path.write_text(
        render_html(primary, fix_applied=True, diff="".join(diffs),
                    test_output={"returncode": after.returncode,
                                 "stdout": after.stdout, "stderr": after.stderr}),
        encoding="utf-8")
    _print(f"\nReport written to {html_path}")
    return 0 if after.green else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="breakbot", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list supported upgrades").set_defaults(
        func=cmd_list)

    a = sub.add_parser("analyze", help="analyze a repo")
    a.add_argument("repo")
    a.add_argument("--upgrade", required=True, choices=list(UPGRADES))
    g = a.add_mutually_exclusive_group()
    g.add_argument("--html", metavar="FILE")
    g.add_argument("--json", action="store_true")
    a.add_argument("--llm", action="store_true",
                   help="cross-check medium-confidence verdicts with an LLM "
                        "(requires BREAKBOT_LLM_API_KEY)")
    a.set_defaults(func=cmd_analyze)

    f = sub.add_parser("fix", help="apply auto-fix patch")
    f.add_argument("repo")
    f.add_argument("--upgrade", required=True, choices=list(UPGRADES))
    f.add_argument("--test", action="store_true", help="run pytest after patching")
    f.set_defaults(func=cmd_fix)

    w = sub.add_parser("web", help="start the web report UI")
    w.add_argument("--repo")
    w.add_argument("--upgrade", default="pydantic1-to-2", choices=list(UPGRADES))
    w.add_argument("--port", type=int, default=8000)
    w.set_defaults(func=cmd_web)

    d = sub.add_parser("demo", help="run the canned demo arc")
    d.add_argument("--workdir")
    d.add_argument("--upgrade", default="pydantic1-to-2", choices=list(UPGRADES))
    d.set_defaults(func=cmd_demo)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
