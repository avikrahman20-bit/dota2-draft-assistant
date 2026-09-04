# Dota 2 Draft Assistant

Real-time Dota 2 drafting tool. Enter picks and bans as they lock; get counter-picks, synergy, threats, a running draft edge, and an AI assistant, all driven by live Stratz matchup data for your bracket.

Live: https://dota2-draft-assistant-production.up.railway.app

---

## Features

- **Draft board** — Radiant/Dire picks plus 14 ban slots. Click an empty slot or press Tab to choose where the next hero goes. Undo (Ctrl+Z), reset, and resume an unfinished draft after a reload.
- **Recommendations** — 0–100 pick score with plain-language reasons (counters, weaknesses, win rate, synergy), position pills, low-data warnings, and an expandable weighted breakdown. "Team still needs" shows uncovered positions.
- **Threats / Enemy Picks / Win% / Lookup** tabs — which enemy heroes hurt you most, what they'll likely pick next, a draft edge from the first pick (full win probability at 5v5), and a lookup for any hero in the current draft.
- **Hero search** — community shorthand (am, sf, qop…), word prefixes ("sky m"), attribute-grouped grid.
- **AI assistant** — Claude, streamed, with the live meta, latest patch notes, your draft, and (if linked) your Stratz match history. 50 messages/day per account.
- **Accounts** — hero pool, roles, playstyle, notes; weights and bracket sync across devices; link a Dota 2 Friend ID / Steam64 / profile URL.
- **Data** — Stratz win rates per bracket and matchup cohorts; refreshed automatically in the background once older than 24 hours.

---

## Requirements

- Python 3.12+
- [Stratz API key](https://stratz.com/api-token) (free)
- [Anthropic API key](https://console.anthropic.com/) — optional; without it drafting works and chat is disabled

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # add STRATZ_API_KEY (and ANTHROPIC_API_KEY if you want chat)
python app.py                 # opens http://127.0.0.1:8000
```

`JWT_SECRET` is generated into `.env` on first run. First start on a machine without `.tmp/` fetches ~128 heroes from Stratz (about a minute). The repo ships a cache bundle so clones start instantly.

---

## Configuration (`.env`)

| Variable | Required | Description |
|---|---|---|
| `STRATZ_API_KEY` | Yes | Stratz GraphQL/REST key |
| `ANTHROPIC_API_KEY` | No | Enables the AI chat |
| `JWT_SECRET` | Auto | Generated on first run; keep it if you move `users.db` |
| `PORT` | No | Defaults to 8000 |

---

## Development

```bash
python app.py                          # run
python -m pytest tests/ -v             # scoring + API tests (API tests use a temp DB)
python tests/browser_smoke.py          # end-to-end in headless Edge; needs the server running
```

Backend change → restart server. Frontend change → Ctrl+Shift+R.

Kill server (Windows): `powershell -Command "Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force"`

---

## Architecture

```
Browser (static SPA)          FastAPI (app.py)                      External
────────────────────          ────────────────                      ────────
index.html                    /api/status, /api/heroes              Stratz GraphQL  (matchups, positions)
app.js  ←──────────────────→  /api/recommend, /api/hero_score       Stratz REST     (heroes, win rates, players)
style.css                     /api/draft_analysis                   Anthropic Claude (chat, streamed)
                              /api/chat, /api/chat/stream, /quota   dota2.com datafeed (patch notes)
                              /api/register, /api/login
                              /api/profile, /api/link_account
                              /api/refresh (localhost only)
```

Key files:
- `app.py` — endpoints, cache load, background auto-refresh, threats
- `tools/scoring_engine.py` — pure scoring (`score_candidates`, `analyze_draft`)
- `tools/assistant.py` — Claude prompt assembly, streaming
- `tools/fetch_*.py` — Stratz fetchers; `.tmp/matchups_bundle.json` is the committed cache
- `database.py`, `auth.py` — SQLite users/profiles, JWT + bcrypt

---

## Scoring

| Component | Default | What it measures |
|---|---|---|
| counter | 0.55 | Shrunk win-rate advantage vs each enemy pick |
| synergy | 0.20 | Co-pick win rate with your allies |
| win_rate | 0.15 | Overall bracket win rate |
| hero_pool | 0.05 | Boost for your saved pool |
| meta | 0.05 | Pick-rate popularity |

Components are normalised to 0–1; inactive ones (no enemies → no counter) have their weight redistributed. Matchups with few games are shrunk toward 50% (k=400). Weights are adjustable in ⚙ Settings.

Win rates use the exact bracket you pick; matchup and synergy data come from Stratz's four combined cohorts (Herald+Guardian, Crusader+Archon, Legend+Ancient, Divine+Immortal).
