# Dota 2 Draft Assistant

Real-time drafting tool that gives counter-pick recommendations, synergy analysis, win probability, and AI-powered draft advice — all backed by live Stratz data.

## Architecture

```
Browser (SPA)          FastAPI Backend (app.py)         External APIs
─────────────          ──────────────────────           ─────────────
index.html             /api/status                      Stratz GraphQL
app.js        ←───→    /api/heroes                        (matchups, win rates)
style.css              /api/recommend                   Stratz REST
                       /api/draft_analysis                (hero list, stats)
                       /api/chat                        Anthropic Claude
                       /api/refresh                       (chat assistant)
                                                        Dota 2 Datafeed
                                                          (patch notes)
```

## Running

```bash
python app.py
# Opens http://127.0.0.1:8000
# First run caches hero data (~2-3 min). Subsequent runs are instant.
```

Python: `C:\Users\Khan Gadget\AppData\Local\Programs\Python\Python312\python.exe`

## File Structure

```
app.py                  # FastAPI server — all API endpoints
static/
  index.html            # Single-page app shell
  app.js                # Frontend state, rendering, API calls
  style.css             # Dark theme, all component styles
tools/
  fetch_hero_data.py    # Stratz REST: hero list + bracket win rates
  fetch_matchups.py     # Stratz GraphQL: vs (counter) + with (synergy) matchup data
  scoring_engine.py     # Pure scoring: counter, win rate, synergy → recommendations + draft analysis
  assistant.py          # Claude-powered chat: builds context from live data + patch notes
  patch_notes.py        # Fetches latest patch notes from dota2.com datafeed
  role_map.json         # Position → hero ID mapping (carry, mid, offlane, support, hard_support)
.env                    # API keys: STRATZ_API_KEY, ANTHROPIC_API_KEY
.tmp/                   # Cache (disposable, regenerated automatically)
  heroes.json           # Cached hero list
  hero_stats.json       # Cached bracket win rates
  matchups_stratz/      # One JSON per hero (vs + with matchup data)
  patch_notes_cache.json # Latest patch notes (1-hour TTL)
```

## Key Technical Details

**Stratz API**
- Bearer token auth via `STRATZ_API_KEY`
- `curl-cffi` with `impersonate="chrome110"` to bypass Cloudflare
- Brackets: HERALD, CRUSADER, ARCHON, LEGEND, ANCIENT, DIVINE, IMMORTAL
- Matchup data has both `vs` (counter) and `with` (synergy) per hero per bracket

**Scoring Engine**
- Default weights: counter=0.55, win_rate=0.15, synergy=0.20, hero_pool=0.05, meta=0.05 (user-adjustable)
- Meta weight uses pick rate (games played) in the selected bracket — popular heroes get a boost
- Hero pool weight boosts heroes in the logged-in user's saved hero pool (1.0 if in pool, 0.0 if not)
- All components normalized to [0,1] before weighting
- Bayesian shrinkage (k=400): regresses matchup win rates toward 50% based on sample size
- Role-weighted matchups: lane interaction weights (mid vs mid = 2.0x, carry vs offlane = 1.5x, cross-lane = 0.3x)
- Win probability: sigmoid function `1 / (1 + exp(-raw * 10)) * 100` (calibrated for ~43-57% typical range)
- Win prob components: matchup advantage (65%), win rate advantage (10%), synergy difference (25%)

**Chat Assistant**
- Model: `claude-haiku-4-5-20251001` (cheapest/fastest)
- Context injected at query time: live Stratz meta data (top heroes per role with win rates + game counts) + latest patch notes + current draft state
- Meta score = win_rate * (picks/max_picks)^0.3 to filter out low-sample inflated win rates

**Patch Notes**
- Source: `https://www.dota2.com/datafeed/patchnotes?version={version}&language=english`
- API schema: `heroes[].hero_id`, `heroes[].hero_notes[]`, `heroes[].abilities[].ability_notes[]`, `items[].ability_notes[]`, `neutral_items[]`
- Hero IDs resolved to names via cached heroes.json

**Frontend**
- Keyboard-optimized: Tab switches my-pick/enemy-pick, Enter picks first search result, Escape clears search
- Auto-clear search + refocus after each pick
- Color-coded score badges on hero grid (green/yellow/red quartiles)
- Win probability panel appears when draft is complete (5v5)
- Floating chat panel (bottom-right FAB)

## Server Management

Kill and restart:
```bash
powershell -Command "Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force"
powershell -Command "Start-Process python -ArgumentList 'app.py' -WorkingDirectory 'C:/Users/Khan Gadget/Desktop/Claude'"
```

After backend changes, always restart the server. After frontend changes, hard refresh the browser (Ctrl+Shift+R).

## Common Pitfalls

- Browser caching stale JS/CSS: always Ctrl+Shift+R after frontend changes
- `_cache["matchups"]` structure: `{"vs": {bracket_enum: {hero_id: {opp_id: {...}}}}, "with": {...}}`
- Stratz bracket enums differ between `fetch_hero_data.py` (e.g. "IMMORTAL") and `fetch_matchups.py` (e.g. "DIVINE_IMMORTAL") — each module has its own `BRACKET_ENUM` mapping
- Patch notes API uses `hero_id` not hero names — must resolve via heroes.json cache
