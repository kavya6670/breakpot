from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import UserRow, all_emails, fetch_user, init_db, list_user_ids


def _fresh_session():
    engine = create_engine("sqlite://")
    UserRow.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return engine, Session()


def test_init_and_fetch():
    engine = create_engine("sqlite://")
    init_db(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        row = fetch_user(session, 1)
        assert row is not None and row.name == "alice"


def test_list_user_ids():
    engine, session = _fresh_session()
    engine.execute("INSERT INTO users (id, name, email) VALUES (2, 'bob', 'b@x.com')")
    assert 2 in list_user_ids(session)


def test_all_emails():
    engine, session = _fresh_session()
    engine.execute("INSERT INTO users (id, name, email) VALUES (3, 'cy', 'c@x.com')")
    assert "c@x.com" in all_emails(session)
