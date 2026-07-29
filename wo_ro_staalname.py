"""
wo_ro_staalname.py - RO Staalname WO Opzoeken
"""

import asyncio
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

_cfg = {}
try:
    pad = Path(__file__).parent / "config.json"
    if pad.exists():
        with open(pad, encoding="utf-8") as f:
            _cfg = json.load(f)
except:
    pass

PS_BASE_URL = os.getenv("PS_BASE_URL", _cfg.get("ps_base_url_td", "https://peoplesoftlogistiek.uz.kuleuven.ac.be/psp/FS9PROD/"))
PS_PSC_URL = os.getenv("PS_PSC_URL", _cfg.get("ps_psc_url", "https://peoplesoftlogistiek.uz.kuleuven.ac.be/psc/FS9PROD/"))
PS_USER = os.getenv("PS_USER", _cfg.get("username", ""))
from crypto_utils import get_ps_password
PS_PASS = os.getenv("PS_PASS", get_ps_password(_cfg))
PS_HEADLESS = os.getenv("PS_HEADLESS", "false").lower() == "true"

RO_DATA_PATH = Path(__file__).parent / "ro_staalname.json"

LOGIN_URL = PS_BASE_URL + "?cmd=login"
WO_ZOEK_URL = (
    PS_PSC_URL
    + "EMPLOYEE/ERP/c/UZFM_MENU.UZFM_WO_ZK.GBL"
    "?FolderPath=PORTAL_ROOT_OBJECT.UZFM.UZFM_WERKORDER.UZFM_WO_ZK_GBL"
    "&IsFolder=false&PortalHostNode=ERP&NoCrumbs=yes&PortalKeyStruct=yes"
)
WO_DETAIL_URL = (
    PS_PSC_URL
    + "EMPLOYEE/ERP/c/UZFM_MENU.UZFM_WERKORDER.GBL"
    "?BUSINESS_UNIT=POUZL&UZ_WO_ID={wo_id}&PAGE=UZFM_WO_ALG"
)

# Werkuren gebruikers uit config
def _lees_werkuren_users() -> list:
    users_cfg = _cfg.get("werkuren_users", [])
    if users_cfg:
        return [{"oprid": u.get("oprid", ""), "naam": u.get("naam", "")} for u in users_cfg if u.get("oprid")]
    return [{"oprid": PS_USER, "naam": ""}]

PS_USERS = _lees_werkuren_users()

from uitvoerder_utils import bepaal_uitvoerder
UITVOERDER = bepaal_uitvoerder(_cfg, PS_USER)

# Statussen die wij willen verwerken
VERWERK_STATUSSEN = {"GOED"}          # Enkel GOED wordt ingevuld
INUITV_STATUSSEN  = {"INUITV"}         # Tonen + vragen of nieuwe WO gewenst
SKIP_STATUSSEN    = {"UITGEV", "GESLOTEN", "UITVOER"}  # Overgeslagen → nieuwe WO aanbieden


def lees_json():
    if RO_DATA_PATH.exists():
        with open(RO_DATA_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"installaties": [], "geschiedenis": []}


async def login(page, log) -> bool:
    log(f"LOGIN: Navigeren naar {LOGIN_URL}")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
    log(f"LOGIN: Pagina geladen — URL: {page.url[:80]}")
    await page.fill("#userid", PS_USER)
    await page.fill("#pwd", PS_PASS)
    await page.click("input[name='Submit']")
    log("LOGIN: Wachten op redirect...")
    try:
        await page.wait_for_url(
            lambda url: "EMPLOYEE" in url or "errorCode" in url,
            timeout=30_000
        )
    except Exception as e:
        log(f"LOGIN: Timeout bij redirect: {e}")
    if "errorCode" in page.url:
        log("LOGIN: ❌ Login mislukt (errorCode)")
        return False
    log("LOGIN: ✓ Login geslaagd")
    return True


async def zoek_wo_voor_installatie(page, installatie, stap_log=None):
    """
    Zoek de werkorder op in PeopleSoft op basis van T-nummer (maximo).
    Geeft terug: {"wo_id": ..., "status": ..., "ok": bool, "fout": ...}
    """

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] WO-zoek {installatie.get('maximo','?')}: {msg}")
        if stap_log:
            stap_log(msg)

    maximo = installatie.get("maximo", "").strip()
    naam   = installatie.get("naam", "?")

    if not maximo:
        return {"ok": False, "wo_id": None, "status": None, "fout": "Geen maximo"}

    # Zoekvenster: 2 maanden terug t/m 15 dagen vooruit vanaf begin huidige maand
    vandaag      = datetime.now()
    maand_begin  = vandaag.replace(day=1)
    start_range  = (maand_begin - timedelta(days=60)).strftime("%d/%m/%Y")
    end_range    = (maand_begin + timedelta(days=15)).strftime("%d/%m/%Y")

    log(f"Stap 1: Navigeren naar WO-zoekscherm")
    log(f"Stap 1: T-nummer={maximo}, zoekvenster={start_range} → {end_range}")

    try:
        await page.goto(WO_ZOEK_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_selector("#UZFM_WO_ZK_WRK_UZ_OMSCHR254", state="visible", timeout=20_000)
        log("Stap 1: ✓ Zoekscherm geladen")

        # Werktype = k (kwaliteit)
        log("Stap 2: Werktype 'k' ingeven")
        await page.click("#UZFM_WO_ZK_WRK_UZ_WO_TYPE")
        await page.keyboard.press("k")
        await page.keyboard.press("k")
        await page.click("#UZFM_WO_ZK_WRK_UZ_WO_TYPE")
        await asyncio.sleep(0.2)

        # Ook afgesloten WO's doorzoeken
        await page.select_option("#UZFM_WO_ZK_WRK_UZ_WO_OPEN_ZK", "")
        await asyncio.sleep(0.2)

        # Begindatum zoekvenster
        log(f"Stap 3: Begindatum ← '{start_range}'")
        await page.click("#UZFM_WO_ZK_WRK_UZ_AANVRAAG_DT")
        await page.keyboard.press("Control+a")
        await asyncio.sleep(0.5)
        await page.type("#UZFM_WO_ZK_WRK_UZ_AANVRAAG_DT", start_range, delay=80)
        await page.keyboard.press("Tab")
        await asyncio.sleep(0.5)

        # Einddatum zoekvenster
        log(f"Stap 4: Einddatum ← '{end_range}'")
        await page.click("#UZFM_WO_ZK_WRK_END_DATE")
        await page.keyboard.press("Control+a")
        await asyncio.sleep(0.5)
        await page.type("#UZFM_WO_ZK_WRK_END_DATE", end_range, delay=80)
        await page.keyboard.press("Tab")
        await asyncio.sleep(0.5)

        # T-nummer — als laatste invullen (anders verdwijnen andere velden)
        log(f"Stap 5: T-nummer ← '{maximo}'")
        await page.click("#UZFM_WO_ZK_WRK_UZ_OBJ_ID")
        await page.keyboard.press("Control+a")
        await asyncio.sleep(0.2)
        await page.type("#UZFM_WO_ZK_WRK_UZ_OBJ_ID", maximo, delay=80)
        await page.keyboard.press("Tab")
        await asyncio.sleep(0.5)

        # Zoeken starten
        log("Stap 6: [KLIK] Zoeken")
        await page.click("#UZFM_WO_ZK_WRK_UZ_ZOEK")

        # Wachten op resultaat (max 30s / 10 pogingen à 3s)
        log("Stap 7: Wachten op zoekresultaten...")
        gevonden_rijen = []
        for poging in range(10):
            await asyncio.sleep(3)
            aanwezig = await page.query_selector("#UZFM_WO_ZK_UZ_OMSCHR254\\$0")
            if aanwezig:
                # Lees alle rijen uit (rij 0, 1, 2, ...)
                rij = 0
                while True:
                    cel = await page.query_selector(f"#UZFM_WO_ZK_UZ_OMSCHR254\\${rij}")
                    if not cel:
                        break
                    try:
                        rid     = (await page.locator(f"#UZ_WO_ID\\${rij}").inner_text(timeout=3_000)).strip()
                        rstatus = (await page.locator(f"#UZFM_WO_ZK_UZ_WO_STATUS\\${rij}").inner_text(timeout=3_000)).strip()
                        romschr = (await page.locator(f"#UZFM_WO_ZK_UZ_OMSCHR254\\${rij}").inner_text(timeout=3_000)).strip()
                        if rid:
                            gevonden_rijen.append({"wo_id": rid, "status": rstatus, "omschrijving": romschr})
                            log(f"Stap 7: Rij {rij}: WO {rid} — {rstatus} — {romschr}")
                    except Exception as e:
                        log(f"Stap 7: Rij {rij} niet leesbaar: {e}")
                        break
                    rij += 1
                break
            log(f"Stap 7: Nog geen resultaat (poging {poging+1}/10)...")
        else:
            log("Stap 7: ℹ️ Geen werkorder gevonden (timeout)")
            return {"ok": True, "wo_id": None, "status": None, "fout": None,
                    "maximo": maximo, "naam": naam, "keuze_nodig": False, "geen_wo": True}

        if not gevonden_rijen:
            log("Stap 7: ⚠ WO-element gevonden maar geen rijen leesbaar")
            return {"ok": False, "wo_id": None, "status": None,
                    "fout": "WO-element gevonden maar geen rijen leesbaar",
                    "maximo": maximo, "naam": naam, "keuze_nodig": False, "geen_wo": False}

        # Filter
        log(f"Stap 8: Filteren — {len(gevonden_rijen)} rij(en) gevonden")
        goed_rijen   = [r for r in gevonden_rijen if r["status"].upper() in VERWERK_STATUSSEN]
        inuitv_rijen = [r for r in gevonden_rijen if r["status"].upper() in INUITV_STATUSSEN]
        skip_rijen   = [r for r in gevonden_rijen if r["status"].upper() in SKIP_STATUSSEN]
        for r in skip_rijen:
            log(f"Stap 8: Overgeslagen (status={r['status']}): WO {r['wo_id']}")

        # INUITV gevonden → tonen + vragen of nieuwe WO gewenst
        if inuitv_rijen and not goed_rijen:
            wo = inuitv_rijen[0]
            log(f"Stap 8: WO {wo['wo_id']} staat al INUITV → tonen, nieuwe WO aanbieden")
            return {"ok": True, "wo_id": wo["wo_id"], "status": wo["status"], "fout": None,
                    "maximo": maximo, "naam": naam, "keuze_nodig": False,
                    "geen_wo": False, "al_inuitv": True}

        log(f"Stap 8: {len(goed_rijen)} GOED WO('s) na filter")

        if not goed_rijen:
            log("Stap 8: Geen GOED WO gevonden → popup nieuw aanmaken")
            return {"ok": True, "wo_id": None, "status": None, "fout": None,
                    "maximo": maximo, "naam": naam, "keuze_nodig": False, "geen_wo": True,
                    "alle_rijen": gevonden_rijen}

        if len(goed_rijen) == 1:
            log(f"Stap 8: ✓ Eén GOED WO: {goed_rijen[0]['wo_id']}")
            return {"ok": True, "wo_id": goed_rijen[0]["wo_id"],
                    "status": goed_rijen[0]["status"], "fout": None,
                    "maximo": maximo, "naam": naam, "keuze_nodig": False, "geen_wo": False}

        # Meerdere GOED WO's — keuze vereist
        log(f"Stap 8: ⚠ {len(goed_rijen)} GOED WO's — keuze vereist")
        return {"ok": True, "wo_id": None, "status": None, "fout": None,
                "maximo": maximo, "naam": naam,
                "keuze_nodig": True, "geen_wo": False, "keuze_opties": goed_rijen}

    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        log(f"❌ Uitzondering: {exc}")
        for r in tb.splitlines():
            log(f"  TRACEBACK: {r}")
        return {"ok": False, "wo_id": None, "status": None, "fout": str(exc), "maximo": maximo, "naam": naam}


async def vul_wo_in(page, installatie: dict, wo_id: str, stap_log=None) -> dict:
    """
    Navigeer naar een bestaande WO (GOED of INUITV), vul uitvoerder, rapportage
    en werkuren in — identiek aan wo_aanmaken.py — en sla op.
    """
    naam   = installatie.get("naam", "?")
    maximo = installatie.get("maximo", "?")

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] VUL_WO {wo_id}: {msg}")
        if stap_log:
            stap_log(msg)

    url = WO_DETAIL_URL.format(wo_id=wo_id)
    log(f"Stap 1: Navigeren naar WO {wo_id} — {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_selector("#UZFM_WO_UZ_WO_STATUS", state="visible", timeout=20_000)

    huidige_status = (await page.locator("#UZFM_WO_UZ_WO_STATUS").inner_text(timeout=5_000)).strip()
    log(f"Stap 1: ✓ WO geladen, huidige status = '{huidige_status}'")

    # ── Uitvoerder invullen ───────────────────────────────────────────────────
    uitvoerder_field = '[id="UZFM_WO_UITVOER_OPRID$0"]'
    log(f"Stap 2: Uitvoerder invullen ← '{UITVOERDER}'")
    await page.click(uitvoerder_field)
    await page.keyboard.press("Control+a")
    await asyncio.sleep(0.2)
    await page.type(uitvoerder_field, UITVOERDER, delay=50)
    await page.keyboard.press("Tab")
    await asyncio.sleep(2)
    try:
        uitv_val = await page.locator(uitvoerder_field).input_value()
        log(f"Stap 2: ✓ Uitvoerder = '{uitv_val}'")
    except Exception:
        log("Stap 2: ✓ Uitvoerder ingevuld (verificatie niet beschikbaar)")

    # ── TargetContent frame zoeken ────────────────────────────────────────────
    log("Stap 3: TargetContent frame zoeken...")
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
    tc_naam = getattr(tc, "name", "page")
    log(f"Stap 3: ✓ Frame = '{tc_naam}'")

    # ── Rapportage-tab ────────────────────────────────────────────────────────
    log("Stap 4: [KLIK] Rapportage-tab '#ICTAB_2'")
    await tc.click("#ICTAB_2")
    await tc.wait_for_selector("#UZFM_WO_WRK_UZ_WO_COMMENT", state="visible", timeout=20_000)
    log("Stap 4: ✓ Rapportage-tab geladen")

    # ── Commentaar ────────────────────────────────────────────────────────────
    commentaar = f"Staalname op {naam} is uitgevoerd"
    log(f"Stap 5: Commentaar ← '{commentaar}'")
    await tc.fill("#UZFM_WO_WRK_UZ_WO_COMMENT", commentaar)
    log("Stap 5: ✓ Commentaar ingevuld")

    # ── Oplossing aanvinken ───────────────────────────────────────────────────
    log("Stap 6: [KLIK] Oplossing checkbox")
    await tc.click("#UZFM_WO_WRK_UZ_WO_OPLOSSING")
    await asyncio.sleep(1)
    log("Stap 6: ✓ Oplossing aangevinkt")

    # ── Werkuren — identiek aan wo_aanmaken.py ────────────────────────────────
    log(f"Stap 7: {len(PS_USERS)} werkuren-gebruiker(s) verwerken")

    async def vul_werkuren_lijn(frame, idx: int, user: dict):
        oprid_field = f'[id="UZFM_WO_WERKUUR_OPRID${idx}"]'
        uren_field  = f'[id="UZFM_WO_WERKUUR_UZ_WO_UREN${idx}"]'
        naam_field  = f'[id="UZ_GEBR_HISTVW2_UZ_NAAM_VOLLEDIG${idx}"]'
        lijn_nr     = idx + 1

        if user["oprid"] and user["oprid"] != PS_USER:
            log(f"  WERKUREN lijn {lijn_nr}: OPRID ← '{user['oprid']}'")
            for poging in range(10):
                await frame.click(oprid_field)
                await asyncio.sleep(0.2)
                await frame.keyboard.press("Control+a")
                await frame.keyboard.press("Delete")
                await asyncio.sleep(2)
                await frame.type(oprid_field, user["oprid"], delay=80)
                await frame.keyboard.press("Tab")
                await asyncio.sleep(3)
                try:
                    naam_tekst = await frame.locator(naam_field).inner_text(timeout=5_000)
                    if not user["naam"] or user["naam"].lower() in naam_tekst.lower():
                        log(f"  WERKUREN lijn {lijn_nr}: ✓ Naam '{naam_tekst.strip()}'")
                        break
                    log(f"  WERKUREN lijn {lijn_nr}: ⚠ Naam '{naam_tekst.strip()}' ≠ '{user['naam']}' (poging {poging+1})")
                except Exception as e:
                    log(f"  WERKUREN lijn {lijn_nr}: Naam niet leesbaar: {e} (poging {poging+1})")
        else:
            log(f"  WERKUREN lijn {lijn_nr}: OPRID = ingelogde user ({PS_USER}) → overslaan")

        await asyncio.sleep(1)
        await frame.click(uren_field)
        await asyncio.sleep(0.5)
        await frame.keyboard.press("Control+a")
        await frame.type(uren_field, "1", delay=100)
        await asyncio.sleep(0.5)
        await frame.keyboard.press("Tab")
        await asyncio.sleep(1)
        try:
            val = await frame.locator(uren_field).input_value()
            log(f"  WERKUREN lijn {lijn_nr}: ✓ Uren = '{val}'")
        except Exception:
            log(f"  WERKUREN lijn {lijn_nr}: ✓ Uren ingevuld")

    for idx, user in enumerate(PS_USERS):
        stap_nr = 7 + idx
        if idx == 0:
            log(f"Stap {stap_nr}: Werkuren lijn 1 — '{user['oprid']}'")
            await vul_werkuren_lijn(tc, 0, user)
            log(f"Stap {stap_nr}: ✓ Lijn 1 klaar")
        else:
            nieuwe_lijn_btn = f'[id="UZFM_WO_WERKUUR$new${idx-1}$$0"]'
            log(f"Stap {stap_nr}: [KLIK] Nieuwe lijn knop '{nieuwe_lijn_btn}'")
            await tc.click(nieuwe_lijn_btn)
            await asyncio.sleep(2)
            uren_field = f'[id="UZFM_WO_WERKUUR_UZ_WO_UREN${idx}"]'
            await tc.wait_for_selector(uren_field, state="visible", timeout=15_000)
            log(f"Stap {stap_nr}: ✓ Nieuwe rij zichtbaar — lijn {idx+1} invullen")
            await vul_werkuren_lijn(tc, idx, user)
            log(f"Stap {stap_nr}: ✓ Lijn {idx+1} klaar")

    # ── Opslaan ───────────────────────────────────────────────────────────────
    laatste_stap = 7 + len(PS_USERS)
    log(f"Stap {laatste_stap}: [KLIK] Opslaan")
    await page.click('[id="#ICSave"]')
    await asyncio.sleep(2)

    log(f"Stap {laatste_stap}: Wachten op eindstatus INUITV (max ~200s)...")
    eindstatus = huidige_status
    for poging in range(100):
        await asyncio.sleep(2)
        try:
            st = (await page.locator("#UZFM_WO_UZ_WO_STATUS").inner_text(timeout=5_000)).strip()
            log(f"Stap {laatste_stap}: Status = '{st}' (poging {poging+1})")
            if st == "INUITV":
                eindstatus = st
                log(f"Stap {laatste_stap}: ✓ INUITV bereikt")
                break
        except Exception as e:
            log(f"Stap {laatste_stap}: Status niet leesbaar: {e}")

    log(f"✓ WO {wo_id} ingevuld — eindstatus: {eindstatus}")
    return {"ok": True, "wo_id": wo_id, "status": eindstatus, "fout": None,
            "maximo": maximo, "naam": naam}


async def opzoek_werkorders_ro(installaties, stap_log=None):
    """
    Login eenmalig, zoek voor alle installaties de WO op.
    Geeft lijst terug met zelfde installatie-dicts, uitgebreid met wo_id en status.
    """
    from playwright.async_api import async_playwright

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] ZOEK: {msg}")
        if stap_log:
            stap_log(msg)

    resultaten = []

    async with async_playwright() as pw:
        log(f"Browser opstarten (headless={PS_HEADLESS})...")
        browser = await pw.chromium.launch(headless=PS_HEADLESS)
        context = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="nl-BE")
        page    = await context.new_page()
        log("Browser gereed ✓")

        try:
            if not await login(page, log):
                log("❌ Login mislukt")
                for inst in installaties:
                    resultaten.append({**inst, "ok": False, "wo_id": None,
                                       "status": None, "fout": "Login mislukt"})
                return resultaten

            log(f"✓ Ingelogd — {len(installaties)} installatie(s) verwerken")
            for idx, installatie in enumerate(installaties):
                log(f"══ {installatie.get('naam','?')} ({idx+1}/{len(installaties)}): {installatie.get('maximo','?')} ══")

                def inst_log(msg, _inst=installatie):
                    lbl = f"{_inst.get('naam','?')} | {_inst.get('maximo','?')}: {msg}"
                    ts  = datetime.now().strftime("%H:%M:%S")
                    print(f"[{ts}] {lbl}")
                    if stap_log:
                        stap_log(lbl)

                try:
                    res = await zoek_wo_voor_installatie(page, installatie, stap_log=inst_log)

                    # Direct invullen: enkel als GOED (niet keuze, niet geen_wo, niet al_inuitv)
                    if res.get("ok") and res.get("wo_id") and not res.get("keuze_nodig") and not res.get("geen_wo") and not res.get("al_inuitv"):
                        log(f"  → WO {res['wo_id']} gevonden ({res['status']}) — direct invullen")
                        try:
                            vul_res = await vul_wo_in(page, installatie, res["wo_id"], stap_log=inst_log)
                            res = {**res, **vul_res}
                        except Exception as vul_exc:
                            import traceback
                            log(f"  ❌ vul_wo_in mislukt: {vul_exc}")
                            for r in traceback.format_exc().splitlines():
                                log(f"    {r}")
                            res["fout"] = f"Invullen mislukt: {vul_exc}"
                            res["ok"] = False

                except Exception as exc:
                    import traceback
                    tb = traceback.format_exc()
                    log(f"❌ Onverwachte fout bij {installatie.get('maximo','?')}: {exc}")
                    for r in tb.splitlines():
                        log(f"  {r}")
                    res = {"ok": False, "wo_id": None, "status": None, "fout": str(exc)}
                resultaten.append({**installatie, **res})
                await asyncio.sleep(1)

        finally:
            log("Browser afsluiten...")
            await browser.close()
            log("Browser afgesloten ✓")

    return resultaten

# ── Nieuwe WO aanmaken voor RO installatie ────────────────────────────────────

WO_NIEUW_URL = (
    PS_PSC_URL
    + "EMPLOYEE/ERP/c/UZFM_MENU.UZFM_WERKORDER.GBL"
    "?BUSINESS_UNIT=POUZL&UZ_WO_ID=NEXT&PAGE=UZFM_WO_ALG"
)


async def wacht_op_element_ro(page, selector: str, timeout: int = 15_000, log=None):
    try:
        await page.wait_for_selector(selector, state="visible", timeout=timeout)
        if log:
            log(f"  ✓ Element '{selector}' zichtbaar")
    except Exception as e:
        if log:
            log(f"  ⚠ Timeout wachten op '{selector}': {e}")


async def maak_nieuw_wo_ro(page, installatie: dict, stap_log=None) -> dict:
    """
    Maak een nieuwe WO aan voor een RO-installatie (geen GOED WO gevonden).
    installatie: dict met 'naam', 'maximo', 'locatie'
    """
    naam   = installatie.get("naam", "?")
    maximo = installatie.get("maximo", "")

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] NIEUW_WO {naam}: {msg}")
        if stap_log:
            stap_log(f"{naam}: {msg}")

    omschrijving = f"Staalname {naam}"
    datum_begin  = (datetime.now() - timedelta(days=15)).strftime("%d/%m/%Y")
    datum_einde  = (datetime.now() + timedelta(days=15)).strftime("%d/%m/%Y")

    log(f"Stap 1: Navigeren naar nieuw WO formulier")
    await page.goto(WO_NIEUW_URL, wait_until="domcontentloaded", timeout=30_000)
    log(f"Stap 1: Pagina geladen — URL: {page.url[:80]}")

    # PTS toggle
    log("Stap 2: PTS toggle klikken")
    await wacht_op_element_ro(page, "#PTS_CFG_CL_WRK_PTS_PAGE_TOGGLE", log=log)
    await page.click("#PTS_CFG_CL_WRK_PTS_PAGE_TOGGLE")
    await asyncio.sleep(3)
    await page.click("#PTS_CFG_CL_WRK_PTS_ADD_BTN")
    await asyncio.sleep(1)
    log("Stap 2: ✓ PTS toggle klaar")

    # Omschrijving
    log(f"Stap 3: Omschrijving ← '{omschrijving}'")
    await wacht_op_element_ro(page, "#UZFM_WO_UZ_OMSCHR254", log=log)
    await page.fill("#UZFM_WO_UZ_OMSCHR254", omschrijving)
    log("Stap 3: ✓ Omschrijving ingevuld")

    # WO-type 'k'
    log("Stap 4: WO-type ← 'k'")
    await page.click("#UZFM_WO_UZ_WO_TYPE")
    await page.keyboard.press("k")
    await page.keyboard.press("k")
    await page.click("#UZFM_WO_UZ_WO_TYPE")
    await asyncio.sleep(0.2)
    log("Stap 4: ✓ WO-type ingevuld")

    # T-nummer (maximo)
    t_nr_field       = '[id="UZFM_WO_UZ_OBJ_ID$12$"]'
    uitvoerder_field = '[id="UZFM_WO_UITVOER_OPRID$0"]'
    omschr_div       = "#win0divUZFM_OBJECT_UZ_OMSCHR150"

    log(f"Stap 5: T-nummer ← '{maximo}'")
    for poging in range(10):
        await page.fill(t_nr_field, maximo)
        await page.click(uitvoerder_field)
        await asyncio.sleep(3)
        try:
            tekst = await page.locator(omschr_div).inner_text(timeout=3_000)
            if tekst and tekst.strip():
                log(f"Stap 5: ✓ Omschrijving geladen = '{tekst.strip()[:80]}'")
                break
            log(f"Stap 5: Omschrijving leeg (poging {poging+1}/10)...")
        except Exception as e:
            log(f"Stap 5: Omschrijving veld exceptie: {e} (poging {poging+1}/10)")

    # Uitvoerder
    log(f"Stap 6: Uitvoerder ← '{UITVOERDER}'")
    await page.click(uitvoerder_field)
    await asyncio.sleep(0.2)
    await page.keyboard.press("Control+a")
    await page.type(uitvoerder_field, UITVOERDER, delay=50)
    await page.keyboard.press("Tab")
    await asyncio.sleep(2)
    log("Stap 6: ✓ Uitvoerder ingevuld")

    # Eerste opslaan → WO-nummer ophalen
    log("Stap 7: Eerste opslaan")
    await page.click('[id="#ICSave"]')
    await asyncio.sleep(2)
    log("Stap 7: Wachten op status INUITV...")
    wo_id = None
    for poging in range(10):
        await asyncio.sleep(3)
        try:
            st = (await page.locator("#UZFM_WO_UZ_WO_STATUS").inner_text(timeout=5_000)).strip()
            log(f"Stap 7: Status = '{st}' (poging {poging+1})")
            if st == "INUITV":
                log("Stap 7: ✓ INUITV bereikt")
                break
        except Exception as e:
            log(f"Stap 7: Status niet leesbaar: {e}")
    else:
        raise RuntimeError("Status werd niet INUITV na eerste opslaan")

    try:
        wo_id = (await page.locator("#UZFM_WO_UZ_WO_ID").inner_text(timeout=5_000)).strip()
        log(f"Stap 7: ✓ WO-nummer = {wo_id}")
    except Exception as e:
        m = __import__("re").search(r"UZ_WO_ID=([^&]+)", page.url)
        if m:
            wo_id = m.group(1)
            log(f"Stap 7: WO-nummer via URL = {wo_id}")
    if not wo_id:
        raise RuntimeError("WO aangemaakt maar nummer niet gevonden")

    # Datums
    async def vul_datum(selector, waarde):
        await wacht_op_element_ro(page, selector, log=log)
        await page.click(selector)
        await page.keyboard.press("Control+a")
        await asyncio.sleep(0.5)
        await page.type(selector, waarde, delay=80)
        await page.keyboard.press("Tab")
        await asyncio.sleep(0.5)

    log(f"Stap 8: Begindatum ← '{datum_begin}'")
    await vul_datum("#UZFM_WO_UZ_WO_DT_BEGIN_PLN", datum_begin)
    log(f"Stap 8: Einddatum ← '{datum_einde}'")
    await vul_datum("#UZFM_WO_UZ_WO_DT_EINDE_PLN", datum_einde)
    log("Stap 8: ✓ Datums ingevuld")

    # TargetContent frame
    log("Stap 9: TargetContent frame zoeken...")
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
    log(f"Stap 9: ✓ Frame = '{getattr(tc, 'name', 'page')}'")

    # Rapportage-tab
    log("Stap 10: Rapportage-tab klikken")
    await tc.click("#ICTAB_2")
    await tc.wait_for_selector("#UZFM_WO_WRK_UZ_WO_COMMENT", state="visible", timeout=20_000)
    log("Stap 10: ✓ Rapportage-tab geladen")

    # Commentaar
    commentaar = f"Staalname op {naam} is uitgevoerd"
    log(f"Stap 11: Commentaar ← '{commentaar}'")
    await tc.fill("#UZFM_WO_WRK_UZ_WO_COMMENT", commentaar)
    log("Stap 11: ✓ Commentaar ingevuld")

    # Oplossing aanvinken
    log("Stap 12: Oplossing aanvinken")
    await tc.click("#UZFM_WO_WRK_UZ_WO_OPLOSSING")
    await asyncio.sleep(1)
    log("Stap 12: ✓ Oplossing aangevinkt")

    # Werkuren
    log(f"Stap 13: {len(PS_USERS)} werkuren-gebruiker(s)")
    for idx, user in enumerate(PS_USERS):
        oprid_field = f'[id="UZFM_WO_WERKUUR_OPRID${idx}"]'
        uren_field  = f'[id="UZFM_WO_WERKUUR_UZ_WO_UREN${idx}"]'
        naam_field  = f'[id="UZ_GEBR_HISTVW2_UZ_NAAM_VOLLEDIG${idx}"]'
        if idx > 0:
            nieuwe_lijn_btn = f'[id="UZFM_WO_WERKUUR$new${idx-1}$$0"]'
            log(f"Stap 13: Nieuwe werkurenlijn {idx+1}")
            await tc.click(nieuwe_lijn_btn)
            await asyncio.sleep(2)
            await tc.wait_for_selector(uren_field, state="visible", timeout=15_000)
        if user["oprid"] and user["oprid"] != PS_USER:
            for poging in range(10):
                await tc.click(oprid_field)
                await asyncio.sleep(0.2)
                await tc.keyboard.press("Control+a")
                await tc.keyboard.press("Delete")
                await asyncio.sleep(2)
                await tc.type(oprid_field, user["oprid"], delay=80)
                await tc.keyboard.press("Tab")
                await asyncio.sleep(3)
                try:
                    naam_tekst = await tc.locator(naam_field).inner_text(timeout=5_000)
                    if not user["naam"] or user["naam"].lower() in naam_tekst.lower():
                        log(f"  Lijn {idx+1}: ✓ '{naam_tekst.strip()}'")
                        break
                except Exception:
                    pass
        await asyncio.sleep(1)
        await tc.click(uren_field)
        await asyncio.sleep(0.5)
        await tc.keyboard.press("Control+a")
        await tc.type(uren_field, "1", delay=100)
        await asyncio.sleep(0.5)
        await tc.keyboard.press("Tab")
        await asyncio.sleep(1)
        log(f"  Lijn {idx+1}: ✓ uren ingevuld")

    # Definitief opslaan
    log("Stap 14: Definitief opslaan")
    await page.click('[id="#ICSave"]')
    await asyncio.sleep(2)
    log("Stap 14: Wachten op eindstatus INUITV...")
    eindstatus = "INUITV"
    for poging in range(100):
        await asyncio.sleep(2)
        try:
            st = (await page.locator("#UZFM_WO_UZ_WO_STATUS").inner_text(timeout=5_000)).strip()
            log(f"Stap 14: Status = '{st}' (poging {poging+1})")
            if st == "INUITV":
                log("Stap 14: ✓ INUITV bereikt")
                eindstatus = st
                break
        except Exception as e:
            log(f"Stap 14: Status niet leesbaar: {e}")

    # Sla op in ro_staalname.json geschiedenis
    try:
        import json as _json
        data = _json.loads(RO_DATA_PATH.read_text(encoding="utf-8"))
        data.setdefault("geschiedenis", []).append({
            "datum":  datetime.now().strftime("%Y-%m-%d %H:%M"),
            "naam":   naam,
            "maximo": maximo,
            "locatie": installatie.get("locatie", ""),
            "wo_id":  wo_id,
            "status": eindstatus,
            "ok":     True,
            "fout":   None,
        })
        RO_DATA_PATH.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"✓ Geschiedenis opgeslagen in ro_staalname.json")
    except Exception as e:
        log(f"⚠ Geschiedenis opslaan mislukt: {e}")

    log(f"✓ Nieuwe WO aangemaakt: {wo_id} — status: {eindstatus}")
    return {"ok": True, "wo_id": wo_id, "status": eindstatus, "naam": naam, "maximo": maximo, "fout": None}
