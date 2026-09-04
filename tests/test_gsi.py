"""GSI payload parsing + ingest/state endpoints (no Dota client needed)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if not (ROOT / ".tmp" / "heroes.json").exists():
    pytest.skip("Stratz cache missing", allow_module_level=True)

import app as app_module  # noqa: E402
from app import _parse_gsi_payload  # noqa: E402


def _draft_payload(match="123", radiant_picks=(8, 11), dire_picks=(1,), bans=(2, 3), team="dire", active=3, picking=True):
    def block(picks, bans_):
        d = {}
        for i, h in enumerate(picks):
            d[f"pick{i}_id"] = h; d[f"pick{i}_class"] = f"hero_{h}"
        for i in range(len(picks), 5):
            d[f"pick{i}_id"] = 0
        for i, h in enumerate(bans_):
            d[f"ban{i}_id"] = h
        return d
    return {
        "provider": {"name": "dota2"},
        "map": {"matchid": match, "game_state": "DOTA_GAMERULES_STATE_HERO_SELECTION"},
        "player": {"team_name": team},
        "draft": {
            "activeteam": active, "pick": picking,
            "team2": block(radiant_picks, bans[:1]),
            "team3": block(dire_picks, bans[1:]),
        },
    }


def test_parse_draft_block():
    u = _parse_gsi_payload(_draft_payload())
    assert u["radiant"] == [8, 11] and u["dire"] == [1]
    assert sorted(u["bans"]) == [2, 3]
    assert u["my_team"] == "dire" and u["active_team"] == "dire" and u["picking"] is True
    assert u["match_id"] == "123"


def test_parse_ignores_zero_and_garbage():
    p = _draft_payload(radiant_picks=(), dire_picks=())
    p["draft"]["team2"]["pick0_id"] = "x"
    u = _parse_gsi_payload(p)
    assert u["radiant"] == [] and u["dire"] == []


def test_parse_in_game_uses_own_hero():
    u = _parse_gsi_payload({"map": {"matchid": "9", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
                            "player": {"team_name": "radiant"}, "hero": {"id": 14}})
    assert u["_own_hero"] == ("radiant", 14) and "radiant" not in u


def test_ingest_and_state_roundtrip():
    from fastapi.testclient import TestClient
    token = app_module._gsi_token()
    with TestClient(app_module.app) as c:
        # TestClient host is "testclient" → local-only endpoints refuse
        assert c.post("/api/gsi", json={}).status_code == 403
    # Call the handlers' pure parts directly: simulate the lock/state update path
    with app_module._gsi_lock:
        app_module._gsi_state.update(radiant=[], dire=[], bans=[], match_id=None)
    upd = _parse_gsi_payload(_draft_payload(match="777"))
    with app_module._gsi_lock:
        if upd.get("match_id") != app_module._gsi_state.get("match_id"):
            app_module._gsi_state.update(radiant=[], dire=[], bans=[])
        app_module._gsi_state.update(upd)
    assert app_module._gsi_state["radiant"] == [8, 11] and app_module._gsi_state["match_id"] == "777"
    assert token and len(token) >= 16


def test_cfg_text_contains_token_and_port():
    txt = app_module._gsi_cfg_text(8123)
    assert "http://127.0.0.1:8123/api/gsi" in txt
    assert app_module._gsi_token() in txt
    assert '"draft"' in txt


def test_parse_spectator_shape():
    u = _parse_gsi_payload({
        "map": {"matchid": "5", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
        "player": {},
        "hero": {"team2": {"player0": {"id": 8}, "player1": {"id": 11}}, "team3": {"player0": {"id": 1}}},
    })
    assert u["radiant"] == [8, 11] and u["dire"] == [1] and "_own_hero" not in u


def test_player_payload_is_partial_with_own_hero():
    u = _parse_gsi_payload({"map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_STRATEGY_TIME"},
                            "player": {"team_name": "dire"}, "hero": {"id": 108}, "draft": {}})
    assert "radiant" not in u and u["_own_hero"] == ("dire", 108) and u["my_team"] == "dire"
