"""Patch generation: the auto-fixes must produce code that actually works
under pydantic v2 / SQLAlchemy 2.0."""
import os
import subprocess
import sys
import textwrap

import pytest

from breakbot.codemod import apply_fixes, build_patched_source
from breakbot.ingest import ingest_repo


def patch_source(code: str, upgrade: str) -> str:
    import tempfile, pathlib
    d = tempfile.mkdtemp()
    p = pathlib.Path(d) / "m.py"
    p.write_text(textwrap.dedent(code))
    mod = ingest_repo(pathlib.Path(d))[0]
    new, edits, notes = build_patched_source(mod, upgrade)
    assert not notes, notes
    assert new is not None, "no patch produced"
    return new


def run_module(code: str, prelude: str = "") -> str:
    """Execute *code* in a fresh subprocess using the venv interpreter."""
    with __import__("tempfile").TemporaryDirectory() as d:
        path = os.path.join(d, "m.py")
        with open(path, "w") as f:
            f.write(textwrap.dedent(code))
        runner = os.path.join(d, "run.py")
        with open(runner, "w") as f:
            f.write(prelude + "\nimport m\nprint('OK')\n")
        proc = subprocess.run([sys.executable, runner], cwd=d,
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout


# --------------------------------------------------------------------------- #
# pydantic
# --------------------------------------------------------------------------- #

def test_patch_model_methods_runs_on_v2():
    new = patch_source('''
        from pydantic import BaseModel
        class M(BaseModel):
            x: int
        m = M(x=1)
        a = m.dict()
        b = M.parse_raw('{"x": 2}')
        c = M(x=3).json()
        d = M.schema()
        e = M.construct(x=4)
        f = m.copy()
        g = M.__fields__
        M.update_forward_refs()
    ''', "pydantic1-to-2")
    assert "model_dump" in new and "model_validate_json" in new
    assert "model_json_schema" in new and "model_construct" in new
    assert "model_copy" in new and "model_fields" in new and "model_rebuild" in new
    assert ".dict(" not in new and ".parse_raw(" not in new and ".__fields__" not in new
    run_module(new)


def test_patch_field_regex_runs_on_v2():
    new = patch_source('''
        from pydantic import BaseModel, Field
        class M(BaseModel):
            email: str = Field(..., regex=r"[^@]+@[^@]+")
            tags: list = Field([], min_items=1, max_items=5)
        m = M(email="a@b.c", tags=["x"])
    ''', "pydantic1-to-2")
    assert "pattern=" in new and "min_length=" in new and "max_length=" in new
    run_module(new)


def test_patch_config_class_runs_on_v2():
    new = patch_source('''
        from pydantic import BaseModel
        class M(BaseModel):
            x: int
            class Config:
                orm_mode = True
                allow_population_by_field_name = True
        class Row:
            x = 5
        m = M.from_orm(Row())
    ''', "pydantic1-to-2")
    assert "model_config = ConfigDict" in new
    assert "from_attributes" in new and "populate_by_name" in new
    assert "class Config" not in new
    run_module(new)


def test_patch_validator_decorator_runs_on_v2():
    new = patch_source('''
        from pydantic import BaseModel, validator
        class M(BaseModel):
            name: str
            @validator("name")
            def v(cls, v):
                return v.strip()
            @validator("name", pre=True)
            def v2(cls, v):
                return str(v).lower()
        assert M(name="  A  ").name == "a"
    ''', "pydantic1-to-2")
    assert "@field_validator" in new and "@classmethod" in new
    assert 'mode="before"' in new
    assert "from pydantic import" in new and "validator" not in new.split("\n")[0]
    # behavior preserved: strip + lowercase
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "m.py")
        open(p, "w").write(new)
        proc = subprocess.run([sys.executable, "-c",
            f"import sys; sys.path.insert(0,'{d}'); import m"],
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


def test_patch_basesettings_import():
    new = patch_source('''
        from pydantic import BaseModel, BaseSettings
        class M(BaseModel):
            x: int = 1
        class Settings(BaseSettings):
            debug: bool = False
        s = Settings()
    ''', "pydantic1-to-2")
    assert "from pydantic_settings import BaseSettings" in new
    run_module(new)


def test_patch_generic_model():
    new = patch_source('''
        from typing import Generic, TypeVar
        from pydantic.generics import GenericModel
        from pydantic import BaseModel
        T = TypeVar("T")
        class Envelope(GenericModel, Generic[T]):
            data: T
        class Inner(BaseModel):
            x: int
        e = Envelope(data=Inner(x=1))
    ''', "pydantic1-to-2")
    assert "class Envelope(BaseModel, Generic[T])" in new
    assert "pydantic.generics" not in new
    run_module(new)


def test_parse_file_uses_path():
    new = patch_source('''
        from pathlib import Path
        import tempfile, json, os
        from pydantic import BaseModel
        class M(BaseModel):
            x: int
        d = tempfile.mkdtemp()
        p = os.path.join(d, "f.json")
        open(p, "w").write(json.dumps({"x": 7}))
        m = M.parse_file(p)
        assert m.x == 7
    ''', "pydantic1-to-2")
    assert "model_validate_json(Path(" in new
    run_module(new)


def test_patch_produces_valid_syntax_for_complex_file():
    code = '''
        from typing import Generic, TypeVar, Optional
        from pydantic import BaseModel, Field, validator, BaseSettings
        from pydantic.generics import GenericModel

        T = TypeVar("T")

        class Envelope(GenericModel, Generic[T]):
            data: Optional[T] = None

        class Settings(BaseSettings):
            debug: bool = False
            class Config:
                env_prefix = "APP_"

        class User(BaseModel):
            id: int
            email: str = Field(..., regex=r"[^@]+@[^@]+")
            class Config:
                orm_mode = True
            @validator("email", pre=True)
            def low(cls, v):
                return v.lower() if isinstance(v, str) else v

        def f():
            return User(id=1, email="A@B.C").dict()
    '''
    new = patch_source(code, "pydantic1-to-2")
    run_module(new)


# --------------------------------------------------------------------------- #
# SQLAlchemy
# --------------------------------------------------------------------------- #

def test_patch_engine_execute_runs_on_2():
    new = patch_source('''
        from sqlalchemy import create_engine
        engine = create_engine("sqlite://")
        def init():
            engine.execute("CREATE TABLE t (id integer)")
            engine.execute("INSERT INTO t (id) VALUES (1)")
        def read():
            with engine.connect() as conn:
                return conn.execute("SELECT id FROM t").fetchall()
        init()
        assert read()[0][0] == 1
    ''', "sqlalchemy1.4-to-2.0")
    assert "with engine.begin()" in new and "text(" in new
    assert "engine.execute(" not in new
    run_module(new)


def test_patch_metadata_bind():
    new = patch_source('''
        from sqlalchemy import create_engine, MetaData
        engine = create_engine("sqlite://")
        metadata = MetaData(bind=engine)
    ''', "sqlalchemy1.4-to-2.0")
    assert "MetaData()" in new and "bind=" not in new
    run_module(new)


def test_patch_legacy_select():
    new = patch_source('''
        from sqlalchemy import select, table, column
        t = table("t", column("id"))
        def f():
            return select([t.c.id])
    ''', "sqlalchemy1.4-to-2.0")
    assert "select(" in new and "select([" not in new
    run_module(new)


def test_patch_query_get():
    new = patch_source('''
        from sqlalchemy import create_engine, Column, Integer
        from sqlalchemy.orm import sessionmaker, declarative_base
        Base = declarative_base()
        class User(Base):
            __tablename__ = "u"
            id = Column(Integer, primary_key=True)
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        S = sessionmaker(bind=engine)
        with S() as session:
            session.get(User, None) if False else None
            session.add(User(id=1))
            session.commit()
        with S() as session:
            u = session.query(User).get(1)
            assert u.id == 1
    ''', "sqlalchemy1.4-to-2.0")
    assert "session.get(User, 1)" in new
    assert ".query(User).get(" not in new
    run_module(new)


def test_patch_autocommit_removed():
    new = patch_source('''
        from sqlalchemy.orm import sessionmaker
        S = sessionmaker(autocommit=True)
    ''', "sqlalchemy1.4-to-2.0")
    assert "autocommit" not in new
    run_module(new)


def test_patch_declarative_import():
    new = patch_source('''
        from sqlalchemy.ext.declarative import declarative_base
        Base = declarative_base()
    ''', "sqlalchemy1.4-to-2.0")
    assert "from sqlalchemy.orm import declarative_base" in new
    assert "ext.declarative" not in new
    run_module(new)


def test_apply_fixes_writes_files_and_diff(tmp_path):
    (tmp_path / "m.py").write_text(textwrap.dedent('''
        from pydantic import BaseModel
        class M(BaseModel):
            x: int
        print(M(x=1).dict())
    '''))
    result = apply_fixes(tmp_path, "pydantic1-to-2")
    assert result.changed_files == ["m.py"]
    assert "model_dump" in result.diff
    assert "model_dump" in (tmp_path / "m.py").read_text()
    assert (tmp_path / ".breakbot-work" / "backup" / "m.py").exists()
