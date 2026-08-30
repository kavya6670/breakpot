"""Bundled demo target: a small pydantic-v1-era app + pytest suite.

The app is copied to a working directory by `breakbot demo` / the web UI so
the original (frozen, legacy) copy always stays pristine in the package.
"""
from __future__ import annotations

from pathlib import Path


def legacy_app_path() -> Path:
    return Path(__file__).parent / "legacy_app"
