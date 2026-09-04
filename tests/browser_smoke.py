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
        check("rec card added to MY team while in enemy mode", first_rec in r and first_rec not in d, f"radiant={r} dire={d}")

        # ── enemy prediction card still adds to ENEMY ────────────────
        await js(sleep % 900)
        pred_id = await js("state.enemy_predictions[0]?.hero_id")
        await js("setAddTarget('my-pick'); document.querySelector('#enemy-predictions-list .enemy-pred-card')?.click();")
        await js(sleep % 300)
        r, d = await js("[state.radiant_picks, state.dire_picks]")
        check("enemy prediction card added to ENEMY team while in my-pick mode", pred_id in d and pred_id not in r, f"radiant={r} dire={d}")

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
        check("Tab is NOT intercepted in search", tab_prevented is False, f"defaultPrevented={tab_prevented}")

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
