"""Shared utilities for tools modules."""


def hero_name(hero_id: int, heroes: dict) -> str:
    """Return localized hero name, falling back to str(hero_id)."""
    return heroes.get(str(hero_id), {}).get("localized_name", str(hero_id))
