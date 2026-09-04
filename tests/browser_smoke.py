"""Browser smoke test via Chrome DevTools Protocol (Edge headless). Needs the server running on :8000.
Run: python tests/browser_smoke.py   (not collected by pytest on purpose)"""
import asyncio, json, subprocess, time, urllib.request, os, sys, tempfile

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if not os.path.exists(EDGE):
    EDGE = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
PORT = 9333
URL = "http://127.0.0.1:8000/"
profile = tempfile.mkdtemp(prefix="edge-cdp-")

proc = subprocess.Popen([
    EDGE, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}", "--window-size=1400,1000", "about:blank",
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def get_ws():
    for _ in range(50):
        try:
            targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
            for t in targets:
                if t.get("type") == "page":
                    return t["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError("no CDP target")

results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  -- {detail}" if detail else ""))

async def main():
    import aiohttp
    ws_url = get_ws()
    async with aiohttp.ClientSession() as session, session.ws_connect(ws_url, max_msg_size=50_000_000) as ws:
        mid = 0
        errors = []
        async def send(method, **params):
            nonlocal mid
            mid += 1
            await ws.send_str(json.dumps({"id": mid, "method": method, "params": params}))
            while True:
                msg = json.loads((await ws.receive()).data)
                if msg.get("method") == "Runtime.exceptionThrown":
                    errors.append(msg["params"]["exceptionDetails"].get("exception", {}).get("description", str(msg["params"])))
                if msg.get("method") == "Runtime.consoleAPICalled" and msg["params"]["type"] == "error":
                    errors.append("console.error: " + " ".join(str(a.get("value", a.get("description"))) for a in msg["params"]["args"]))
                if msg.get("id") == mid:
                    return msg.get("result", {})
        async def js(expr, timeout=20):
            r = await send("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True, timeout=timeout*1000)
            if "exceptionDetails" in r:
                raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description", str(r["exceptionDetails"])))
            return r.get("result", {}).get("value")

        await send("Runtime.enable")
        await send("Page.enable")
        await send("Page.navigate", url=URL)
        # wait for app init
        await js("""new Promise(res => { const t = setInterval(() => { if (!document.getElementById('app').classList.contains('hidden')) { clearInterval(t); res(true); } }, 100); })""")
        check("app initialised (splash gone)", True)

        sleep = "new Promise(r => setTimeout(r, %d))"

        # ── 1.3: rec card adds to MY team even in enemy mode ─────────
        await js("state.radiant_picks=[]; state.dire_picks=[]; state.my_team='radiant'; document.getElementById('my-team-select').value='radiant';")
        await js("setAddTarget('enemy-pick'); handleHeroCardClick(8); handleHeroCardClick(11);")  # enemy: Juggernaut, Shadow Fiend
        await js(sleep % 900)
        n_rec = await js("document.querySelectorAll('#rec-list .rec-card').length")
        check("recommendations rendered after enemy picks", n_rec > 0, f"{n_rec} cards")
        first_rec = await js("state.recommendations[0]?.hero_id")
        await js("setAddTarget('enemy-pick'); document.querySelector('#rec-list .rec-card').click();")
        await js(sleep % 300)
        r, d = await js("[state.radiant_picks, state.dire_picks]")
        check("rec card follows toggle (enemy mode -> dire)", first_rec in d and first_rec not in r, f"radiant={r} dire={d}")

        # ── enemy prediction card still adds to ENEMY ────────────────
        await js(sleep % 900)
        pred_id = await js("state.enemy_predictions[0]?.hero_id")
        await js("setAddTarget('my-pick'); document.querySelector('#enemy-predictions-list .enemy-pred-card')?.click();")
        await js(sleep % 300)
        r, d = await js("[state.radiant_picks, state.dire_picks]")
        check("prediction card follows toggle (my-pick mode -> radiant)", pred_id in r and pred_id not in d, f"radiant={r} dire={d}")

        # ── 1.13: threat tiers by threshold ──────────────────────────
        await js(sleep % 900)
        tiers = await js("[...document.querySelectorAll('.threat-tier-label')].map(e=>e.textContent)")
        sevs  = await js("[...document.querySelectorAll('.threat-sev')].map(e=>e.textContent)")
        crit_count = await js("state.threats.filter(t=>t.avg_win_rate>=0.53).length")
        check("threat tier label matches threshold", ("CRITICAL THREATS" in tiers) == (crit_count > 0), f"tiers={tiers} sevs={sevs}")

        # ── 1.4: slot remove is a button with aria-label ─────────────
        tag = await js("document.querySelector('.pick-slot.filled .slot-remove')?.tagName")
        aria = await js("document.querySelector('.pick-slot.filled .slot-remove')?.getAttribute('aria-label')")
        check("slot remove is a <button> with aria-label", tag == "BUTTON" and aria and aria.startswith("Remove "), f"{tag} {aria}")

        # ── 1.5: Space on empty search toggles team; Tab is not intercepted ──
        await js("setAddTarget('my-pick'); const s=document.getElementById('hero-search'); s.value=''; s.focus();")
        await js("document.getElementById('hero-search').dispatchEvent(new KeyboardEvent('keydown',{key:' ',bubbles:true,cancelable:true}))")
        t1 = await js("state.add_target")
        tab_prevented = await js("const e=new KeyboardEvent('keydown',{key:'Tab',bubbles:true,cancelable:true}); document.getElementById('hero-search').dispatchEvent(e); e.defaultPrevented")
        check("Space on empty search flips target", t1 == "enemy-pick", t1)
        t2 = await js("state.add_target")
        check("Tab flips target back (user preference)", tab_prevented is True and t2 == "my-pick", f"prevented={tab_prevented} target={t2}")

        # ── 1.1: XSS — Steam name escaped ────────────────────────────
        await js("""showAccountStatus({name: '<img src=x onerror="window.__xss=1">', rank: '<b>Divine</b>', overall_wr: 55, total_matches: 100})""")
        await js(sleep % 200)
        xss = await js("window.__xss === 1")
        raw = await js("document.getElementById('account-info').textContent")
        check("Steam name rendered as text, no script execution", (not xss) and "<img" in raw, raw.strip()[:60])

        # ── 1.7: win prob renders WHY sections ───────────────────────
        await js("state.radiant_picks=[1,2,3,4,5]; state.dire_picks=[6,7,8,9,10]; onStateChange();")
        await js(sleep % 1500)
        wp_visible = await js("!document.getElementById('winprob-panel').classList.contains('hidden')")
        factors = await js("document.querySelectorAll('#winprob-content .wp-factor-row').length")
        matchups = await js("document.querySelectorAll('#winprob-content .wp-matchup-row').length")
        syns = await js("document.querySelectorAll('#winprob-content .wp-syn-row').length")
        check("win prob panel shows factors + matchups + synergies", wp_visible and factors == 3 and matchups > 0 and syns > 0, f"factors={factors} matchups={matchups} syn={syns}")

        # ── 1.6: stale request guard — rapid changes end consistent ──
        await js("state.radiant_picks=[]; state.dire_picks=[]; onStateChange();")
        await js("for (const id of [1,2,3]) { state.dire_picks=[id]; fetchRecommendations(); }")  # 3 rapid calls, only last should win
        await js(sleep % 1200)
        hint = await js("document.getElementById('rec-hint').textContent")
        top_vs = await js("state.recommendations[0]?.breakdown?.counters_detail?.map(c=>c.vs_hero_id)")
        check("stale-request guard: results match final draft (dire=[3])", top_vs == [3], f"counters vs {top_vs}; hint='{hint}'")

        # ── 1.11: Escape closes auth modal; hero cards keyboard-activatable ──
        await js("openAuthModal('login')")
        await js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
        closed = await js("document.getElementById('auth-modal').classList.contains('hidden')")
        check("Escape closes auth modal", closed)
        await js("state.radiant_picks=[]; state.dire_picks=[]; setAddTarget('my-pick'); onStateChange();")
        await js("const c=document.querySelector('#hero-grid .hero-card:not(.used)'); c.focus(); c.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))")
        await js(sleep % 200)
        r = await js("state.radiant_picks.length")
        check("Enter on focused hero card picks it", r == 1, f"radiant={r}")

        # ── chat gating: not logged in → disabled input + login link ─
        await js("setAuthState(null,null); chatGateShown=false; chatOpen();")
        disabled = await js("document.getElementById('chat-input').disabled")
        link = await js("!!document.getElementById('chat-login-link')")
        check("chat gated for guests with login link", disabled and link)

        # ═══ Phase 2 ═══════════════════════════════════════════════
        await js("localStorage.removeItem('draft_state_v1'); state.radiant_picks=[]; state.dire_picks=[]; state.bans=[]; state.first_pick_team=null; state.my_team='radiant'; document.getElementById('my-team-select').value='radiant'; updateYouBadge(); setAddTarget('my-pick'); onStateChange();")

        # 2.11 YOU badge follows my_team
        mine = await js("[document.getElementById('team-radiant').classList.contains('mine'), document.getElementById('team-dire').classList.contains('mine')]")
        check("YOU badge on Radiant when my_team=radiant", mine == [True, False], str(mine))

        # 2.2 bans: 14 slots, ban mode, backend excludes banned hero from recs
        nban = await js("document.querySelectorAll('#ban-slots .ban-slot').length")
        await js("setAddTarget('ban'); handleHeroCardClick(1); handleHeroCardClick(2);")
        await js(sleep % 900)
        bans = await js("state.bans")
        target_after_ban = await js("state.add_target")
        rec_ids = await js("state.recommendations.map(r=>r.hero_id)")
        filled = await js("document.querySelectorAll('#ban-slots .ban-slot.filled').length")
        check("14 ban slots, bans recorded, stay in ban mode, banned heroes absent from recs",
              nban == 14 and bans == [1, 2] and target_after_ban == 'ban' and filled == 2 and 1 not in rec_ids and 2 not in rec_ids,
              f"slots={nban} bans={bans} target={target_after_ban} filled={filled}")

        # 2.1 click empty enemy (dire) slot -> enemy-pick target + marker on that slot
        await js("document.querySelector('#dire-picks .pick-slot.empty').click()")
        t = await js("state.add_target")
        marked = await js("document.querySelector('#dire-picks .pick-slot.target-next') !== null")
        check("clicking empty Dire slot sets enemy-pick target and marks it", t == 'enemy-pick' and marked, f"target={t} marked={marked}")

        # No draft-order guessing: target stays put until the side is full
        await js("handleHeroCardClick(8)")   # dire
        t_after = await js("state.add_target")
        await js("setAddTarget('my-pick'); handleHeroCardClick(9); handleHeroCardClick(10);")
        r, d = await js("[state.radiant_picks, state.dire_picks]")
        check("target does not auto-switch after a pick", t_after == 'enemy-pick' and r == [9, 10] and d == [8], f"after={t_after} r={r} d={d}")

        # 2.3 undo via Ctrl+Z, via Backspace on empty search, and the header button
        before = await js("[state.radiant_picks.length, state.dire_picks.length, state.bans.length]")
        await js("document.body.focus(); document.dispatchEvent(new KeyboardEvent('keydown',{key:'z',ctrlKey:true,bubbles:true}))")
        after_ctrl = await js("[state.radiant_picks.length, state.dire_picks.length, state.bans.length]")
        await js("{ const s=document.getElementById('hero-search'); s.value=''; s.focus(); s.dispatchEvent(new KeyboardEvent('keydown',{key:'Backspace',bubbles:true,cancelable:true})); }")
        after_bs = await js("[state.radiant_picks.length, state.dire_picks.length, state.bans.length]")
        btn_enabled = await js("!document.getElementById('undo-btn').disabled")
        check("Ctrl+Z and Backspace each undo one step; Undo button enabled", before == [2,1,2] and after_ctrl == [1,1,2] and after_bs == [0,1,2] and btn_enabled, f"{before}->{after_ctrl}->{after_bs}")

        # 2.4 persistence: saved to localStorage; resume offer after reload
        saved = await js("JSON.parse(localStorage.getItem('draft_state_v1'))")
        check("draft persisted to localStorage", bool(saved) and saved.get('dire') == [8] and saved.get('bans') == [1, 2], str({k: saved.get(k) for k in ('radiant','dire','bans')}) if saved else "none")
        await send("Page.reload")
        await js("new Promise(res => { const t = setInterval(() => { const a = document.getElementById('app'); if (a && !a.classList.contains('hidden')) { clearInterval(t); res(true); } }, 100); })")
        await js(sleep % 300)
        bar_shown = await js("!document.getElementById('resume-bar').classList.contains('hidden')")
        await js("document.getElementById('resume-yes').click()")
        await js(sleep % 300)
        restored = await js("[state.radiant_picks, state.dire_picks, state.bans]")
        check("resume bar offered after reload and restores picks/bans", bar_shown and restored == [[], [8], [1, 2]], f"shown={bar_shown} restored={restored}")

        # 2.8 search: alias, word-prefix, ranking
        await js("state.radiant_picks=[]; state.dire_picks=[]; state.bans=[]; onStateChange();")
        am = await js("searchHeroes('am')[0]?.localized_name")
        sky = await js("searchHeroes('sky m')[0]?.localized_name")
        qop = await js("searchHeroes('qop')[0]?.localized_name")
        sk  = await js("searchHeroes('sk')[0]?.localized_name")
        check("alias + word-prefix search", am == 'Anti-Mage' and sky == 'Skywrath Mage' and qop == 'Queen of Pain' and sk == 'Sand King', f"{am} / {sky} / {qop} / {sk}")

        # 2.9 grid grouped by attribute when not searching
        groups = await js("[...document.querySelectorAll('#hero-grid .hero-grid-group')].map(e=>e.textContent)")
        is_open = await js("document.getElementById('hero-grid-toggle').open")
        check("hero grid collapsed by default, grouped by attribute", (not is_open) and groups == ['STRENGTH', 'AGILITY', 'INTELLIGENCE', 'UNIVERSAL'], f"open={is_open} {groups}")

        # 2.10 digit key takes Nth recommendation
        await js("setAddTarget('enemy-pick'); handleHeroCardClick(11); setAddTarget('my-pick');")
        await js(sleep % 900)
        third = await js("state.recommendations[2]?.hero_id")
        await js("{ const s=document.getElementById('hero-search'); s.value=''; s.focus(); s.dispatchEvent(new KeyboardEvent('keydown',{key:'3',bubbles:true,cancelable:true})); }")
        await js(sleep % 200)
        r = await js("state.radiant_picks")
        check("pressing 3 takes the 3rd recommendation onto my team", third in r, f"third={third} radiant={r}")

        # 2.6 / 2.7 disclosure
        note = await js("document.getElementById('bracket-note').textContent")
        foot = await js("document.getElementById('app-footer').textContent")
        check("bracket cohort note + freshness footer rendered", "matchup data" in note and "Stratz data updated" in foot, f"{note} | {foot[:60]}")

        await js("localStorage.removeItem('draft_state_v1')")

        # ── console errors ───────────────────────────────────────────
        await js(sleep % 300)
        check("no uncaught JS errors / console.error", len(errors) == 0, "; ".join(errors)[:300])

    proc.terminate()

try:
    asyncio.run(main())
except Exception:
    import traceback; traceback.print_exc()
finally:
    try: proc.kill()
    except Exception: pass
    fails = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(fails)}/{len(results)} passed")
    sys.exit(1 if fails else 0)
