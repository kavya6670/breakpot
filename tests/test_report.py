"""Report rendering smoke tests."""
import textwrap

from breakbot.engine import analyze
from breakbot.report import render_html, render_json, render_text
import json


def test_text_report_contains_verdicts(tmp_path):
    (tmp_path / "m.py").write_text(textwrap.dedent('''
        from pydantic import BaseModel, Field
        class M(BaseModel):
            email: str = Field(..., regex="x")
    '''))
    r = analyze(tmp_path, "pydantic1-to-2")
    text = render_text(r)
    assert "✗" in text and "cited:" in text and "pydantic-1-to-2.md" in text


def test_json_report_roundtrips(tmp_path):
    (tmp_path / "m.py").write_text(textwrap.dedent('''
        from pydantic import BaseModel
        class M(BaseModel):
            x: int
        print(M(x=1).dict())
    '''))
    r = analyze(tmp_path, "pydantic1-to-2")
    data = json.loads(render_json(r))
    assert data["upgrade_id"] == "pydantic1-to-2"
    assert data["breaks"] == 0 and data["warnings"] >= 1
    v = data["files"][0]["verdicts"][0]
    assert v["citations"][0]["line"] > 0
    assert v["citations"][0]["url"].startswith("http")


def test_html_report_escapes_and_includes_button(tmp_path):
    (tmp_path / "m.py").write_text(textwrap.dedent('''
        from pydantic import BaseModel, Field
        class M(BaseModel):
            email: str = Field(..., regex="x")
    '''))
    r = analyze(tmp_path, "pydantic1-to-2")
    html = render_html(r)
    assert "<!doctype html>" in html.lower()
    assert "Generate fix PR" in html
    assert "BREAKS" in html
    assert "docs.pydantic.dev" in html
    # fixed view shows the run-tests button
    html_fixed = render_html(r, fix_applied=True, diff="--- a/x\n+print(1)\n")
    assert "Run test suite" in html_fixed and "diff" in html_fixed.lower()
