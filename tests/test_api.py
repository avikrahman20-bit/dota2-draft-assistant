"""
API smoke tests via FastAPI TestClient.
Needs the Stratz cache in .tmp/ (present in the repo) and STRATZ_API_KEY in .env.
Uses a temporary SQLite DB so real accounts are untouched. No Anthropic calls.
"""
import os
import sys
import time
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if not (ROOT / ".tmp" / "heroes.json").exists():
    pytest.skip("Stratz cache missing (.tmp/heroes.json)", allow_module_level=True)

from fastapi.testclient import TestClient  # noqa: E402

import database as db  # noqa: E402
import app as app_module  # noqa: E402


@pytest.fixture(scope="module")
def client():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db.DB_PATH = tmp.name
    db.init_db()
    with TestClient(app_module.app) as c:
        # lifespan kicks off the cache load in a thread; wait for it
        for _ in range(600):
            if c.get("/api/status").json().get("ready"):
                break
            time.sleep(0.1)
        assert c.get("/api/status").json()["ready"], "cache never became ready"
        yield c
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


def _ids(client, n):
    heroes = client.get("/api/heroes").json()
    return [int(k) for k in list(heroes)[:n]]


def test_status_fields(client):
    s = client.get("/api/status").json()
    for k in ("ready", "can_refresh", "chat_enabled", "data_updated_at", "patch_name"):
        assert k in s


def test_heroes_have_positions_and_attr(client):
    heroes = client.get("/api/heroes").json()
    assert len(heroes) > 100
    h = next(iter(heroes.values()))
    assert "positions" in h and "primary_attr" in h and "img_url" in h


def test_recommend_excludes_picked_and_banned(client):
    ids = _ids(client, 6)
    ally, enemy, bans = ids[:1], ids[1:3], ids[3:5]
    r = client.post("/api/recommend", json={
        "ally_picks": ally, "enemy_picks": enemy, "bans": bans, "my_team": "radiant", "mmr_bracket": "7",
    })
    assert r.status_code == 200, r.text
    d = r.json()
    top_ids = [x["hero_id"] for x in d["top"]]
    assert len(top_ids) == 20
    assert not set(top_ids) & set(ally + enemy + bans)
    assert all(0 <= x["total_score"] <= 1 for x in d["top"])
    assert {t["enemy_id"] for t in d["threats"]} == set(enemy)
    assert d["enemy_predictions"] and not set(x["hero_id"] for x in d["enemy_predictions"]) & set(ally + enemy + bans)


def test_recommend_rejects_unknown_hero(client):
    r = client.post("/api/recommend", json={"ally_picks": [999999], "enemy_picks": []})
    assert r.status_code == 400


def test_recommend_role_filter(client):
    r = client.post("/api/recommend", json={"ally_picks": [], "enemy_picks": [], "role_filter": "carry"})
    assert r.status_code == 200
    heroes = client.get("/api/heroes").json()
    for x in r.json()["top"]:
        assert "carry" in heroes[str(x["hero_id"])]["positions"]


def test_hero_score_matches_recommend_scale(client):
    ids = _ids(client, 3)
    r = client.post("/api/hero_score", json={"hero_id": ids[0], "ally_picks": [], "enemy_picks": ids[1:3]})
    assert r.status_code == 200
    d = r.json()
    assert d["hero_id"] == ids[0] and "breakdown" in d and 0 <= d["total_score"] <= 1


def test_draft_analysis_partial_and_complete(client):
    ids = _ids(client, 10)
    partial = client.post("/api/draft_analysis", json={"radiant": ids[:1], "dire": ids[5:6]})
    assert partial.status_code == 200 and partial.json()["complete"] is False and partial.json()["picks"] == 2
    full = client.post("/api/draft_analysis", json={"radiant": ids[:5], "dire": ids[5:10]})
    assert full.status_code == 200
    d = full.json()
    assert d["complete"] is True
    assert abs(d["radiant_win_prob"] + d["dire_win_prob"] - 100) < 0.11
    assert len(d["key_matchups"]["radiant_best"]) == 3 and len(d["synergies"]["radiant_best"]) == 2
    assert client.post("/api/draft_analysis", json={"radiant": [], "dire": ids[:1]}).status_code == 400


def test_auth_profile_roundtrip(client):
    reg = client.post("/api/register", json={"username": "apitest_user", "password": "password123"})
    assert reg.status_code == 200, reg.text
    token = reg.json()["token"]
    h = {"Authorization": f"Bearer {token}"}

    assert client.post("/api/register", json={"username": "apitest_user", "password": "password123"}).status_code == 409
    assert client.post("/api/login", json={"username": "apitest_user", "password": "wrong-pass"}).status_code == 401
    assert client.get("/api/profile").status_code == 401

    ids = _ids(client, 2)
    put = client.put("/api/profile", headers=h, json={
        "preferred_roles": ["mid"], "hero_pool": ids, "custom_weights": {"counter": 0.7}, "mmr_bracket": "4",
    })
    assert put.status_code == 200
    p = client.get("/api/profile", headers=h).json()
    assert p["preferred_roles"] == ["mid"] and p["hero_pool"] == ids
    assert p["custom_weights"] == {"counter": 0.7} and p["mmr_bracket"] == "4"

    # pool heroes get the in_hero_pool flag in recommendations
    rec = client.post("/api/recommend", headers=h, json={"ally_picks": [], "enemy_picks": []}).json()
    flagged = {x["hero_id"] for x in rec["top"] if x["in_hero_pool"]}
    assert flagged <= set(ids)

    q = client.get("/api/chat/quota", headers=h)
    assert q.status_code == 200 and q.json()["remaining"] == 50


def test_chat_requires_login(client):
    assert client.post("/api/chat", json={"question": "hi"}).status_code in (401, 503)
    assert client.post("/api/chat/stream", json={"question": "hi"}).status_code in (401, 503)


def test_refresh_localhost_only_flag(client):
    # TestClient reports a non-loopback host, so refresh must be refused
    r = client.post("/api/refresh", json={"force": False})
    assert r.status_code in (403, 503)
