"""
wo_aanmaken.py
==============
Playwright-scraper die voor elk geselecteerd TD-nummer een werkorder (WO)
aanmaakt in PeopleSoft (exacte poort van de VBA-macro).

Configuratie via environment-variabelen (of .env via python-dotenv):
    PS_BASE_URL     Login basis-URL  (bv. https://peoplesoftlogistiek.../psp/FS9PROD_1)
    PS_PSC_URL      PSC basis-URL    (bv. https://peoplesoftlogistiek.../psc/FS9PROD_1/)
    PS_USER         gebruikersnaam
    PS_PASS         wachtwoord
    PS_USER0        OPRID gebruiker 0 (verplicht, werkuren lijn 1)
    PS_USER0_NAAM   Volledige naam gebruiker 0 (voor verificatie)
    PS_USER1        OPRID gebruiker 1 (optioneel, werkuren lijn 2)
    PS_USER1_NAAM   Volledige naam gebruiker 1
    PS_USER2        OPRID gebruiker 2 (optioneel, werkuren lijn 3)
    PS_USER2_NAAM   Volledige naam gebruiker 2
    PS_HEADLESS     'true'/'false' (default: true)

Aanroep vanuit app.py:
    from wo_aanmaken import verwerk_selectie
    resultaten = asyncio.run(verwerk_selectie([53, 57, 64]))
"""

import asyncio
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

# ── Configuratie ────────────────────────────────────────────────────────────
def _lees_config() -> dict:
    pad = Path(__file__).parent / "config.json"
    if pad.exists():
        with open(pad, encoding="utf-8") as f:
            return json.load(f)
    return {}

_cfg = _lees_config()

PS_BASE_URL = os.getenv("PS_BASE_URL", _cfg.get("ps_base_url_td",
    "https://peoplesoftlogistiek.uz.kuleuven.ac.be/psp/FS9PROD_1"))
PS_PSC_URL  = os.getenv("PS_PSC_URL",  _cfg.get("ps_psc_url",
    "https://peoplesoftlogistiek.uz.kuleuven.ac.be/psc/FS9PROD_1/"))
PS_USER     = os.getenv("PS_USER",     _cfg.get("username", ""))
from crypto_utils import get_ps_password
PS_PASS     = os.getenv("PS_PASS",     get_ps_password(_cfg))
PS_HEADLESS = os.getenv("PS_HEADLESS", "false").lower() == "true"

# Werkuren gebruikers: eerst uit config.json, dan uit env vars
def _lees_werkuren_users() -> list[dict]:
    users_cfg = _cfg.get("werkuren_users", [])
    if users_cfg:
        return [{"oprid": u.get("oprid", ""), "naam": u.get("naam", "")} for u in users_cfg if u.get("oprid")]
    users = []
    for i in range(3):
        oprid = os.getenv(f"PS_USER{i}", "")
        naam  = os.getenv(f"PS_USER{i}_NAAM", "")
        if oprid:
            users.append({"oprid": oprid, "naam": naam})
    return users

PS_USERS = _lees_werkuren_users()

from uitvoerder_utils import bepaal_uitvoerder
UITVOERDER = bepaal_uitvoerder(_cfg, PS_USER)

TD_DATA_PATH = Path(__file__).parent / "thuisdialyse.json"

LOGIN_URL = PS_BASE_URL + "?cmd=login"
WO_NIEUW_URL = (
    PS_PSC_URL
    + "EMPLOYEE/ERP/c/UZFM_MENU.UZFM_WERKORDER.GBL"
    "?BUSINESS_UNIT=POUZL&UZ_WO_ID=NEXT&PAGE=UZFM_WO_ALG"
)


# ── Data helpers ─────────────────────────────────────────────────────────────

def lees_json() -> dict:
    if TD_DATA_PATH.exists():
        with open(TD_DATA_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"toestellen": [], "geschiedenis": []}


def sla_json_op(data: dict):
    with open(TD_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def toestel_info(td_nr: int) -> dict | None:
    return next((t for t in lees_json()["toestellen"] if t["td_nr"] == td_nr), None)


def update_toestel_status(td_nr: int, wo_id: str, status: str):
    data = lees_json()
    for t in data["toestellen"]:
        if t["td_nr"] == td_nr:
            t["laatste_wo"]    = wo_id
            t["laatste_wo_id"] = wo_id
            t["laatste_status"] = status
    sla_json_op(data)


def voeg_geschiedenis_toe(entry: dict):
    data = lees_json()
    data.setdefault("geschiedenis", []).append(entry)
    sla_json_op(data)


# ── Login ────────────────────────────────────────────────────────────────────

async def login(page, log) -> bool:
    """
    Navigeer naar login-pagina, vul credentials in, wacht op EMPLOYEE in URL.
    """
    log(f"LOGIN: Navigeren naar {LOGIN_URL}")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
    log(f"LOGIN: Pagina geladen — URL: {page.url[:80]}")

    log(f"LOGIN: Gebruikersnaam '{PS_USER}' invullen in #userid")
    await page.fill("#userid", PS_USER)

    log("LOGIN: Wachtwoord invullen in #pwd")
    await page.fill("#pwd", PS_PASS)

    log("LOGIN: Klikken op Submit knop")
    await page.click("input[name='Submit']")

    log("LOGIN: Wachten op redirect (EMPLOYEE of errorCode in URL)...")
    try:
        await page.wait_for_url(
            lambda url: "EMPLOYEE" in url or "errorCode" in url,
            timeout=30_000
        )
    except Exception as e:
        log(f"LOGIN: Timeout bij wachten op redirect: {e}")

    log(f"LOGIN: Huidige URL na redirect: {page.url[:100]}")
    if "errorCode" in page.url:
        log("LOGIN: ❌ errorCode gevonden in URL — login mislukt")
        return False
    log("LOGIN: ✓ Geen errorCode — login geslaagd")
    return True


# ── Hulpfuncties ─────────────────────────────────────────────────────────────

async def wacht_op_element(page, selector: str, timeout: int = 15_000, log=None):
    """Wacht tot een element zichtbaar is."""
    if log:
        log(f"  WACHT: op element '{selector}' (max {timeout//1000}s)")
    await page.wait_for_selector(selector, state="visible", timeout=timeout)
    if log:
        log(f"  WACHT: element '{selector}' zichtbaar ✓")


async def zoek_win1_frame(page, log=None):
    """Zoek het TargetContent iframe."""
    if log:
        log("  FRAME: Zoeken naar TargetContent iframe...")
    try:
        frames = page.frames
    except AttributeError:
        if log: log("  FRAME: page heeft geen .frames → page zelf is frame, terugkeren")
        return page

    if log:
        log(f"  FRAME: {len(frames)} frame(s) beschikbaar:")
        for f in frames:
            try:
                log(f"    → name='{f.name}' url='{f.url[:70]}'")
            except Exception:
                log(f"    → (frame info niet leesbaar)")

    for f in frames:
        try:
            if f.name == "TargetContent":
                if log: log(f"  FRAME: TargetContent gevonden ✓")
                return f
        except Exception:
            pass

    if log: log("  FRAME: TargetContent niet gevonden via naam, zoeken via urenveld...")
    for f in frames:
        try:
            el = await f.query_selector('[id="UZFM_WO_WERKUUR_UZ_WO_UREN$0"]')
            if el is not None:
                if log: log(f"  FRAME: Urenveld gevonden in frame '{f.name}' → dit frame gebruiken ✓")
                return f
        except Exception as e:
            if log: log(f"  FRAME: Fout bij zoeken in frame '{f.name}': {e}")

    if log: log("  FRAME: Geen geschikt frame gevonden → page als fallback")
    return page


async def vul_werkuren_lijn(frame, idx: int, user: dict, log, uren: str = "0.5"):
    """
    Vul één werkurenlijn in: OPRID verwijderen (2s wachten) → nieuw OPRID → uren invullen.
    'uren' moet een decimaal getal zijn met een PUNT (bv. "0.75"), geen komma —
    het PeopleSoft-veld verwerkt een komma foutief. Standaard "0.5".
    Elke klik, elke toetsaanslag en elke gelezen waarde wordt gelogd.
    """
    oprid_field = f'[id="UZFM_WO_WERKUUR_OPRID${idx}"]'
    uren_field  = f'[id="UZFM_WO_WERKUUR_UZ_WO_UREN${idx}"]'
    naam_field  = f'[id="UZ_GEBR_HISTVW2_UZ_NAAM_VOLLEDIG${idx}"]'
    lijn_nr     = idx + 1

    # ── Huidige waarden lezen vóór wijziging ─────────────────────────────────
    try:
        huidige_oprid = await frame.locator(oprid_field).input_value(timeout=3_000)
        log(f"  WERKUREN lijn {lijn_nr}: Huidig OPRID in veld = '{huidige_oprid}'")
    except Exception as e:
        log(f"  WERKUREN lijn {lijn_nr}: OPRID veld niet leesbaar ({e})")

    # ── OPRID invullen met naam-verificatie ──────────────────────────────────
    if user["oprid"] and user["oprid"] != PS_USER:
        log(f"  WERKUREN lijn {lijn_nr}: OPRID instellen op '{user['oprid']}' (verwachte naam: '{user.get('naam','?')}')")

        for poging in range(10):
            log(f"  WERKUREN lijn {lijn_nr}: [KLIK] op OPRID-veld '{oprid_field}'")
            await frame.click(oprid_field)
            await asyncio.sleep(0.2)

            log(f"  WERKUREN lijn {lijn_nr}: [TOETS] Ctrl+A → alles selecteren")
            await frame.keyboard.press("Control+a")

            log(f"  WERKUREN lijn {lijn_nr}: [TOETS] Delete → veld wissen")
            await frame.keyboard.press("Delete")

            try:
                waarde_na_wis = await frame.locator(oprid_field).input_value(timeout=2_000)
                log(f"  WERKUREN lijn {lijn_nr}: Veld na wissen = '{waarde_na_wis}'")
            except Exception:
                log(f"  WERKUREN lijn {lijn_nr}: Veld na wissen niet leesbaar")

            log(f"  WERKUREN lijn {lijn_nr}: Wachten 2 seconden voor naam-reset in PeopleSoft...")
            await asyncio.sleep(2)

            # Controleer of naam-veld ook leeg is na 2s
            try:
                naam_na_wis = await frame.locator(naam_field).inner_text(timeout=2_000)
                log(f"  WERKUREN lijn {lijn_nr}: Naam-veld na 2s wachten = '{naam_na_wis.strip()}'")
            except Exception:
                log(f"  WERKUREN lijn {lijn_nr}: Naam-veld niet leesbaar na wachten")

            log(f"  WERKUREN lijn {lijn_nr}: [TYPE] OPRID '{user['oprid']}' ingeven (80ms/karakter)")
            await frame.type(oprid_field, user["oprid"], delay=80)

            log(f"  WERKUREN lijn {lijn_nr}: [TOETS] Tab → veld verlaten (triggert PS validatie)")
            await frame.keyboard.press("Tab")

            log(f"  WERKUREN lijn {lijn_nr}: Wachten 3s op naamsvalidatie door PeopleSoft...")
            await asyncio.sleep(3)

            try:
                naam_tekst = await frame.locator(naam_field).inner_text(timeout=5_000)
                log(f"  WERKUREN lijn {lijn_nr}: Naam-veld gelezen = '{naam_tekst.strip()}'")
                if user["naam"] and user["naam"].lower() in naam_tekst.lower():
                    log(f"  WERKUREN lijn {lijn_nr}: ✓ Naam geverifieerd ('{user['naam']}' gevonden)")
                    break
                elif not user["naam"]:
                    log(f"  WERKUREN lijn {lijn_nr}: Geen verificatienaam geconfigureerd → doorgaan")
                    break
                else:
                    log(f"  WERKUREN lijn {lijn_nr}: ⚠ Naam '{naam_tekst.strip()}' ≠ '{user['naam']}' → poging {poging+1}/10")
            except Exception as e:
                log(f"  WERKUREN lijn {lijn_nr}: Naam-veld exceptie: {e} → poging {poging+1}/10")
    else:
        log(f"  WERKUREN lijn {lijn_nr}: OPRID = ingelogde gebruiker ({PS_USER}) → OPRID veld overslaan")

    # ── Uren invullen ────────────────────────────────────────────────────────
    log(f"  WERKUREN lijn {lijn_nr}: Wachten 1s voor urenveld stabiel is...")
    await asyncio.sleep(1)

    log(f"  WERKUREN lijn {lijn_nr}: [KLIK] op urenveld '{uren_field}'")
    await frame.click(uren_field)
    await asyncio.sleep(0.5)

    # Lees huidige waarde
    try:
        huidige_uren = await frame.locator(uren_field).input_value(timeout=2_000)
        log(f"  WERKUREN lijn {lijn_nr}: Urenveld huidige waarde = '{huidige_uren}'")
    except Exception:
        log(f"  WERKUREN lijn {lijn_nr}: Urenveld waarde niet leesbaar")

    log(f"  WERKUREN lijn {lijn_nr}: [TOETS] Ctrl+A → uren selecteren")
    await frame.keyboard.press("Control+a")

    log(f"  WERKUREN lijn {lijn_nr}: [TYPE] '{uren}' ingeven (100ms/karakter)")
    await frame.type(uren_field, uren, delay=100)
    await asyncio.sleep(0.5)

    log(f"  WERKUREN lijn {lijn_nr}: [TOETS] Tab → uren bevestigen")
    await frame.keyboard.press("Tab")
    await asyncio.sleep(1)

    # ── Verificatie ─────────────────────────────────────────────────────────
    try:
        waarde = await frame.locator(uren_field).input_value(timeout=3_000)
        if waarde == "0.5" or waarde == ".5" or "0.5" in waarde:
            log(f"  WERKUREN lijn {lijn_nr}: ✓ Urenveld bevestigd = '{waarde}'")
        else:
            log(f"  WERKUREN lijn {lijn_nr}: ⚠ Urenveld onverwachte waarde = '{waarde}' (verwacht 0.5)")
    except Exception as e:
        log(f"  WERKUREN lijn {lijn_nr}: Urenveld verificatie mislukt: {e}")


# ── WO aanmaken voor één toestel ─────────────────────────────────────────────

async def maak_wo_voor_toestel(page, td_nr: int, stap_log=None) -> dict:
    toestel = toestel_info(td_nr)

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] TD{td_nr}: {msg}")
        if stap_log: stap_log(msg)

    if not toestel:
        log(f"❌ TD{td_nr} niet gevonden in thuisdialyse.json")
        return {"td_nr": td_nr, "ok": False, "wo_id": None, "status": None,
                "fout": f"TD{td_nr} niet gevonden in thuisdialyse.json"}

    maximo       = toestel.get("maximo", "")
    omschrijving = f"Staalname toestel TD{td_nr}"
    datum_begin  = (datetime.now() - timedelta(days=15)).strftime("%d/%m/%Y")
    datum_einde  = (datetime.now() + timedelta(days=15)).strftime("%d/%m/%Y")

    log(f"Toestelinfo geladen: maximo={maximo}, patient={toestel.get('patient')}, type={toestel.get('toestel_type')}")
    log(f"Datums: begin={datum_begin}, einde={datum_einde}")
    log(f"Omschrijving: '{omschrijving}'")

    try:
        TEST_MODUS = False

        # ── 1. Navigeer naar nieuw WO ─────────────────────────────────────
        log(f"Stap 1: [NAVIGEER] naar nieuw WO formulier")
        log(f"Stap 1: URL = {WO_NIEUW_URL}")
        await page.goto(WO_NIEUW_URL, wait_until="domcontentloaded", timeout=30_000)
        log(f"Stap 1: Pagina geladen — huidige URL: {page.url[:100]}")
        log(f"Stap 1: Paginatitel: '{await page.title()}'")

        # ── 2. PTS toggle ────────────────────────────────────────────────
        log("Stap 2: Wachten op PTS toggle knop '#PTS_CFG_CL_WRK_PTS_PAGE_TOGGLE'...")
        await wacht_op_element(page, "#PTS_CFG_CL_WRK_PTS_PAGE_TOGGLE", log=log)
        log("Stap 2: [KLIK] PTS toggle '#PTS_CFG_CL_WRK_PTS_PAGE_TOGGLE'")
        await page.click("#PTS_CFG_CL_WRK_PTS_PAGE_TOGGLE")
        log("Stap 2: Wachten 3s op vereenvoudigd scherm...")
        await asyncio.sleep(3)
        log("Stap 2: [KLIK] PTS Add knop '#PTS_CFG_CL_WRK_PTS_ADD_BTN'")
        await page.click("#PTS_CFG_CL_WRK_PTS_ADD_BTN")
        await asyncio.sleep(1)
        log("Stap 2: PTS toggle klaar ✓")

        # ── 3. Omschrijving ───────────────────────────────────────────────
        log("Stap 3: Wachten op omschrijvingsveld '#UZFM_WO_UZ_OMSCHR254'...")
        await wacht_op_element(page, "#UZFM_WO_UZ_OMSCHR254", log=log)
        log(f"Stap 3: [FILL] '#UZFM_WO_UZ_OMSCHR254' ← '{omschrijving}'")
        await page.fill("#UZFM_WO_UZ_OMSCHR254", omschrijving)
        # verificatie
        ingevuld = await page.locator("#UZFM_WO_UZ_OMSCHR254").input_value()
        log(f"Stap 3: Veld leest nu: '{ingevuld}' ✓")

        # ── 4. WO-type ────────────────────────────────────────────────────
        log("Stap 4: [KLIK] WO-type veld '#UZFM_WO_UZ_WO_TYPE'")
        await page.click("#UZFM_WO_UZ_WO_TYPE")
        log("Stap 4: [TOETS] 'k'")
        await page.keyboard.press("k")
        log("Stap 4: [TOETS] 'k' (tweede keer)")
        await page.keyboard.press("k")
        log("Stap 4: [KLIK] '#UZFM_WO_UZ_WO_TYPE' nogmaals")
        await page.click("#UZFM_WO_UZ_WO_TYPE")
        await asyncio.sleep(0.2)
        try:
            wo_type_waarde = await page.locator("#UZFM_WO_UZ_WO_TYPE").input_value()
            log(f"Stap 4: WO-type veld = '{wo_type_waarde}' ✓")
        except Exception:
            log("Stap 4: WO-type veld niet leesbaar als input")

        # ── 5. T-nummer invullen ──────────────────────────────────────────
        t_nr_field       = '[id="UZFM_WO_UZ_OBJ_ID$12$"]'
        omschr_div       = "#win0divUZFM_OBJECT_UZ_OMSCHR150"
        uitvoerder_field = '[id="UZFM_WO_UITVOER_OPRID$0"]'

        log(f"Stap 5: T-nummer invullen — veld: {t_nr_field}")
        for poging in range(10):
            log(f"Stap 5: [FILL] {t_nr_field} ← '{maximo}' (poging {poging+1})")
            await page.fill(t_nr_field, maximo)
            log(f"Stap 5: [KLIK] uitvoerder-veld {uitvoerder_field} (tab-effect)")
            await page.click(uitvoerder_field)
            log(f"Stap 5: Wachten 3s op laden omschrijving uit PeopleSoft...")
            await asyncio.sleep(3)
            try:
                tekst = await page.locator(omschr_div).inner_text(timeout=3_000)
                if tekst and tekst.strip():
                    log(f"Stap 5: ✓ Omschrijving geladen = '{tekst.strip()[:80]}'")
                    break
                else:
                    log(f"Stap 5: Omschrijving leeg (poging {poging+1}/10)...")
            except Exception as e:
                log(f"Stap 5: Omschrijving veld exceptie: {e} (poging {poging+1}/10)")

        # ── 6. Uitvoerder ─────────────────────────────────────────────────
        log(f"Stap 6: [KLIK] uitvoerder-veld {uitvoerder_field}")
        await page.click(uitvoerder_field)
        await asyncio.sleep(0.2)
        log("Stap 6: [TOETS] Ctrl+A → veld leegmaken")
        await page.keyboard.press("Control+a")
        log(f"Stap 6: [TYPE] uitvoerder OPRID '{UITVOERDER}' (50ms/karakter)")
        await page.type(uitvoerder_field, UITVOERDER, delay=50)
        log("Stap 6: [TOETS] Tab → bevestigen")
        await page.keyboard.press("Tab")
        log("Stap 6: Wachten 2s op PS validatie uitvoerder...")
        await asyncio.sleep(2)
        try:
            uitv_waarde = await page.locator(uitvoerder_field).input_value()
            log(f"Stap 6: Uitvoerder veld = '{uitv_waarde}' ✓")
        except Exception:
            log("Stap 6: Uitvoerder veld niet leesbaar")

        # ── 7. Eerste opslaan ─────────────────────────────────────────────
        wo_id = None
        if not TEST_MODUS:
            log("Stap 7: [KLIK] Opslaan knop '[id=\"#ICSave\"]'")
            await page.click('[id="#ICSave"]')
            log("Stap 7: Wachten 2s na opslaan...")
            await asyncio.sleep(2)

            log("Stap 7: Wachten op status INUITV (max 30s / 10 pogingen)...")
            for poging in range(10):
                await asyncio.sleep(3)
                try:
                    status_tekst = await page.locator("#UZFM_WO_UZ_WO_STATUS").inner_text(timeout=5_000)
                    log(f"Stap 7: Status = '{status_tekst.strip()}' (poging {poging+1}/10)")
                    if status_tekst.strip() == "INUITV":
                        log("Stap 7: ✓ Status INUITV bereikt")
                        break
                except Exception as e:
                    log(f"Stap 7: Status veld niet leesbaar: {e} (poging {poging+1}/10)")
            else:
                raise RuntimeError("Status werd niet INUITV na opslaan (timeout 30s)")

            log("Stap 7: WO-nummer ophalen uit '#UZFM_WO_UZ_WO_ID'...")
            try:
                wo_id = (await page.locator("#UZFM_WO_UZ_WO_ID").inner_text(timeout=5_000)).strip()
                log(f"Stap 7: WO-nummer via element = '{wo_id}'")
            except Exception as e:
                log(f"Stap 7: Element niet leesbaar ({e}), proberen via URL...")
                m = re.search(r"UZ_WO_ID=([^&]+)", page.url)
                if m:
                    wo_id = m.group(1)
                    log(f"Stap 7: WO-nummer via URL = '{wo_id}'")

            if not wo_id:
                raise RuntimeError("WO aangemaakt maar nummer niet gevonden op pagina of URL")
            log(f"Stap 7: ✓ WO-nummer = {wo_id}")

        # ── 8. Datums invullen ────────────────────────────────────────────
        log("Stap 8: Datums invullen...")

        async def vul_datum(selector: str, waarde: str):
            log(f"  DATUM: Wachten op '{selector}'...")
            await wacht_op_element(page, selector, timeout=15_000, log=log)
            log(f"  DATUM: [KLIK] '{selector}'")
            await page.click(selector)
            await page.keyboard.press("Control+a")
            await asyncio.sleep(0.5)
            log(f"  DATUM: [TYPE] '{waarde}'")
            await page.type(selector, waarde, delay=80)
            await page.keyboard.press("Tab")
            await asyncio.sleep(0.5)
            try:
                ingevuld = await page.locator(selector).input_value()
                log(f"  DATUM: Veld '{selector}' = '{ingevuld}' ✓")
            except Exception:
                log(f"  DATUM: Veld '{selector}' niet leesbaar na invullen")

        log(f"Stap 8: Begindatum '#UZFM_WO_UZ_WO_DT_BEGIN_PLN' ← '{datum_begin}'")
        await vul_datum("#UZFM_WO_UZ_WO_DT_BEGIN_PLN", datum_begin)
        log(f"Stap 8: Einddatum '#UZFM_WO_UZ_WO_DT_EINDE_PLN' ← '{datum_einde}'")
        await vul_datum("#UZFM_WO_UZ_WO_DT_EINDE_PLN", datum_einde)
        log("Stap 8: ✓ Datums ingevuld")

        # ── 8b. TargetContent iframe zoeken ──────────────────────────────
        log("Stap 8b: TargetContent iframe opzoeken voor werkuren/rapportage...")
        tc = await zoek_win1_frame(page, log=log)
        tc_naam = tc.name if hasattr(tc, 'name') else 'PAGE (geen frame)'
        log(f"Stap 8b: ✓ Actief frame = '{tc_naam}'")

        # ── 9. Rapportage-tab ─────────────────────────────────────────────
        log("Stap 9: [KLIK] Rapportage-tab '#ICTAB_2' in frame '{tc_naam}'")
        await tc.click("#ICTAB_2")
        log("Stap 9: Wachten op commentaarveld '#UZFM_WO_WRK_UZ_WO_COMMENT'...")
        await tc.wait_for_selector("#UZFM_WO_WRK_UZ_WO_COMMENT", state="visible", timeout=20_000)
        log("Stap 9: ✓ Rapportage-tab geladen, commentaarveld zichtbaar")

        # ── 10. Commentaar ────────────────────────────────────────────────
        commentaar = f"Staalname op TD{td_nr} is uitgevoerd"
        log(f"Stap 10: [FILL] '#UZFM_WO_WRK_UZ_WO_COMMENT' ← '{commentaar}'")
        await tc.fill("#UZFM_WO_WRK_UZ_WO_COMMENT", commentaar)
        try:
            comm_waarde = await tc.locator("#UZFM_WO_WRK_UZ_WO_COMMENT").input_value()
            log(f"Stap 10: ✓ Commentaar veld = '{comm_waarde}'")
        except Exception:
            log("Stap 10: ✓ Commentaar ingevuld (verificatie niet beschikbaar)")

        # ── 11. Oplossing aanvinken ───────────────────────────────────────
        log("Stap 11: [KLIK] Oplossing checkbox '#UZFM_WO_WRK_UZ_WO_OPLOSSING'")
        await tc.click("#UZFM_WO_WRK_UZ_WO_OPLOSSING")
        await asyncio.sleep(1)
        try:
            is_aangevinkt = await tc.locator("#UZFM_WO_WRK_UZ_WO_OPLOSSING").is_checked()
            log(f"Stap 11: ✓ Oplossing aangevinkt = {is_aangevinkt}")
        except Exception:
            log("Stap 11: ✓ Oplossing aangeklikt (checkbox-status niet leesbaar)")

        # ── 12+. Werkuren ─────────────────────────────────────────────────
        actieve_users = PS_USERS
        log(f"Stap 12: {len(actieve_users)} werkuren-gebruiker(s) verwerken:")
        for i, u in enumerate(actieve_users):
            log(f"  Gebruiker {i+1}: oprid='{u['oprid']}', naam='{u.get('naam','?')}'")

        for idx, user in enumerate(actieve_users):
            stap_nr = 12 + idx
            if idx == 0:
                log(f"Stap {stap_nr}: Werkuren lijn 1 — gebruiker '{user['oprid']}'")
                await vul_werkuren_lijn(tc, 0, user, log=log)
                log(f"Stap {stap_nr}: ✓ Werkuren lijn 1 klaar")
            else:
                nieuwe_lijn_btn = f'[id="UZFM_WO_WERKUUR$new${idx-1}$$0"]'
                log(f"Stap {stap_nr}: [KLIK] Nieuwe werkurenlijn knop '{nieuwe_lijn_btn}'")
                await tc.click(nieuwe_lijn_btn)
                log(f"Stap {stap_nr}: Wachten 2s op nieuwe rij...")
                await asyncio.sleep(2)
                uren_field = f'[id="UZFM_WO_WERKUUR_UZ_WO_UREN${idx}"]'
                log(f"Stap {stap_nr}: Wachten op urenveld '{uren_field}'...")
                await tc.wait_for_selector(uren_field, state="visible", timeout=15_000)
                log(f"Stap {stap_nr}: ✓ Nieuwe rij zichtbaar — werkuren lijn {idx+1} invullen")
                await vul_werkuren_lijn(tc, idx, user, log=log)
                log(f"Stap {stap_nr}: ✓ Werkuren lijn {idx+1} klaar")

        # ── 15. Definitief opslaan ────────────────────────────────────────
        if not TEST_MODUS:
            log("Stap 15: [KLIK] Definitief opslaan '[id=\"#ICSave\"]'")
            await page.click('[id="#ICSave"]')
            log("Stap 15: Wachten 2s na opslaan...")
            await asyncio.sleep(2)
            log("Stap 15: Wachten op eindstatus INUITV (max ~200s / 100 pogingen)...")
            for poging in range(100):
                await asyncio.sleep(2)
                try:
                    status_tekst = await page.locator("#UZFM_WO_UZ_WO_STATUS").inner_text(timeout=5_000)
                    log(f"Stap 15: Status = '{status_tekst.strip()}' (poging {poging+1})")
                    if status_tekst.strip() == "INUITV":
                        log("Stap 15: ✓ Eindstatus INUITV bereikt")
                        break
                except Exception as e:
                    log(f"Stap 15: Status niet leesbaar: {e} (poging {poging+1})")

        eindstatus = "INUITV"
        try:
            eindstatus = (await page.locator("#UZFM_WO_UZ_WO_STATUS").inner_text(timeout=5_000)).strip()
        except Exception:
            pass
        log(f"Stap 16: Eindstatus gelezen = '{eindstatus}'")

        update_toestel_status(td_nr, wo_id, eindstatus)
        voeg_geschiedenis_toe({
            "datum":   datetime.now().strftime("%Y-%m-%d %H:%M"),
            "td_nr":   td_nr,
            "patient": toestel.get("patient"),
            "maximo":  maximo,
            "wo_id":   wo_id,
            "status":  eindstatus,
            "ok":      True,
            "fout":    None,
        })
        log(f"Stap 16: ✓ Resultaat opgeslagen in thuisdialyse.json")

        return {"td_nr": td_nr, "ok": True, "wo_id": wo_id, "status": eindstatus, "fout": None}

    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        fout = str(exc)
        log(f"❌ UITZONDERING: {fout}")
        for regel in tb.splitlines():
            log(f"  TRACEBACK: {regel}")
        voeg_geschiedenis_toe({
            "datum":   datetime.now().strftime("%Y-%m-%d %H:%M"),
            "td_nr":   td_nr,
            "patient": toestel.get("patient") if toestel else None,
            "maximo":  toestel.get("maximo") if toestel else None,
            "wo_id":   None,
            "status":  None,
            "ok":      False,
            "fout":    fout,
        })
        return {"td_nr": td_nr, "ok": False, "wo_id": None, "status": None, "fout": fout}


# ── CCS container vervanging WO ──────────────────────────────────────────────

async def maak_wo_ccs_container(page, container: str, maximo: str, stap_log=None) -> dict:
    """
    Maak een werkorder aan voor een CCS container vervanging.
    container: bv. 'container 1.1'
    maximo:    T-nummer van de CCS installatie (uit config: ccs_maximo)
    """
    LOG_DIR  = Path(__file__).parent / "logs"
    LOG_DIR.mkdir(exist_ok=True)
    LOG_PATH = LOG_DIR / "ccs_wo.log"
    logbestand = open(LOG_PATH, "a", encoding="utf-8")
    logbestand.write(f"\n===== {datetime.now().strftime('%d/%m/%Y %H:%M:%S')} — START CCS WO {container} =====\n")
    logbestand.flush()

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        regel = f"[{ts}] CCS {container}: {msg}"
        print(regel)
        try:
            logbestand.write(regel + "\n")
            logbestand.flush()
        except Exception:
            pass
        if stap_log: stap_log(msg)

    container_nr = container.replace('container ', '').strip()   # bv. "1.1"
    omschrijving = f"Container {container_nr} leeg"
    commentaar   = f"Container {container_nr} werd vervangen"
    datum_begin  = datetime.now().strftime("%d/%m/%Y")
    datum_einde  = (datetime.now() + timedelta(days=30)).strftime("%d/%m/%Y")

    log(f"CCS WO aanmaken — container='{container}', maximo='{maximo}'")
    log(f"Omschrijving: '{omschrijving}'")
    log(f"Commentaar: '{commentaar}'")
    log(f"Datums: begin={datum_begin}, einde={datum_einde}")

    try:
        TEST_MODUS = False

        # ── 1. Navigeer naar nieuw WO ─────────────────────────────────────
        log("Stap 1: [NAVIGEER] naar nieuw WO formulier")
        await page.goto(WO_NIEUW_URL, wait_until="domcontentloaded", timeout=30_000)
        log(f"Stap 1: Pagina geladen — URL: {page.url[:100]}")

        # ── 2. PTS toggle ────────────────────────────────────────────────
        log("Stap 2: Wachten op PTS toggle...")
        await wacht_op_element(page, "#PTS_CFG_CL_WRK_PTS_PAGE_TOGGLE", log=log)
        await page.click("#PTS_CFG_CL_WRK_PTS_PAGE_TOGGLE")
        await asyncio.sleep(3)
        await page.click("#PTS_CFG_CL_WRK_PTS_ADD_BTN")
        await asyncio.sleep(1)
        log("Stap 2: PTS toggle klaar ✓")

        # ── 3. Omschrijving ───────────────────────────────────────────────
        log("Stap 3: Wachten op omschrijvingsveld...")
        await wacht_op_element(page, "#UZFM_WO_UZ_OMSCHR254", log=log)
        log(f"Stap 3: [FILL] omschrijving ← '{omschrijving}'")
        await page.fill("#UZFM_WO_UZ_OMSCHR254", omschrijving)
        ingevuld = await page.locator("#UZFM_WO_UZ_OMSCHR254").input_value()
        log(f"Stap 3: Veld leest nu: '{ingevuld}' ✓")

        # ── 4. WO-type ────────────────────────────────────────────────────
        log("Stap 4: WO-type invullen...")
        await page.select_option("#UZFM_WO_UZ_WO_TYPE", "AO")
        await asyncio.sleep(2)
        log("Stap 4: WO-type klaar ✓")

        # ── 4b. WO-bron = MAN ────────────────────────────────────────────
        log("Stap 4b: WO-bron 'MAN' instellen...")
        try:
            await page.wait_for_selector("#UZFM_WO_UZ_WO_BRON", state="visible", timeout=10_000)
            await page.select_option("#UZFM_WO_UZ_WO_BRON", value="MAN")
            log("Stap 4b: MAN geselecteerd — wachten 3s op PS reload...")
            await asyncio.sleep(3)
            log("Stap 4b: ✓ WO-bron 'MAN' geselecteerd")
        except Exception as e:
            log(f"Stap 4b: ⚠ WO-bron instellen mislukt: {e}")

        # ── 4c. Omschrijving opnieuw invullen (kan gewist zijn door reload) ──
        log("Stap 4c: Omschrijving herbevestigen...")
        await wacht_op_element(page, "#UZFM_WO_UZ_OMSCHR254", log=log)
        huidige_waarde = await page.locator("#UZFM_WO_UZ_OMSCHR254").input_value()
        if huidige_waarde.strip() != omschrijving:
            log(f"Stap 4c: Omschrijving was '{huidige_waarde}', opnieuw invullen ← '{omschrijving}'")
            await page.fill("#UZFM_WO_UZ_OMSCHR254", omschrijving)
            await asyncio.sleep(1)
            ingevuld = await page.locator("#UZFM_WO_UZ_OMSCHR254").input_value()
            log(f"Stap 4c: Veld leest nu: '{ingevuld}' ✓")
        else:
            log("Stap 4c: Omschrijving nog correct ✓")

        # ── 5. T-nummer invullen ──────────────────────────────────────────
        t_nr_field       = '[id="UZFM_WO_UZ_OBJ_ID$12$"]'
        omschr_div       = "#win0divUZFM_OBJECT_UZ_OMSCHR150"
        uitvoerder_field = '[id="UZFM_WO_UITVOER_OPRID$0"]'

        log(f"Stap 5: T-nummer invullen — maximo='{maximo}'")
        for poging in range(10):
            await page.fill(t_nr_field, maximo)
            await page.click(uitvoerder_field)
            await asyncio.sleep(3)
            try:
                tekst = await page.locator(omschr_div).inner_text(timeout=3_000)
                if tekst and tekst.strip():
                    log(f"Stap 5: ✓ Omschrijving geladen = '{tekst.strip()[:80]}'")
                    break
                else:
                    log(f"Stap 5: Omschrijving leeg (poging {poging+1}/10)...")
            except Exception as e:
                log(f"Stap 5: Omschrijving veld exceptie: {e} (poging {poging+1}/10)")

        # ── 6. Uitvoerder ─────────────────────────────────────────────────
        await page.click(uitvoerder_field)
        await asyncio.sleep(0.2)
        await page.keyboard.press("Control+a")
        await page.type(uitvoerder_field, UITVOERDER, delay=50)
        await page.keyboard.press("Tab")
        log("Stap 6: Wachten 6s op PS validatie uitvoerder voor save...")
        await asyncio.sleep(6)
        log("Stap 6: Uitvoerder ingevuld ✓")

        # ── 7. Eerste opslaan ─────────────────────────────────────────────
        wo_id = None
        if not TEST_MODUS:
            log("Stap 7: [KLIK] Save knop '[id=\"#ICSave\"]'")
            await page.click('[id="#ICSave"]')
            await asyncio.sleep(2)
            log("Stap 7: Wachten op status INUITV...")
            for poging in range(10):
                await asyncio.sleep(3)
                try:
                    status_tekst = await page.locator("#UZFM_WO_UZ_WO_STATUS").inner_text(timeout=5_000)
                    log(f"Stap 7: Status = '{status_tekst.strip()}' (poging {poging+1}/10)")
                    if status_tekst.strip() == "INUITV":
                        break
                except Exception as e:
                    log(f"Stap 7: Status niet leesbaar: {e}")
            else:
                raise RuntimeError("Status werd niet INUITV na opslaan (timeout 30s)")

            try:
                wo_id = (await page.locator("#UZFM_WO_UZ_WO_ID").inner_text(timeout=5_000)).strip()
                log(f"Stap 7: WO-nummer = '{wo_id}'")
            except Exception as e:
                m = re.search(r"UZ_WO_ID=([^&]+)", page.url)
                if m:
                    wo_id = m.group(1)
                    log(f"Stap 7: WO-nummer via URL = '{wo_id}'")

            if not wo_id:
                raise RuntimeError("WO aangemaakt maar nummer niet gevonden")
            log(f"Stap 7: ✓ WO-nummer = {wo_id}")

        # ── 8. Datums invullen ────────────────────────────────────────────
        async def vul_datum(selector: str, waarde: str):
            await wacht_op_element(page, selector, timeout=15_000, log=log)
            await page.click(selector)
            await page.keyboard.press("Control+a")
            await asyncio.sleep(0.5)
            await page.type(selector, waarde, delay=80)
            await page.keyboard.press("Tab")
            await asyncio.sleep(0.5)

        log("Stap 8: Datums worden niet ingevuld (standaard behouden)")

        # ── 8b. TargetContent iframe zoeken ──────────────────────────────
        tc = await zoek_win1_frame(page, log=log)
        log("Stap 8b: ✓ Frame gevonden")

        # ── 9. Rapportage-tab ─────────────────────────────────────────────
        for selector in ["#ICPanel2", "#ICTAB_2"]:
            try:
                btn = await tc.query_selector(selector)
                if btn:
                    await tc.click(selector)
                    break
            except Exception:
                pass
        await tc.wait_for_selector("#UZFM_WO_WRK_UZ_WO_COMMENT", state="visible", timeout=20_000)
        log("Stap 9: ✓ Rapportage-tab geladen")

        # ── 10. Status op UITGEV zetten ───────────────────────────────────
        log("Stap 10: Status instellen op UITGEV")
        await tc.select_option("#UZFM_WO_WRK_UZ_WO_STAT_WZ_TECH\\$70\\$", "UITGEV")
        await asyncio.sleep(0.5)
        log("Stap 10: ✓ Status UITGEV geselecteerd")

        # ── 11. Commentaar ────────────────────────────────────────────────
        log(f"Stap 11: [FILL] commentaar ← '{commentaar}'")
        await tc.fill("#UZFM_WO_WRK_UZ_WO_COMMENT", commentaar)
        log(f"Stap 11: ✓ Commentaar ingevuld")

        # ── 12. Oplossing aanvinken ───────────────────────────────────────
        await tc.click("#UZFM_WO_WRK_UZ_WO_OPLOSSING")
        await asyncio.sleep(1)
        log("Stap 12: ✓ Oplossing aangevinkt")

        # ── 13+. Werkuren ─────────────────────────────────────────────────
        actieve_users = PS_USERS
        log(f"Stap 13: {len(actieve_users)} werkuren-gebruiker(s) verwerken")
        for idx, user in enumerate(actieve_users):
            stap_nr = 13 + idx
            if idx == 0:
                log(f"Stap {stap_nr}: Werkuren lijn 1 — '{user['oprid']}'")
                await vul_werkuren_lijn(tc, 0, user, log=log)
                log(f"Stap {stap_nr}: ✓ Werkuren lijn 1 klaar")
            else:
                nieuwe_lijn_btn = f'[id="UZFM_WO_WERKUUR$new${idx-1}$$0"]'
                await tc.click(nieuwe_lijn_btn)
                await asyncio.sleep(2)
                uren_field = f'[id="UZFM_WO_WERKUUR_UZ_WO_UREN${idx}"]'
                await tc.wait_for_selector(uren_field, state="visible", timeout=15_000)
                await vul_werkuren_lijn(tc, idx, user, log=log)
                log(f"Stap {stap_nr}: ✓ Werkuren lijn {idx+1} klaar")

        # ── Definitief opslaan ────────────────────────────────────────────
        laatste_stap = 13 + len(actieve_users)
        if not TEST_MODUS:
            log(f"Stap {laatste_stap}: [KLIK] Definitief opslaan '[id=\"#ICSave\"]'")
            await page.click('[id="#ICSave"]')
            await asyncio.sleep(2)
            log(f"Stap {laatste_stap}: Wachten op eindstatus UITGEV (max ~200s)...")
            for poging in range(100):
                await asyncio.sleep(2)
                try:
                    status_tekst = await page.locator("#UZFM_WO_UZ_WO_STATUS").inner_text(timeout=5_000)
                    log(f"Stap {laatste_stap}: Status = '{status_tekst.strip()}' (poging {poging+1})")
                    if status_tekst.strip() == "UITGEV":
                        log(f"Stap {laatste_stap}: ✓ Eindstatus UITGEV bereikt")
                        break
                except Exception as e:
                    log(f"Stap {laatste_stap}: Status niet leesbaar: {e}")

        eindstatus = "UITGEV"
        try:
            eindstatus = (await page.locator("#UZFM_WO_UZ_WO_STATUS").inner_text(timeout=5_000)).strip()
        except Exception:
            pass
        log(f"Eindstatus = '{eindstatus}'")
        log(f"✓ CCS WO afgerond — wo_id={wo_id}, status={eindstatus}")

        return {"ok": True, "wo_id": wo_id, "status": eindstatus, "container": container, "fout": None}

    except Exception as exc:
        import traceback
        fout = str(exc)
        log(f"❌ UITZONDERING: {fout}")
        for regel in traceback.format_exc().splitlines():
            log(f"  TRACEBACK: {regel}")
        return {"ok": False, "wo_id": None, "status": None, "container": container, "fout": fout}

    finally:
        try:
            logbestand.write(f"===== {datetime.now().strftime('%d/%m/%Y %H:%M:%S')} — EINDE CCS WO {container} =====\n")
            logbestand.close()
        except Exception:
            pass



# ── Hoofd-entry: verwerk hele selectie ───────────────────────────────────────

async def verwerk_selectie(td_nummers: list[int], stap_log=None) -> list[dict]:
    """
    Login eenmalig, verwerk alle TD-nummers sequentieel in dezelfde sessie.
    stap_log(msg): callback — elke regel wordt DIRECT aangeroepen (realtime).
    """
    from playwright.async_api import async_playwright

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        volledig = f"[{ts}] {msg}"
        print(volledig)
        if stap_log: stap_log(msg)

    resultaten = []

    async with async_playwright() as pw:
        log(f"Browser opstarten (headless={PS_HEADLESS})...")
        browser = await pw.chromium.launch(headless=PS_HEADLESS)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="nl-BE",
        )
        page = await context.new_page()
        log("Browser en pagina gereed ✓")

        try:
            succes = await login(page, log)
            if not succes:
                log("❌ Login mislukt — verwerking gestopt")
                for nr in td_nummers:
                    resultaten.append({
                        "td_nr": nr, "ok": False, "wo_id": None,
                        "status": None, "fout": "Login mislukt (errorCode 105)"
                    })
                return resultaten
            log("✓ Ingelogd — starten met verwerking")

            for td_nr in td_nummers:
                log(f"══════ Start TD{td_nr} ({td_nummers.index(td_nr)+1}/{len(td_nummers)}) ══════")

                def td_log(msg, _td_nr=td_nr):
                    stap_log_msg = f"TD{_td_nr}: {msg}"
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"[{ts}] {stap_log_msg}")
                    if stap_log: stap_log(stap_log_msg)

                result = await maak_wo_voor_toestel(page, td_nr, stap_log=td_log)
                resultaten.append(result)
                if result["ok"]:
                    log(f"══════ TD{td_nr} KLAAR: WO {result['wo_id']} — {result['status']} ✓ ══════")
                else:
                    log(f"══════ TD{td_nr} MISLUKT: {result['fout']} ══════")
                await asyncio.sleep(1)

        except Exception as outer_exc:
            import traceback
            log(f"❌ KRITIEKE FOUT: {outer_exc}")
            for regel in traceback.format_exc().splitlines():
                log(f"  {regel}")
            raise
        finally:
            log("Browser afsluiten...")
            await browser.close()
            log("Browser afgesloten ✓")

    return resultaten


# ── CLI voor testen ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Gebruik: python wo_aanmaken.py 57 64 47")
        sys.exit(1)
    nummers = [int(x) for x in sys.argv[1:]]
    resultaten = asyncio.run(verwerk_selectie(nummers))
    for r in resultaten:
        icon = "✅" if r["ok"] else "❌"
        print(f"{icon} TD{r['td_nr']:3d}: {r.get('wo_id') or r.get('fout')}")