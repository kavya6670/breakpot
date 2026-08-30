from app.api import (
    create_user,
    describe,
    field_names,
    get_user_dict,
    get_user_json,
    parse_user,
    rebuild,
    schema,
    user_envelope,
)
from app.config import get_settings


class FakeRow:
    id = 42
    name = "Helen"
    email = "helen@example.com"


def test_create_user():
    u = create_user({"name": "Ivan", "email": "ivan@example.com"})
    assert u.name == "Ivan"


def test_get_user_json_contains_email():
    assert "helen@example.com" in get_user_json(FakeRow())


def test_get_user_dict():
    d = get_user_dict(FakeRow())
    assert d["id"] == 42 and d["name"] == "Helen"


def test_parse_user():
    u = parse_user('{"id": 11, "name": "Judy", "email": "j@example.com"}')
    assert u.id == 11


def test_envelope_wraps_user():
    env = user_envelope(FakeRow())
    assert env.data.email == "helen@example.com"


def test_field_names_and_schema():
    assert "email" in field_names()
    assert schema()["title"] == "User"
    rebuild()  # should not raise


def test_describe_and_settings():
    info = describe()
    assert info["user_template"]["email"] == "t@example.com"
    assert info["copied"]["id"] == 1
    s = get_settings()
    assert s.app_name == "breakbot-demo"
