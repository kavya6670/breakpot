"""Detection verdicts for the SQLAlchemy 1.4→2.0 upgrade."""
import textwrap

from breakbot.engine import analyze
from breakbot.models import Severity


def analyze_code(tmp_path, code, name="db.py"):
    (tmp_path / name).write_text(textwrap.dedent(code))
    return analyze(tmp_path, "sqlalchemy1.4-to-2.0")


def test_engine_execute_break(tmp_path):
    r = analyze_code(tmp_path, """
        from sqlalchemy import create_engine
        engine = create_engine("sqlite://")
        def f():
            engine.execute("SELECT 1")
    """)
    v = r.breaks[0]
    assert "Engine.execute" in v.title and v.auto_fixable
    assert v.citations and v.citations[0].line > 0


def test_metadata_bind_break(tmp_path):
    r = analyze_code(tmp_path, """
        from sqlalchemy import create_engine, MetaData
        engine = create_engine("sqlite://")
        m = MetaData(bind=engine)
    """)
    assert any("MetaData" in v.title for v in r.breaks)


def test_raw_string_execute(tmp_path):
    r = analyze_code(tmp_path, """
        from sqlalchemy import create_engine
        engine = create_engine("sqlite://")
        with engine.connect() as conn:
            conn.execute("SELECT id FROM t")
    """)
    assert any("Raw SQL string" in v.title for v in r.breaks)


def test_legacy_select(tmp_path):
    r = analyze_code(tmp_path, """
        from sqlalchemy import select, column, table
        t = table("t", column("id"))
        def f():
            return select([t.c.id])
    """)
    assert any("select" in v.title for v in r.breaks)


def test_query_get_warning_fixable(tmp_path):
    r = analyze_code(tmp_path, """
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.orm import declarative_base
        from sqlalchemy import Column, Integer, create_engine
        Base = declarative_base()
        class User(Base):
            __tablename__ = "u"
            id = Column(Integer, primary_key=True)
        Session = sessionmaker(bind=create_engine("sqlite://"))
        def f(session):
            return session.query(User).get(1)
    """)
    v = next(v for v in r.warnings if "Session.get" in v.title)
    assert v.auto_fixable and "session.get" in v.fix_summary


def test_autocommit_break(tmp_path):
    r = analyze_code(tmp_path, """
        from sqlalchemy.orm import sessionmaker
        S = sessionmaker(autocommit=True)
    """)
    assert any("autocommit" in v.title for v in r.breaks)


def test_declarative_import_warning(tmp_path):
    r = analyze_code(tmp_path, """
        from sqlalchemy.ext.declarative import declarative_base
        Base = declarative_base()
    """)
    v = next(v for v in r.warnings if "declarative_base" in v.title)
    assert v.auto_fixable and "sqlalchemy.orm" in v.fix_summary


def test_legacy_query_warning(tmp_path):
    r = analyze_code(tmp_path, """
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.orm import declarative_base
        from sqlalchemy import Column, Integer, create_engine
        Base = declarative_base()
        class User(Base):
            __tablename__ = "u"
            id = Column(Integer, primary_key=True)
        Session = sessionmaker(bind=create_engine("sqlite://"))
        def f(session):
            return session.query(User).all()
    """)
    assert any("Legacy Query" in v.title for v in r.warnings)
