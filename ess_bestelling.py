"""
wo_bestelling.py — Automatisch plaatsen van een ESS-aanvraag (bestelling nieuw vat)
=====================================================================================

Wordt getriggerd na het aanmaken van een CCS container-WO (zie app.py
/api/ccs/maak-wo): naast de werkorder ("Container x.x leeg") moet er ook
automatisch een nieuw vat besteld worden via een ESS-aanvraag in PeopleSoft.

Login verloopt identiek aan wo_aanmaken.py (zelfde PS_BASE_URL/PS_USER/PS_PASS,
zelfde login()-flow). De config (incl. ccs_vaten met type + bestelnummer per
container) wordt gelezen uit config.json.

TODO: ESS-pagina URL + formuliervelden invullen zodra de stappen bekend zijn.
"""

import asyncio
import json
import os
import re
from datetime import datetime
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

LOGIN_URL = PS_BASE_URL + "?cmd=login"

# Vaten-mapping (type + bestelnummer per container), bv.
# {"container 1.1": {"type": "K2 Ca 1.25", "bestelnummer": "301383"}, ...}
CCS_VATEN = _cfg.get("ccs_vaten", {})

# ESS-aanvraag pagina (Material Stock Request)
ESS_AANVRAAG_URL = (
    PS_PSC_URL
    + "EMPLOYEE/ERP/c/UZ_ESS.UZ_ESS_REQ.GBL?Folder=MYFAVORITES"
)

# Logbestand voor ESS-bestellingen (append per regel, met timestamp)
LOG_DIR  = Path(__file__).parent / "logs"
LOG_PATH = LOG_DIR / "ess_bestelling.log"


# ── Login (identiek aan wo_aanmaken.login) ──────────────────────────────────
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


# ── Vat-info helper ──────────────────────────────────────────────────────────
def vat_info(container: str) -> dict | None:
    """
    Geeft {"type": "K2 Ca 1.25", "bestelnummer": "301383"} terug voor
    container='container 1.1', of None als onbekend.
    """
    return CCS_VATEN.get(container)


# ── Hulpfuncties ─────────────────────────────────────────────────────────────

async def wacht_op_element(page, selector: str, timeout: int = 15_000, log=None):
    """Wacht tot een element zichtbaar is."""
    if log:
        log(f"  WACHT: op element '{selector}' (max {timeout//1000}s)")
    await page.wait_for_selector(selector, state="visible", timeout=timeout)
    if log:
        log(f"  WACHT: element '{selector}' zichtbaar ✓")


async def zoek_target_frame(page, log=None):
    """Zoek het TargetContent iframe (zelfde patroon als wo_aanmaken.zoek_win1_frame)."""
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

    if log: log("  FRAME: TargetContent niet gevonden via naam → page als fallback")
    return page


# ── ESS-aanvraag aanmaken ────────────────────────────────────────────────────
async def maak_ess_aanvraag(page, container: str, stap_log=None) -> dict:
    """
    Plaats een ESS-aanvraag voor een nieuw vat voor de gegeven container.
    container: bv. 'container 1.1'

    TODO: implementeren zodra de PeopleSoft-stappen gekend zijn:
      1. Navigeer naar ESS-aanvraag pagina (ESS_AANVRAAG_URL)
      2. Vul bestelnummer / artikelnummer in (uit vat_info(container))
      3. Vul aantal, leverdatum, etc. in
      4. Submit / bevestig
      5. Lees aanvraag-/ordernummer uit bevestigingsscherm
    """
    LOG_DIR.mkdir(exist_ok=True)
    logbestand = None

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        regel = f"[{ts}] ESS {container}: {msg}"
        if stap_log:
            stap_log(msg)
        print(regel)
        if logbestand and not logbestand.closed:
            try:
                logbestand.write(regel + "\n")
                logbestand.flush()
            except Exception:
                pass

    try:
        logbestand = open(LOG_PATH, "a", encoding="utf-8")
        logbestand.write(f"\n===== {datetime.now().strftime('%d/%m/%Y %H:%M:%S')} — START ESS aanvraag {container} =====\n")

        info = vat_info(container)
        if not info:
            fout = f"Geen vat-info gevonden voor '{container}' in config.json (ccs_vaten)"
            log(f"❌ {fout}")
            return {"ok": False, "container": container, "fout": fout}

        log(f"ESS aanvraag aanmaken — container='{container}', "
            f"type='{info['type']}', bestelnummer='{info['bestelnummer']}'")

        # ── Stap 1: navigeer naar ESS-aanvraag pagina ──
        log(f"Stap 1: Navigeren naar {ESS_AANVRAAG_URL}")
        await page.goto(ESS_AANVRAAG_URL, wait_until="domcontentloaded", timeout=30_000)
        log(f"Stap 1: Pagina geladen — URL: {page.url[:100]}")

        # ── Stap 2: klik op "Add" ──
        log("Stap 2: Klikken op 'Add' (PTS_CFG_CL_WRK_PTS_ADD_BTN)")
        await page.click("#PTS_CFG_CL_WRK_PTS_ADD_BTN")
        log("Stap 2: Wachten 5s op laden formulier na Add...")
        await asyncio.sleep(5)

        # ── Stap 3: nieuwe frame zoeken + Requester ID invullen ──
        log("Stap 3: TargetContent frame zoeken na 'Add'...")
        frame = await zoek_target_frame(page, log)

        log("Stap 3: Requester ID '1966' invullen in #UZ_ESS_REQ_HDR_REQUESTOR_ID")
        await wacht_op_element(frame, "#UZ_ESS_REQ_HDR_REQUESTOR_ID", log=log)
        await frame.fill("#UZ_ESS_REQ_HDR_REQUESTOR_ID", "1966")
        await frame.press("#UZ_ESS_REQ_HDR_REQUESTOR_ID", "Tab")
        log("Stap 3: Wachten 5s op PS validatie requester ID...")
        await asyncio.sleep(5)

        log("Stap 3: Wachten op auto-fill van requester omschrijving (#REQUESTOR_TBLVW_OPRDEFNDESC)")
        try:
            await frame.wait_for_function(
                """() => {
                    const el = document.getElementById('REQUESTOR_TBLVW_OPRDEFNDESC');
                    return el && el.textContent.trim().length > 0;
                }""",
                timeout=15_000
            )
        except Exception as e:
            log(f"Stap 3: ⚠ Timeout bij wachten op requester omschrijving: {e}")

        requester_desc = (await frame.locator("#REQUESTOR_TBLVW_OPRDEFNDESC").inner_text()).strip()
        log(f"Stap 3: Requester omschrijving = '{requester_desc}'")
        if "CONCENTRA" not in requester_desc.upper():
            log(f"Stap 3: ⚠ Onverwachte requester omschrijving: '{requester_desc}'")

        # ── Stap 4: Artikelnummer (bestelnummer) en aantal invullen ──
        bestelnummer = info["bestelnummer"]
        log(f"Stap 4: Artikelnummer '{bestelnummer}' invullen in #INV_ITEM_ID$0")
        await wacht_op_element(frame, "#INV_ITEM_ID\\$0", log=log)
        await frame.fill("#INV_ITEM_ID\\$0", bestelnummer)
        await frame.press("#INV_ITEM_ID\\$0", "Tab")
        log("Stap 4: Wachten 5s op PS validatie artikelnummer...")
        await asyncio.sleep(5)

        log("Stap 4: Wachten op auto-fill van artikelomschrijving (#DESCR254_MIXED$0)")
        try:
            await frame.wait_for_function(
                """() => {
                    const el = document.getElementById('DESCR254_MIXED$0');
                    return el && el.textContent.trim().length > 0;
                }""",
                timeout=15_000
            )
        except Exception as e:
            log(f"Stap 4: ⚠ Timeout bij wachten op artikelomschrijving: {e}")

        item_desc = (await frame.locator("#DESCR254_MIXED\\$0").inner_text()).strip()
        log(f"Stap 4: Artikelomschrijving = '{item_desc}'")

        # Verifieer dat de "Kx Cax.x" in de omschrijving overeenkomt met het verwachte vat-type
        verwacht_type = info["type"].replace(" ", "").upper()        # bv. "K2CA1.25"
        gevonden_type = re.sub(r"\s+", "", item_desc).upper()
        if verwacht_type not in gevonden_type:
            log(f"Stap 4: ⚠ Artikelomschrijving '{item_desc}' lijkt niet te matchen "
                f"met verwacht type '{info['type']}' voor {container}")
        else:
            log(f"Stap 4: ✓ Artikelomschrijving matcht verwacht type '{info['type']}'")

        log("Stap 4: Aantal '1' invullen in #AANTAL$0")
        await frame.fill("#AANTAL\\$0", "1")

        # ── Stap 5: status "Doorsturen" selecteren ──
        log("Stap 5: Status 'Doorsturen' selecteren (radio $7$)")
        await wacht_op_element(frame, "#UZ_ESS_REQ_HDR_UZ_ESS_STATUS\\$7\\$", log=log)
        await frame.check("#UZ_ESS_REQ_HDR_UZ_ESS_STATUS\\$7\\$")
        log("Stap 5: Wachten 5s op PS herlaad na statuswijziging...")
        await asyncio.sleep(5)

        # Na de radio-klik herlaadt de pagina (submitAction) -> frame opnieuw zoeken
        log("Stap 5: Frame opnieuw zoeken na statuswijziging...")
        frame = await zoek_target_frame(page, log)

        # ── Stap 6: opslaan ──
        log("Stap 6: Klikken op 'Save' ([id=\"#ICSave\"])")
        await wacht_op_element(frame, '[id="#ICSave"]', log=log)
        await frame.click('[id="#ICSave"]')
        log("Stap 6: Wachten 5s na opslaan...")
        await asyncio.sleep(5)

        # Na opslaan herlaadt de pagina opnieuw -> frame opnieuw zoeken
        log("Stap 6: Frame opnieuw zoeken na opslaan...")
        frame = await zoek_target_frame(page, log)

        # ── Stap 7: ESS aanvraagnummer uitlezen ──
        log("Stap 7: Wachten op ESS aanvraagnummer (#UZ_ESS_REQ_HDR_UZ_ESS_REQ_ID)")
        try:
            await frame.wait_for_function(
                """() => {
                    const el = document.getElementById('UZ_ESS_REQ_HDR_UZ_ESS_REQ_ID');
                    return el && el.textContent.trim().length > 0
                        && el.textContent.trim() !== '999999999';
                }""",
                timeout=30_000
            )
        except Exception as e:
            log(f"Stap 7: ⚠ Timeout bij wachten op aanvraagnummer: {e}")

        aanvraag_id = (await frame.locator("#UZ_ESS_REQ_HDR_UZ_ESS_REQ_ID").inner_text()).strip()
        log(f"Stap 7: ESS aanvraagnummer = '{aanvraag_id}'")

        if not aanvraag_id or aanvraag_id == "999999999":
            fout = f"ESS aanvraag opslaan mislukt — aanvraagnummer is nog steeds '{aanvraag_id}'"
            log(f"❌ {fout}")
            return {"ok": False, "container": container, "fout": fout, "vat_info": info}

        log(f"✓ ESS aanvraag succesvol aangemaakt — nummer={aanvraag_id}")
        return {
            "ok": True,
            "container": container,
            "vat_info": info,
            "aanvraag_id": aanvraag_id,
            "fout": None,
        }

    except Exception as exc:
        import traceback
        log(f"❌ FOUT: {exc}")
        for regel in traceback.format_exc().splitlines():
            log(regel)
        return {"ok": False, "container": container, "fout": str(exc)}

    finally:
        if logbestand and not logbestand.closed:
            try:
                logbestand.write(f"===== {datetime.now().strftime('%d/%m/%Y %H:%M:%S')} — EINDE ESS aanvraag {container} =====\n")
                logbestand.close()
            except Exception:
                pass