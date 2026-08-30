"""Application settings — pydantic v1 BaseSettings style."""
from pydantic import BaseSettings, Field


class Settings(BaseSettings):
    app_name: str = "breakbot-demo"
    debug: bool = Field(False, env="DEBUG")
    max_items: int = 50

    class Config:
        env_prefix = "APP_"
        env_file = ".env"
        case_sensitive = False


def get_settings() -> Settings:
    return Settings()
