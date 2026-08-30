"""SQLAlchemy 1.4-era data access layer."""
from sqlalchemy import Column, Integer, String, create_engine, select
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

Base = declarative_base()


class UserRow(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    name = Column(String(50))
    email = Column(String(120))


engine = create_engine("sqlite://")
Session = sessionmaker(bind=engine)


def init_db(engine=engine) -> None:
    engine.execute("CREATE TABLE IF NOT EXISTS users ("
                   "id INTEGER PRIMARY KEY, name VARCHAR(50), "
                   "email VARCHAR(120))")
    engine.execute("INSERT INTO users (id, name, email) "
                   "VALUES (1, 'alice', 'alice@example.com')")


def fetch_user(session, uid: int):
    return session.query(UserRow).get(uid)


def list_user_ids(session):
    rows = session.execute("SELECT id FROM users")
    return [r[0] for r in rows]


def all_emails(session):
    result = session.execute(select([UserRow.email]))
    return [r[0] for r in result]
