"""
Fetches and caches the latest Dota 2 patch notes from the official datafeed.
Cache TTL: 1 hour. Falls back to empty string on any failure.
"""

import json
import time
from pathlib import Path

_CACHE_FILE = Path(__file__).parent.parent / ".tmp" / "patch_notes_cache.json"
_CACHE_TTL  = 3600  # seconds

_LIST_URL  = "https://www.dota2.com/datafeed/patchnoteslist?language=english"
_NOTES_URL = "https://www.dota2.com/datafeed/patchnotes?version={version}&language=english"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


def _http_get(url: str) -> dict | None:
    try:
        from curl_cffi import requests as cffi_req
        r = cffi_req.get(url, headers=_HEADERS, impersonate="chrome110", timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        pass
    try:
        import urllib.request
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _load_hero_names() -> dict[int, str]:
    """Load hero_id → localized_name from cached heroes.json."""
    heroes_path = Path(__file__).parent.parent / ".tmp" / "heroes.json"
    if heroes_path.exists():
        try:
            data = json.loads(heroes_path.read_text(encoding="utf-8"))
            return {int(k): v["localized_name"] for k, v in data.items()}
        except Exception:
            pass
    return {}


def _parse_notes(data: dict) -> str:
    """Convert the Dota 2 patch notes JSON into a readable string."""
    hero_names = _load_hero_names()
    lines = []
    patch_name = data.get("patch_name", "")
    if patch_name:
        lines.append(f"=== Dota 2 Patch {patch_name} ===\n")

    # Generic / general changes
    generic = data.get("generic", [])
    if generic:
        lines.append("GENERAL CHANGES:")
        for item in generic[:25]:
            note = item.get("note", "") if isinstance(item, dict) else str(item)
            if note:
                lines.append(f"  • {note}")
        lines.append("")

    # Hero changes (API schema: hero_id, hero_notes, abilities)
    heroes = data.get("heroes", [])
    if heroes:
        lines.append("HERO CHANGES:")
        for hero in heroes:
            hero_id = hero.get("hero_id", 0)
            name = hero_names.get(hero_id, f"Hero {hero_id}")

            all_notes = []
            for n in hero.get("hero_notes", []):
                note = n.get("note", "") if isinstance(n, dict) else str(n)
                if note:
                    all_notes.append(note)
            for ab in hero.get("abilities", []):
                for n in ab.get("ability_notes", []):
                    note = n.get("note", "") if isinstance(n, dict) else str(n)
                    if note:
                        all_notes.append(note)
            for n in hero.get("talent_notes", []):
                note = n.get("note", "") if isinstance(n, dict) else str(n)
                if note:
                    all_notes.append(f"[Talent] {note}")

            if all_notes:
                lines.append(f"  {name}:")
                for note in all_notes:
                    lines.append(f"    • {note}")
        lines.append("")

    # Item changes (API schema: ability_id, ability_notes)
    items = data.get("items", [])
    if items:
        lines.append("ITEM CHANGES:")
        for item in items:
            for n in item.get("ability_notes", []):
                note = n.get("note", "") if isinstance(n, dict) else str(n)
                if note:
                    lines.append(f"  • {note}")
        lines.append("")

    # Neutral item changes
    neutrals = data.get("neutral_items", [])
    if neutrals:
        lines.append("NEUTRAL ITEM CHANGES:")
        for ni in neutrals:
            title = ni.get("title", "")
            if title:
                lines.append(f"  {title}:")
            for n in ni.get("ability_notes", []):
                note = n.get("note", "") if isinstance(n, dict) else str(n)
                if note:
                    lines.append(f"  • {note}")
        lines.append("")

    return "\n".join(lines)


def get_patch_notes() -> str:
    """
    Returns the latest Dota 2 patch notes as a plain-text string.
    Result is cached for 1 hour in .tmp/patch_notes_cache.json.
    """
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Serve from cache if fresh
    if _CACHE_FILE.exists():
        try:
            cached = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            if time.time() - cached.get("timestamp", 0) < _CACHE_TTL:
                return cached["content"]
        except Exception:
            pass

    # Fetch patch list to find latest version
    patch_list = _http_get(_LIST_URL)
    if not patch_list:
        return ""

    patches = patch_list.get("patches") or patch_list.get("results") or []
    if not patches:
        return ""

    # Latest patch is typically last in the list
    latest = patches[-1]
    version = latest.get("patch_name") or latest.get("version") or ""
    if not version:
        return ""

    # Fetch the actual patch notes
    notes_data = _http_get(_NOTES_URL.format(version=version))
    if not notes_data:
        return ""

    content = _parse_notes(notes_data)

    # Save to cache
    try:
        _CACHE_FILE.write_text(
            json.dumps({"timestamp": time.time(), "content": content}),
            encoding="utf-8",
        )
    except Exception:
        pass

    return content
