"""End-to-end demo arc: the bundled legacy app fails on v2, BreakBot patches
it, then its pytest suite passes."""
import os
import shutil
import subprocess
import sys

import pytest

from breakbot import demo as demo_pkg
from breakbot.codemod import apply_fixes
from breakbot.engine import analyze

pytestmark = pytest.mark.slow


def _pytest_result(d):
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=d,
                          capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def test_demo_red_before_green_after(tmp_path):
    work = tmp_path / "app"
    shutil.copytree(demo_pkg.legacy_app_path(), work)

    # Before: the legacy app must FAIL on the installed pydantic v2 / sqla 2.0
    rc_before, out_before = _pytest_result(work)
    assert rc_before != 0, "legacy app should fail on upgraded deps"
    assert "regex" in out_before or "execute" in out_before

    # Analyze: both upgrades report findings
    r1 = analyze(work, "pydantic1-to-2")
    r2 = analyze(work, "sqlalchemy1.4-to-2.0")
    assert r1.breaks and r1.warnings
    assert r2.breaks
    # every verdict carries a changelog citation with a line number
    for v in r1.all_verdicts + r2.all_verdicts:
        assert v.citations and v.citations[0].line >= 1

    # Patch both upgrades
    res1 = apply_fixes(work, "pydantic1-to-2")
    res2 = apply_fixes(work, "sqlalchemy1.4-to-2.0")
    assert res1.changed_files and res2.changed_files

    # After: the demo's own test suite is green
    rc_after, out_after = _pytest_result(work)
    assert rc_after == 0, f"expected green after patch, got:\n{out_after}"
    assert "passed" in out_after


def test_demo_pydantic_only_arc(tmp_path):
    """Pydantic-only subset: models/api/config tests green after pydantic patch."""
    work = tmp_path / "app"
    shutil.copytree(demo_pkg.legacy_app_path(), work)
    rc_before, _ = _pytest_result(work)
    assert rc_before != 0
    apply_fixes(work, "pydantic1-to-2")
    rc, out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_models.py",
         "tests/test_api.py"],
        cwd=work, capture_output=True, text=True,
    ).returncode, ""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_models.py",
         "tests/test_api.py"],
        cwd=work, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
