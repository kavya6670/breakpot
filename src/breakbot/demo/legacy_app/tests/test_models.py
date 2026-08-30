import pytest

from app.models import Envelope, User, UserCreate


def test_user_validates_and_cleans():
    u = User(id=1, name="  Alice  ", email="ALICE@EXAMPLE.COM")
    assert u.name == "Alice"          # validator strips
    assert u.email == "alice@example.com"  # validator lowercases (pre)


def test_blank_name_rejected():
    with pytest.raises(Exception):
        User(id=2, name="   ", email="x@example.com")


def test_bad_email_rejected():
    with pytest.raises(Exception):
        User(id=3, name="Bob", email="not-an-email")


def test_parse_raw_roundtrip():
    u = User.parse_raw('{"id": 7, "name": "Carol", "email": "c@example.com"}')
    assert u.id == 7 and u.name == "Carol"


def test_dict_and_json():
    u = User(id=4, name="Dan", email="d@example.com")
    dumped = u.dict()
    assert dumped["email"] == "d@example.com"
    # json()/model_dump_json() emit compact JSON; the email value is present
    assert "d@example.com" in u.json()
    import json
    assert json.loads(u.json())["email"] == "d@example.com"


def test_from_orm():
    class Row:
        id = 9
        name = "Eve"
        email = "eve@example.com"

    u = User.from_orm(Row())
    assert u.id == 9 and u.name == "Eve"


def test_fields_and_schema():
    assert set(User.__fields__.keys()) >= {"id", "name", "email", "tags"}
    schema = User.schema()
    assert schema["title"] == "User"


def test_user_create_strips_whitespace():
    uc = UserCreate(name="  Frank  ", email="frank@example.com")
    assert uc.name == "Frank"


def test_generic_envelope():
    u = User(id=5, name="Grace", email="g@example.com")
    env = Envelope(data=u)
    dumped = env.dict()
    assert dumped["ok"] is True
    assert dumped["data"]["email"] == "g@example.com"
