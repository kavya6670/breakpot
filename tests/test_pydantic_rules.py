"""Detection verdicts for the pydantic 1→2 upgrade."""
import textwrap

import pytest

from breakbot.engine import analyze
from breakbot.ingest import ingest_repo
from breakbot.models import Severity


def analyze_code(tmp_path, code, name="m.py"):
    (tmp_path / name).write_text(textwrap.dedent(code))
    return analyze(tmp_path, "pydantic1-to-2")


def test_dict_rename_cited(tmp_path):
    r = analyze_code(tmp_path, """
        from pydantic import BaseModel
        class M(BaseModel):
            x: int
        def f():
            return M(x=1).dict()
    """)
    v = next(v for v in r.all_verdicts if ".dict()" in v.title)
    assert v.severity == Severity.WARNING and v.auto_fixable
    assert v.citations and "dict" in v.citations[0].quote
    assert v.citations[0].line > 0 and v.citations[0].url.startswith("http")


def test_regex_is_break(tmp_path):
    r = analyze_code(tmp_path, """
        from pydantic import BaseModel, Field
        class M(BaseModel):
            email: str = Field(..., regex="x")
    """)
    breaks = [v for v in r.breaks if "regex" in v.title]
    assert breaks and breaks[0].auto_fixable


def test_validator_decorator(tmp_path):
    r = analyze_code(tmp_path, """
        from pydantic import BaseModel, validator
        class M(BaseModel):
            x: str
            @validator("x")
            def v(cls, v): return v
    """)
    v = next(v for v in r.all_verdicts if "field_validator" in v.title)
    assert v.auto_fixable and "@classmethod" in v.fix_summary


def test_validator_each_item_manual(tmp_path):
    r = analyze_code(tmp_path, """
        from pydantic import BaseModel, validator
        from typing import List
        class M(BaseModel):
            xs: List[int] = []
            @validator("xs", each_item=True)
            def v(cls, v): return v
    """)
    v = next(v for v in r.breaks if "manual" in v.title.lower())
    assert not v.auto_fixable


def test_root_validator_manual_break(tmp_path):
    r = analyze_code(tmp_path, """
        from pydantic import BaseModel, root_validator
        class M(BaseModel):
            x: int
            @root_validator(pre=True)
            def rv(cls, values): return values
    """)
    assert any("model_validator" in v.title for v in r.breaks)


def test_basesettings_break(tmp_path):
    r = analyze_code(tmp_path, "from pydantic import BaseSettings\n")
    v = r.breaks[0]
    assert "pydantic-settings" in v.detail and v.auto_fixable


def test_generic_model(tmp_path):
    r = analyze_code(tmp_path, """
        from typing import Generic, TypeVar
        from pydantic.generics import GenericModel
        T = TypeVar("T")
        class Envelope(GenericModel, Generic[T]):
            data: T
    """)
    assert any("GenericModel" in v.title for v in r.all_verdicts)
    assert r.breaks  # class-level finding is a break


def test_config_class(tmp_path):
    r = analyze_code(tmp_path, """
        from pydantic import BaseModel
        class M(BaseModel):
            x: int
            class Config:
                orm_mode = True
    """)
    v = next(v for v in r.all_verdicts if "model_config" in v.title)
    assert v.auto_fixable


def test_removed_config_key_is_break(tmp_path):
    r = analyze_code(tmp_path, """
        from pydantic import BaseModel
        class M(BaseModel):
            x: int
            class Config:
                allow_mutation = False
    """)
    assert any("allow_mutation" in v.title for v in r.breaks)


def test_fields_dunder(tmp_path):
    r = analyze_code(tmp_path, """
        from pydantic import BaseModel
        class M(BaseModel):
            x: int
        print(M.__fields__)
    """)
    v = next(v for v in r.all_verdicts if "model_fields" in v.title)
    assert v.auto_fixable


def test_const_is_break(tmp_path):
    r = analyze_code(tmp_path, """
        from pydantic import BaseModel, Field
        class M(BaseModel):
            kind: str = Field("a", const=True)
    """)
    assert any("const" in v.title for v in r.breaks)


def test_unaffected_counted(tmp_path):
    r = analyze_code(tmp_path, """
        from pydantic import BaseModel
        class M(BaseModel):
            x: int
        m = M(x=1)
    """)
    # class decl + constructor usage are untouched library touches
    assert r.unaffected_count >= 1


def test_pydantic_v1_compat_skipped(tmp_path):
    r = analyze_code(tmp_path, """
        from pydantic.v1 import BaseModel
        class M(BaseModel):
            x: int
        M(x=1).dict()
    """)
    assert r.all_verdicts == []


def test_no_pydantic_no_findings(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    r = analyze(tmp_path, "pydantic1-to-2")
    assert r.all_verdicts == []


def test_citation_line_matches_changelog(tmp_path):
    r = analyze_code(tmp_path, """
        from pydantic import BaseModel
        class M(BaseModel):
            x: int
        M(x=1).parse_obj({"x": 1})
    """)
    v = next(v for v in r.all_verdicts if "parse_obj" in v.title or "model_validate" in v.title)
    c = v.citations[0]
    from breakbot.changelog import load_doc
    doc = load_doc("pydantic-1-to-2")
    assert "parse_obj" in doc.line(c.line) or "model_validate" in doc.line(c.line)
