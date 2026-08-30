"""Domain models — pydantic v1 style (this is the code BreakBot upgrades)."""
from typing import Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field, validator
from pydantic.generics import GenericModel

T = TypeVar("T")


class Envelope(GenericModel, Generic[T]):
    """Generic API response envelope."""
    ok: bool = True
    data: Optional[T] = None


class User(BaseModel):
    id: int
    name: str
    email: str = Field(..., regex=r"[^@]+@[^@]+\.[^@]+")
    tags: List[str] = Field(default_factory=list, min_items=0, max_items=10)

    class Config:
        orm_mode = True
        allow_population_by_field_name = True

    @validator("name")
    def name_not_blank(cls, v):
        if not v or not v.strip():
            raise ValueError("name must not be blank")
        return v.strip()

    @validator("email", pre=True)
    def lower_email(cls, v):
        return v.lower() if isinstance(v, str) else v


class UserCreate(BaseModel):
    name: str
    email: str = Field(..., regex=r"[^@]+@[^@]+\.[^@]+")

    class Config:
        anystr_strip_whitespace = True
