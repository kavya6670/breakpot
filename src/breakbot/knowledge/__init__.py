"""Registry of supported upgrades."""
from __future__ import annotations

from .base import Finding, Rule, Upgrade
from . import pydantic_v2, sqlalchemy_v2

UPGRADES: dict[str, Upgrade] = {
    u.id: u
    for u in (pydantic_v2.UPGRADE, sqlalchemy_v2.UPGRADE)
}


def get_upgrade(upgrade_id: str) -> Upgrade:
    if upgrade_id not in UPGRADES:
        raise KeyError(
            f"Unknown upgrade {upgrade_id!r}. Available: {', '.join(UPGRADES)}"
        )
    return UPGRADES[upgrade_id]


__all__ = ["UPGRADES", "get_upgrade", "Upgrade", "Rule", "Finding"]
