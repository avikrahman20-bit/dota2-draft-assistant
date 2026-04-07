# Deployment, Rollback & Operations

---

## Pre-Deploy Checklist

- [ ] `.env` exists with valid `STRATZ_API_KEY` and `ANTHROPIC_API_KEY`
- [ ] `python -m pytest tests/ -v` passes
- [ ] Server starts cleanly: `python app.py` — no import errors
- [ ] Cache loads fully (progress bar reaches 100%, app unlocks)
- [ ] `/api/status` returns `{"ready": true}` in browser
- [ ] Auth: register a test user, log in, save profile, log out
- [ ] Draft: add 3 picks each side — recommendations appear
- [ ] Chat: send one message — AI replies (confirms Anthropic key works)
- [ ] Stratz link: enter a real Friend ID — stats populate
- [ ] `.tmp/` populated with hero + matchup JSON files
- [ ] Commit all changes before going live: `git add -A && git commit`

---

## Deployment (Local / LAN)

This app is designed for **local-only use** on `127.0.0.1:8000`.

```bash
python app.py
```

To expose on LAN (e.g. for a friend to connect):
- Change `uvicorn.run(app, host="127.0.0.1", ...)` → `host="0.0.0.0"` in `app.py`
- Open firewall port 8000
- **WARNING**: This removes the localhost-only restriction on `/api/refresh`. Add auth before doing this.

---

## Post-Deploy Verification

After starting the server, verify:

1. `http://127.0.0.1:8000` loads splash screen
2. Progress bar animates and app unlocks within ~3 min on first run (instant on subsequent runs)
3. Hero grid shows 120+ heroes
4. Adding 1+ enemy picks shows recommendations
5. Chat responds to "who counters Axe?"
6. No ERROR lines in terminal output

---

## Rollback Checklist

If something breaks after a code change:

```bash
# 1. Find the last good commit
git log --oneline -10

# 2. Hard-reset to it (WARNING: discards uncommitted changes)
git checkout <commit-hash>

# 3. If dependencies changed
pip install -r requirements.txt

# 4. Restart server
python app.py
```

**Database rollback** (`users.db`): SQLite — back up by copying the file.
```bash
cp users.db users.db.bak    # before risky migration
cp users.db.bak users.db    # to restore
```

**Cache rollback** (`.tmp/`): Delete to force a fresh Stratz fetch.
```bash
# Windows
rmdir /s /q .tmp
```

---

## Known Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Stratz API down | Medium | Cache load fails on first run | `.tmp/` cache persists; only affects fresh installs or forced refresh |
| Stratz API key revoked | Low | App starts but matchup data stale | Re-generate key at stratz.com/api |
| Anthropic API key exhausted | Low | Chat returns 429 | UI shows clear error; rest of app still works |
| Port 8000 already in use | High (dev) | Server fails to start | Kill existing process; error is logged |
| `users.db` corruption | Very Low | Auth unavailable | Delete `users.db` to reset (loses all accounts) |
| JWT_SECRET rotated | Low | All existing sessions invalidated | Don't change `JWT_SECRET` unless intentional; users must re-login |
| Patch after cache load | Medium | Recommendations slightly stale | Use Refresh Data button or wait for next startup |
| Hero pool/weights in localStorage cleared | Low | User settings lost | Only affects frontend prefs; profile (roles, pool, notes) is server-side |

---

## Maintenance

**Refresh Stratz data:**
- Automatic: delete `.tmp/` and restart
- Manual (server running): `POST http://127.0.0.1:8000/api/refresh` (localhost only)

**View logs:**
- Server output in terminal
- All API requests logged: `METHOD /api/path → STATUS (Xs)`
- Warnings on 4xx/5xx and rate limit hits

**Database:**
- Location: `users.db` in project root
- Auto-migrates schema on startup — no manual migrations needed
- Back up before any schema changes

**Dependency updates:**
```bash
pip install --upgrade -r requirements.txt
```
Upper bounds in `requirements.txt` protect against breaking major versions. Test after upgrading.
