"""
wo_melkkoppelingen.py
=====================
Verwerkt automatisch WO's voor 'Aandraaien melkkoppelingen ter preventie van waterlekken'.

Logica:
  - Na elke scrape-cyclus worden de verse werkorders gescand
  - Als er een WO staat met omschrijving die 'Aandraaien melkkoppelingen' bevat
    én status GOED → afwerken
  - Afwerken = uitvoerder (rbruyn1) invullen → opslaan (INUITV) → rapportage-tab →
    oplossing aanvinken → uren (2u op rbruyn1) →
    status UITGEV → definitief opslaan → check UITGEV

Deduplicatie: per WO-ID wordt bijgehouden of die al verwerkt is (in-memory,
reset bij herstart).
"""

import asyncio
import logging
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# ── Log bestand ───────────────────────────────────────────────────────────────
_LOG_PAD = Path(__file__).parent / "melkkoppelingen_log.txt"

def _schrijf_log(msg: str):
    """Schrijft een regel naar melkkoppelingen_log.txt én naar de Python logger."""
    ts = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    regel = f"[{ts}] {msg}"
    log.info(f"Melkkoppelingen: {msg}")
    try:
        with open(_LOG_PAD, "a", encoding="utf-8") as f:
            f.write(regel + "\n")
    except Exception as e:
        log.error(f"Kon niet schrijven naar melkkoppelingen_log.txt: {e}")

def _trigger_refresh():
    """Vraagt de Flask app om onmiddellijk een nieuwe scrape te doen."""
    try:
        import urllib.request, json as _json
        from pathlib import Path as _Path
        cfg_pad = _Path(__file__).parent / "config.json"
        port = 5000
        try:
            with open(cfg_pad, encoding="utf-8") as f:
                port = _json.load(f).get("server_port", 5000)
        except Exception:
            pass
        urllib.request.urlopen(
            urllib.request.Request(
                f"http://localhost:{port}/api/refresh",
                data=b"",
                method="POST"
            ),
            timeout=5
        )
        _schrijf_log("✓ Refresh getriggerd na verwerking")
    except Exception as e:
        _schrijf_log(f"⚠ Refresh trigger mislukt (niet kritiek): {e}")


# ── Sleutelwoord in omschrijving ──────────────────────────────────────────────
MELK_KEYWORD  = "Aandraaien melkkoppelingen"

# ── Uitvoerder + uren ─────────────────────────────────────────────────────────
MELK_OPRID    = "rbruyn1"
MELK_UREN     = "2"

# ── Hoofdscraper-check via scraper.lock ───────────────────────────────────────
_LOCK_PAD = Path(__file__).parent / "scraper.lock"

def _is_hoofdscraper() -> bool:
    """Geeft True als deze machine de hoofdscraper is (host in scraper.lock)."""
    import socket, json
    try:
        with open(_LOCK_PAD, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("host", "").lower() == socket.gethostname().lower()
    except Exception:
        return False

# ── In-memory set van al verwerkte WO-ID's ────────────────────────────────────
_verwerkt: set[str] = set()
_verwerkt_lock = threading.Lock()


def is_melk_wo(wo: dict) -> bool:
    """Geeft True als dit een melkkoppelingen WO is in status GOED."""
    omschr = wo.get("omschrijving", "")
    status = wo.get("status", "").upper()
    return status == "GOED" and MELK_KEYWORD in omschr


def filter_te_verwerken(werkorders: list[dict]) -> list[dict]:
    """Geeft de melkkoppelingen WO's terug die nog verwerkt moeten worden."""
    kandidaten = [wo for wo in werkorders if is_melk_wo(wo)]
    if kandidaten:
        _schrijf_log(f"Scrape: {len(kandidaten)} melkkoppelingen WO('s) gevonden in GOED: "
                     f"{[w['wo_id'] for w in kandidaten]}")
    with _verwerkt_lock:
        te_doen = [wo for wo in kandidaten if wo.get("wo_id") not in _verwerkt]
    if kandidaten and not te_doen:
        _schrijf_log("Alle gevonden WO's zijn al verwerkt in deze sessie — overgeslagen")
    return te_doen


def markeer_verwerkt(wo_id: str):
    with _verwerkt_lock:
        _verwerkt.add(wo_id)


# ── Playwright: WO afwerken ───────────────────────────────────────────────────

async def verwerk_melk_wo(page, wo_id: str, stap_log=None) -> dict:
    """
    Navigeer naar bestaande WO (GOED), vul in en zet op UITGEV:
      uitvoerder rbruyn1 → opslaan (INUITV) → rapportage →
      oplossing aanvinken → 2u op rbruyn1 → UITGEV → definitief opslaan
    """
    from wo_ro_staalname import PS_USER, WO_DETAIL_URL

    async def _zoek_frame(page):
        frames = page.frames
        tc = page
        for f in frames:
            try:
                if f.name == "TargetContent":
                    tc = f
                    break
            except Exception:
                pass
        if tc is page:
            for f in frames:
                try:
                    el = await f.query_selector('[id="UZFM_WO_WERKUUR_UZ_WO_UREN$0"]')
                    if el:
                        tc = f
                        break
                except Exception:
                    pass
        return tc

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] MELK {wo_id}: {msg}")
        if stap_log:
            stap_log(msg)

    log(f"Start verwerking WO {wo_id}")

    url = WO_DETAIL_URL.format(wo_id=wo_id)
    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_selector("#UZFM_WO_UZ_WO_STATUS", state="visible", timeout=20_000)
    status = (await page.locator("#UZFM_WO_UZ_WO_STATUS").inner_text(timeout=5_000)).strip()
    log(f"Stap 1: ✓ WO geladen — status = '{status}'")

    # ── Uitvoerder ────────────────────────────────────────────────────────────
    uitvoerder_field = '[id="UZFM_WO_UITVOER_OPRID$0"]'
    log(f"Stap 2: Uitvoerder ← '{MELK_OPRID}'")
    await page.click(uitvoerder_field)
    await page.keyboard.press("Control+a")
    await page.type(uitvoerder_field, MELK_OPRID, delay=50)
    await page.keyboard.press("Tab")
    await asyncio.sleep(2)
    log("Stap 2: ✓ Uitvoerder ingevuld")

    # ── Eerste opslaan → INUITV ───────────────────────────────────────────────
    log("Stap 3: [KLIK] Opslaan → wachten op INUITV")
    await page.click('[id="#ICSave"]')
    await asyncio.sleep(2)
    for poging in range(15):
        await asyncio.sleep(3)
        try:
            st = (await page.locator("#UZFM_WO_UZ_WO_STATUS").inner_text(timeout=5_000)).strip()
            log(f"Stap 3: Status = '{st}' (poging {poging+1})")
            if st == "INUITV":
                log("Stap 3: ✓ INUITV bereikt")
                break
        except Exception as e:
            log(f"Stap 3: Status niet leesbaar: {e}")
    else:
        raise RuntimeError("Status werd niet INUITV na opslaan (timeout)")

    # ── TargetContent frame ───────────────────────────────────────────────────
    log("Stap 4: Frame zoeken...")
    tc = await _zoek_frame(page)
    tc_naam = getattr(tc, "name", "page")
    log(f"Stap 4: ✓ Frame = '{tc_naam}'")

    # ── Rapportage-tab ────────────────────────────────────────────────────────
    log("Stap 5: [KLIK] Rapportage-tab")
    for selector in ["#ICPanel2", "#ICTAB_2"]:
        try:
            btn = await tc.query_selector(selector)
            if btn:
                await tc.click(selector)
                break
        except Exception:
            pass
    await tc.wait_for_selector("#UZFM_WO_WRK_UZ_WO_COMMENT", state="visible", timeout=20_000)
    log("Stap 5: ✓ Rapportage-tab geladen")

    # ── Opmerking ─────────────────────────────────────────────────────────────
    log("Stap 5b: Opmerking ← 'koppelingen nagekeken'")
    await tc.fill("#UZFM_WO_WRK_UZ_WO_COMMENT", "koppelingen nagekeken")
    log("Stap 5b: ✓ Opmerking ingevuld")

    # ── Status → UITGEV ───────────────────────────────────────────────────────
    log("Stap 6: Status instellen op UITGEV")
    await tc.select_option("#UZFM_WO_WRK_UZ_WO_STAT_WZ_TECH\\$70\\$", "UITGEV")
    await asyncio.sleep(0.5)
    log("Stap 6: ✓ Status UITGEV geselecteerd")

    # ── Oplossing aanvinken ───────────────────────────────────────────────────
    log("Stap 7: [KLIK] Oplossing checkbox")
    await tc.click("#UZFM_WO_WRK_UZ_WO_OPLOSSING")
    await asyncio.sleep(1)
    log("Stap 7: ✓ Oplossing aangevinkt")

    # ── Werkuren: 2u op rbruyn1 ───────────────────────────────────────────────
    log(f"Stap 8: Werkuren — 2u op {MELK_OPRID}")
    oprid_field = '[id="UZFM_WO_WERKUUR_OPRID$0"]'
    uren_field  = '[id="UZFM_WO_WERKUUR_UZ_WO_UREN$0"]'

    # OPRID enkel invullen als het niet de ingelogde user is
    if MELK_OPRID != PS_USER:
        for poging in range(10):
            await tc.click(oprid_field)
            await asyncio.sleep(0.2)
            await tc.keyboard.press("Control+a")
            await tc.keyboard.press("Delete")
            await asyncio.sleep(2)
            await tc.type(oprid_field, MELK_OPRID, delay=80)
            await tc.keyboard.press("Tab")
            await asyncio.sleep(3)
            try:
                naam_field = '[id="UZ_GEBR_HISTVW2_UZ_NAAM_VOLLEDIG$0"]'
                naam_tekst = await tc.locator(naam_field).inner_text(timeout=5_000)
                if naam_tekst.strip():
                    log(f"  ✓ Naam '{naam_tekst.strip()}'")
                    break
            except Exception as e:
                log(f"  Naam niet leesbaar (poging {poging+1}): {e}")
    else:
        log(f"  Ingelogde user = {MELK_OPRID} → OPRID overslaan")

    await asyncio.sleep(1)
    await tc.click(uren_field)
    await asyncio.sleep(0.5)
    await tc.keyboard.press("Control+a")
    await tc.type(uren_field, MELK_UREN, delay=100)
    await asyncio.sleep(0.5)
    await tc.keyboard.press("Tab")
    await asyncio.sleep(1)
    try:
        val = await tc.locator(uren_field).input_value()
        log(f"  ✓ Uren = '{val}'")
    except Exception:
        log(f"  ✓ Uren ingevuld")
    log("Stap 8: ✓ Werkuren klaar")

    # ── Definitief opslaan → UITGEV ───────────────────────────────────────────
    log("Stap 9: [KLIK] Definitief opslaan → wachten op UITGEV")
    await page.click('[id="#ICSave"]')
    await asyncio.sleep(2)
    for poging in range(100):
        await asyncio.sleep(2)
        try:
            st = (await page.locator("#UZFM_WO_UZ_WO_STATUS").inner_text(timeout=5_000)).strip()
            log(f"Stap 9: Status = '{st}' (poging {poging+1})")
            if st == "UITGEV":
                log("Stap 9: ✓ Eindstatus UITGEV bereikt")
                break
        except Exception as e:
            log(f"Stap 9: Status niet leesbaar: {e}")

    eindstatus = "UITGEV"
    try:
        eindstatus = (await page.locator("#UZFM_WO_UZ_WO_STATUS").inner_text(timeout=5_000)).strip()
    except Exception:
        pass
    log(f"✓ WO {wo_id} klaar — eindstatus: '{eindstatus}'")
    return {"ok": True, "wo_id": wo_id, "status": eindstatus, "fout": None}


async def verwerk_alle(werkorders: list[dict], stap_log=None) -> list[dict]:
    """
    Login eenmalig, verwerk alle gevonden melkkoppelingen WO's sequentieel.
    """
    from wo_ro_staalname import login, PS_HEADLESS
    from playwright.async_api import async_playwright

    if not _is_hoofdscraper():
        return []

    with _verwerkt_lock:
        te_verwerken = [wo for wo in werkorders if is_melk_wo(wo) and wo.get("wo_id") not in _verwerkt]
    if not te_verwerken:
        return []

    def log(msg):
        _schrijf_log(msg)
        if stap_log:
            stap_log(msg)

    _schrijf_log(f"START verwerking — {len(te_verwerken)} WO('s): "
                 f"{[w['wo_id'] for w in te_verwerken]}")
    resultaten = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=PS_HEADLESS)
        context = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="nl-BE")
        page    = await context.new_page()
        try:
            _schrijf_log("Login poging...")
            if not await login(page, log):
                _schrijf_log("❌ Login mislukt — melkkoppelingen WO's niet verwerkt")
                return [{"ok": False, "wo_id": w["wo_id"], "fout": "Login mislukt"} for w in te_verwerken]
            _schrijf_log("✓ Login geslaagd")

            for wo in te_verwerken:
                wo_id = wo["wo_id"]
                _schrijf_log(f"══ Start WO {wo_id} ══")
                try:
                    res = await verwerk_melk_wo(page, wo_id, stap_log=stap_log)
                    resultaten.append(res)
                    if res["ok"]:
                        markeer_verwerkt(wo_id)
                        _schrijf_log(f"══ WO {wo_id} ✓ KLAAR — eindstatus: {res['status']} ══")
                    else:
                        _schrijf_log(f"══ WO {wo_id} ❌ MISLUKT: {res['fout']} ══")
                except Exception as exc:
                    _schrijf_log(f"══ WO {wo_id} ❌ UITZONDERING: {exc} ══")
                    resultaten.append({"ok": False, "wo_id": wo_id, "fout": str(exc)})
        finally:
            await browser.close()

    _schrijf_log(f"EINDE — {sum(1 for r in resultaten if r['ok'])}/{len(resultaten)} geslaagd")
    if any(r["ok"] for r in resultaten):
        _trigger_refresh()
    return resultaten