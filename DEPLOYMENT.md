# Deployment & Operations

## Railway (production)

- Service `dota2-draft-assistant` in project `optimistic-nourishment`, linked to GitHub `avikrahman20-bit/dota2-draft-assistant`.
- **Every push to `master` auto-deploys.** Check with `railway status` / `railway deployment list`.
- URL: https://dota2-draft-assistant-production.up.railway.app
- Env vars set in Railway: `STRATZ_API_KEY`, `ANTHROPIC_API_KEY`, `JWT_SECRET`, `RAILWAY_ENVIRONMENT` (auto). `PORT` is injected.
- The committed `.tmp/` cache (hero list, stats, role map, `matchups_bundle.json`) means a deploy starts serving in seconds. The server unpacks the bundle into `.tmp/matchups_stratz/` on first start and refreshes from Stratz in the background once the data is older than 24 h (`.tmp/last_fetch.json` carries the true fetch time across checkouts).
- The container filesystem is ephemeral: `users.db` and refreshed cache files are lost on redeploy unless a volume is attached. Attach a Railway volume at the project root (or set `DB_PATH`) before relying on accounts in production.
- `/api/refresh` is refused off-localhost; the UI hides the button.

## Pre-deploy checklist

- [ ] `python -m pytest tests/ -v` passes (scoring + API)
- [ ] `python tests/browser_smoke.py` passes against a local server
- [ ] `git status` shows only intended changes (cache refreshes touch `.tmp/matchups_bundle.json` and `.tmp/last_fetch.json`)
- [ ] Commit, push — Railway builds automatically

## Post-deploy verification

1. `curl <url>/api/status` → `"ready": true` within ~30 s
2. Open the site: board renders, add one pick each side → recommendations and Threats tab populate
3. Log in, send one chat message → streamed reply

## Local / LAN

`python app.py` binds `127.0.0.1:8000`. `launch.bat` does the same from a double-click. `launch.ps1` also opens a Cloudflare quick tunnel for sharing.

## Rollback

```bash
git log --oneline -10
git revert <bad-commit>       # preferred: keeps history, Railway redeploys
# or
git reset --hard <good-commit> && git push --force origin master
```

Database: `users.db` is SQLite — copy the file to back up, copy it back to restore.
Cache: delete `.tmp/matchups_stratz/` (or the whole `.tmp/`) to force a fresh Stratz fetch; the bundle is regenerated after the next fetch.

## Known risks

| Risk | Impact | Mitigation |
|---|---|---|
| Stratz API down | First-run fetch fails; refresh skipped | Committed bundle keeps serving; auto-refresh retries every 30 min |
| Stratz key revoked | Data goes stale | Regenerate at stratz.com/api-token |
| Anthropic key missing/exhausted | Chat disabled / 429 | Drafting unaffected; UI says so |
| Railway redeploy | `users.db` wiped without a volume | Attach a volume |
| JWT_SECRET changed | All sessions invalid | Users log in again |
