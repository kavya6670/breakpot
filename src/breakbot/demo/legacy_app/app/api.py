"""User API operations — exercises v1 model methods all over the call sites."""
from typing import Optional

from .models import Envelope, User, UserCreate
from .config import get_settings


def create_user(payload: dict) -> User:
    data = UserCreate.parse_obj(payload)
    user = User(id=0, name=data.name, email=data.email)
    return user


def get_user_json(orm_row) -> str:
    user = User.from_orm(orm_row)
    return user.json()


def get_user_dict(orm_row) -> dict:
    user = User.from_orm(orm_row)
    return user.dict()


def parse_user(blob: str) -> User:
    return User.parse_raw(blob)


def user_envelope(orm_row) -> Envelope:
    user = User.from_orm(orm_row)
    return Envelope(data=user)


def field_names() -> list:
    return list(User.__fields__.keys())


def schema() -> dict:
    return User.schema()


def rebuild() -> None:
    User.update_forward_refs()


def describe() -> dict:
    settings = get_settings()
    user = User.construct(id=1, name="template", email="t@example.com")
    return {
        "app": settings.app_name,
        "user_template": user.dict(),
        "copied": user.copy().dict(),
        "fields": field_names(),
    }
