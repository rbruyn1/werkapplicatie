"""
wo_toestel_werkorder.py
=======================
Snelle werkorder aanmaken voor een toestel (geen wisselstukken).

Flow:
  1. Login
  2. Nieuw WO-formulier → PTS toggle
  3. WO-type = AO, WO-bron = MAN
  4. Toestel opzoeken via verrekijker (verkortingsnummer → T-nummer via ptModFrame_0)
  5. Omschrijving (probleemmelding, max 50 tekens) invullen
  6. Uitvoerder = rbruyn1
  7. Eerste opslaan → INUITV
  8. Rapportage-tab → oplossing aanvinken → commentaar invullen → uren → status UITGEV
  9. Definitief opslaan → verificatie UITGEV

Beschikbaar voor alle gebruikers.
"""

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

# ── Configuratie ──────────────────────────────────────────────────────────────

def _lees_config() -> dict:
    pad = Path(__file__).parent / "config.json"
    if pad.exists():
        with open(pad, encoding="utf-8") as f:
            return json.load(f)
    return {}

_cfg = _lees_config()

PS_BASE_URL  = _cfg.get("ps_base_url",
    "https://peoplesoftlogistiek.uz.kuleuven.ac.be/psp/FS9PROD/")
PS_PSC_URL   = _cfg.get("ps_psc_url",
    "https://peoplesoftlogistiek.uz.kuleuven.ac.be/psc/FS9PROD/")
PS_HEADLESS  = False
OPRID        = "rbruyn1"

WO_NIEUW_URL = (
    PS_PSC_URL.rstrip("/") + "/"
    + "EMPLOYEE/ERP/c/UZFM_MENU.UZFM_WERKORDER.GBL"
    "?BUSINESS_UNIT=POUZL&UZ_WO_ID=NEXT&PAGE=UZFM_WO_ALG"
)


# ── Playwright helpers ────────────────────────────────────────────────────────

async def _wacht_op_element(page, selector: str, timeout: int = 15_000):
    await page.wait_for_selector(selector, state="visible", timeout=timeout)


async def _zoek_win1_frame(page):
    """Zoekt het TargetContent frame (win0 of win1)."""
    for f in page.frames:
        try:
            if f.name in ("TargetContent", "win0"):
                return f
        except Exception:
            pass
    # Fallback: zoek frame met bekende WO-selector
    for f in page.frames:
        try:
            el = await f.query_selector("#UZFM_WO_WRK_UZ_WO_COMMENT")
            if el:
                return f
        except Exception:
            pass
    return page


# ── Toestel opzoeken via verkortingsnummer ────────────────────────────────────

OBJECT_ZK_URL = (
    PS_PSC_URL.rstrip("/") + "/"
    + "EMPLOYEE/ERP/c/UZFM_MENU.UZFM_OBJECT_ZK.GBL?Folder=MYFAVORITES"
)

OBJECT_ZK_DIENST   = "8"
OBJECT_ZK_VAKGROEP = "14550700"

# Herkent een rechtstreeks ingegeven T-nummer (bv. "T64952", ook met spaties/kleine letter)
_TNUMMER_PATRONN = re.compile(r"^\s*T(\d+)\s*$", re.IGNORECASE)


def _als_tnummer(waarde: str) -> str | None:
    """Geeft het genormaliseerde T-nummer terug ('T64952') als waarde al een T-nummer is, anders None."""
    m = _TNUMMER_PATRONN.match(waarde or "")
    return f"T{m.group(1)}" if m else None


async def zoek_tnummer_via_verkortingsnummer(verkortingsnummer: str, stap_log=None) -> dict:
    """
    Zoek het T-nummer op via de object-zoekpagina (UZFM_OBJECT_ZK).

    Flow:
      1. Login
      2. Navigeer naar UZFM_OBJECT_ZK.GBL
      3. Vul verkortingsnummer, dienst ('8') en vakgroep ('14550700') in
      4. Klik 'Zoek' en wacht tot pagina herladen is
      5. Lees eerste resultaat (link OBJECT$0) → T-nummer
    """
    log_lijnen = []

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        regel = f"[{ts}] TOESTEL-ZOEK: {msg}"
        log_lijnen.append(regel)
        print(regel)
        if stap_log:
            stap_log(msg)

    resultaat = {"t_nummer": "", "omschrijving": "", "ok": False, "log": log_lijnen}

    if not verkortingsnummer:
        log("❌ Geen verkortingsnummer opgegeven")
        return resultaat

    # Als er al rechtstreeks een T-nummer werd ingegeven, hoeft er niet opgezocht te worden
    direct_tnr = _als_tnummer(verkortingsnummer)
    if direct_tnr:
        log(f"'{verkortingsnummer}' is al een T-nummer — opzoeken overgeslagen, gebruik '{direct_tnr}'")
        resultaat["t_nummer"] = direct_tnr
        resultaat["omschrijving"] = ""
        resultaat["ok"] = True
        return resultaat

    log(f"T-nummer zoeken voor verkortingsnummer '{verkortingsnummer}'...")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=PS_HEADLESS)
            context = await browser.new_context()
            page = await context.new_page()

            # ── 1. Login ───────────────────────────────────────────────────
            log("Stap 1: Inloggen...")
            from wo_aanmaken import login
            ok = await login(page, log)
            if not ok:
                log("❌ Login mislukt")
                await browser.close()
                resultaat["fout"] = "Login mislukt"
                return resultaat
            log("Stap 1: ✓ Ingelogd")
            await asyncio.sleep(2)

            # ── 2. Navigeer naar object-zoekpagina ──────────────────────────
            log(f"Stap 2: Navigeren naar {OBJECT_ZK_URL}")
            await page.goto(OBJECT_ZK_URL, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(2)

            # ── 3. Velden invullen ───────────────────────────────────────────
            label_veld   = "#UZFM_OBJ_ZK_WRK_UZ_OBJ_LABEL_ID"
            dienst_veld  = "#UZFM_OBJ_ZK_WRK_UZ_BAS_DIENST"
            vakgroep_veld = "#UZFM_OBJ_ZK_WRK_UZ_VAKGROEP_ID"
            zoek_knop    = "#UZFM_OBJ_ZK_WRK_UZ_ZOEK"

            log(f"Stap 3: Verkortingsnummer '{verkortingsnummer}' invullen...")
            await page.wait_for_selector(label_veld, state="visible", timeout=15_000)
            await page.fill(label_veld, verkortingsnummer)

            log(f"Stap 3: Dienst '{OBJECT_ZK_DIENST}' invullen...")
            try:
                await page.fill(dienst_veld, OBJECT_ZK_DIENST)
            except Exception as e:
                log(f"Stap 3: ⚠ Dienst invullen mislukt: {e}")

            log(f"Stap 3: Vakgroep '{OBJECT_ZK_VAKGROEP}' invullen...")
            try:
                await page.fill(vakgroep_veld, OBJECT_ZK_VAKGROEP)
            except Exception as e:
                log(f"Stap 3: ⚠ Vakgroep invullen mislukt: {e}")

            log("Stap 3: ✓ Velden ingevuld")

            # ── 4. Zoek-knop klikken en wachten op herladen ──────────────────
            log("Stap 4: 'Zoek' klikken...")
            await page.wait_for_selector(zoek_knop, state="visible", timeout=10_000)
            await page.click(zoek_knop)
            await page.wait_for_load_state("domcontentloaded", timeout=20_000)
            await asyncio.sleep(2)
            log("Stap 4: ✓ Zoekresultaten geladen")

            # ── 5. Eerste resultaat lezen (T-nummer) ─────────────────────────
            resultaat_sel = '[id="OBJECT$0"]'
            try:
                await page.wait_for_selector(resultaat_sel, state="visible", timeout=10_000)
                t_nr_tekst = (await page.locator(resultaat_sel).inner_text()).strip()
                log(f"Stap 5: ✓ Eerste resultaat = '{t_nr_tekst}'")
            except Exception as e:
                log(f"Stap 5: ⚠ Geen resultaat voor '{verkortingsnummer}': {e}")
                try:
                    body = await page.locator("body").inner_text()
                    log(f"Stap 5: Pagina-inhoud: {body[:200]}")
                except Exception:
                    pass
                await browser.close()
                resultaat["fout"] = f"Geen toestel gevonden voor '{verkortingsnummer}'"
                return resultaat

            # ── 6. Omschrijving naast resultaat lezen (indien aanwezig) ─────
            omschrijving = ""
            try:
                rij = page.locator(resultaat_sel).locator(
                    "xpath=ancestor::tr[1]")
                omschrijving = (await rij.inner_text(timeout=5_000)).strip()
                # Verwijder het T-nummer zelf uit de omschrijvingstekst indien dubbel
                if t_nr_tekst and omschrijving.startswith(t_nr_tekst):
                    omschrijving = omschrijving[len(t_nr_tekst):].strip(" \t-")
                log(f"Stap 6: Omschrijving = '{omschrijving[:80]}'")
            except Exception as e:
                log(f"Stap 6: Omschrijving niet leesbaar ({e})")

            await browser.close()

            if t_nr_tekst:
                resultaat["t_nummer"]     = t_nr_tekst
                resultaat["omschrijving"] = omschrijving
                resultaat["ok"]           = True
                log(f"✓ T-nummer gevonden: '{t_nr_tekst}' — '{omschrijving[:60]}'")
            else:
                log("⚠ T-nummer leeg na alle stappen")
                resultaat["fout"] = "T-nummer kon niet worden bevestigd"

    except Exception as e:
        log(f"❌ Onverwachte fout: {e}")
        resultaat["fout"] = str(e)

    return resultaat


# ── WO aanmaken ───────────────────────────────────────────────────────────────

async def maak_toestel_werkorder(t_nummer: str, probleemmelding: str,
                                  oplossing: str, uren: str,
                                  stap_log=None) -> dict:
    """
    Maak een snelle WO aan voor een toestel (geen wisselstukken).

    Parameters:
        t_nummer        : T-nummer van het toestel (bv. 'T64952')
        probleemmelding : Korte omschrijving (max 50 tekens)
        oplossing       : Vrije tekst voor rapportage-commentaar
        uren            : Aantal uren als string (bv. '1', '1.5', '2u30')
        stap_log        : Optionele callback voor live logging naar de UI
    """
    log_lijnen = []

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        regel = f"[{ts}] TOESTEL-WO: {msg}"
        log_lijnen.append(regel)
        print(regel)
        if stap_log:
            stap_log(msg)

    resultaat = {"ok": False, "wo_id": None, "log": log_lijnen}

    if not t_nummer:
        log("❌ Geen T-nummer opgegeven")
        resultaat["fout"] = "Geen T-nummer"
        return resultaat

    omschrijving = probleemmelding[:50]
    uren_decimaal = _zet_uren_om(uren)

    log(f"WO aanmaken: T={t_nummer} | '{omschrijving}' | {uren_decimaal}u")

    try:
        from wo_aanmaken import login, wacht_op_element, zoek_win1_frame

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=PS_HEADLESS)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900}, locale="nl-BE"
            )
            page = await context.new_page()

            # ── 1. Login ───────────────────────────────────────────────────
            log("Stap 1: Inloggen...")
            ok = await login(page, log)
            if not ok:
                log("❌ Login mislukt")
                await browser.close()
                resultaat["fout"] = "Login mislukt"
                return resultaat
            log("Stap 1: ✓ Ingelogd")
            await asyncio.sleep(2)

            # ── 2. Nieuw WO ────────────────────────────────────────────────
            log(f"Stap 2: Navigeren naar nieuw WO...")
            await page.goto(WO_NIEUW_URL, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(2)

            # ── 3. PTS toggle ──────────────────────────────────────────────
            log("Stap 3: PTS toggle...")
            await _wacht_op_element(page, "#PTS_CFG_CL_WRK_PTS_PAGE_TOGGLE")
            await page.click("#PTS_CFG_CL_WRK_PTS_PAGE_TOGGLE")
            await asyncio.sleep(3)
            await page.click("#PTS_CFG_CL_WRK_PTS_ADD_BTN")
            await asyncio.sleep(1)
            log("Stap 3: ✓ PTS toggle klaar")

            # ── 4. WO-type = AO ────────────────────────────────────────────
            log("Stap 4: WO-type 'AO'...")
            try:
                await page.wait_for_selector("#UZFM_WO_UZ_WO_TYPE", state="visible", timeout=10_000)
                await page.select_option("#UZFM_WO_UZ_WO_TYPE", value="AO")
                await asyncio.sleep(0.5)
                log("Stap 4: ✓ WO-type 'AO'")
            except Exception as e:
                log(f"Stap 4: ⚠ select_option mislukt ({e}) — fallback")
                await page.click("#UZFM_WO_UZ_WO_TYPE")
                await page.keyboard.press("a")
                await asyncio.sleep(0.3)

            # ── 4b. WO-bron = MAN ─────────────────────────────────────────
            log("Stap 4b: WO-bron 'MAN'...")
            try:
                await page.wait_for_selector("#UZFM_WO_UZ_WO_BRON", state="visible", timeout=10_000)
                await page.select_option("#UZFM_WO_UZ_WO_BRON", value="MAN")
                await asyncio.sleep(0.5)
                log("Stap 4b: ✓ WO-bron 'MAN'")
            except Exception as e:
                log(f"Stap 4b: ⚠ WO-bron mislukt: {e}")

            # ── 5. T-nummer invullen ───────────────────────────────────────
            t_nr_field       = '[id="UZFM_WO_UZ_OBJ_ID$12$"]'
            omschr_div       = "#win0divUZFM_OBJECT_UZ_OMSCHR150"
            uitvoerder_field = '[id="UZFM_WO_UITVOER_OPRID$0"]'

            log(f"Stap 5: T-nummer '{t_nummer}' invullen...")
            for poging in range(10):
                await page.fill(t_nr_field, t_nummer)
                await page.click(uitvoerder_field)
                await asyncio.sleep(3)
                try:
                    tekst = await page.locator(omschr_div).inner_text(timeout=3_000)
                    if tekst and tekst.strip():
                        log(f"Stap 5: ✓ Toestelomschrijving = '{tekst.strip()[:60]}'")
                        break
                    log(f"Stap 5: Omschrijving leeg (poging {poging+1}/10)...")
                except Exception as e:
                    log(f"Stap 5: Exceptie (poging {poging+1}/10): {e}")

            # ── 6. Omschrijving (probleemmelding) invullen ────────────────
            log(f"Stap 6: Omschrijving ← '{omschrijving}'")
            try:
                await _wacht_op_element(page, "#UZFM_WO_UZ_OMSCHR254")
                await page.fill("#UZFM_WO_UZ_OMSCHR254", omschrijving)
                log("Stap 6: ✓ Omschrijving ingevuld")
            except Exception as e:
                log(f"Stap 6: ⚠ Omschrijving mislukt: {e}")

            # ── 7. Uitvoerder = rbruyn1 ────────────────────────────────────
            log(f"Stap 7: Uitvoerder ← '{OPRID}'")
            await page.click(uitvoerder_field)
            await asyncio.sleep(0.2)
            await page.keyboard.press("Control+a")
            await page.type(uitvoerder_field, OPRID, delay=50)
            await page.keyboard.press("Tab")
            await asyncio.sleep(2)
            log("Stap 7: ✓ Uitvoerder ingevuld")

            # ── 8. Eerste opslaan → INUITV ────────────────────────────────
            wo_id = None
            log("Stap 8: Opslaan → wachten op INUITV...")
            await page.click('[id="#ICSave"]')
            await asyncio.sleep(2)

            for poging in range(10):
                await asyncio.sleep(3)
                try:
                    status = (await page.locator("#UZFM_WO_UZ_WO_STATUS")
                              .inner_text(timeout=5_000)).strip()
                    log(f"Stap 8: Status = '{status}' (poging {poging+1})")
                    if status in ("INUITV", "NIEUW", "OPEN"):
                        break
                except Exception as e:
                    log(f"Stap 8: Status niet leesbaar: {e}")

            # WO-ID lezen
            try:
                wo_id_el = await page.query_selector("#UZFM_WO_UZ_WO_ID")
                if wo_id_el:
                    wo_id = (await wo_id_el.inner_text()).strip()
                    if not wo_id:
                        wo_id = (await wo_id_el.get_attribute("value") or "").strip()
            except Exception:
                pass
            if not wo_id:
                m = re.search(r"UZ_WO_ID=(\d+)", page.url)
                if m:
                    wo_id = m.group(1)
            log(f"Stap 8: ✓ WO-ID = '{wo_id}'")

            # ── 9. Rapportage-tab ──────────────────────────────────────────
            log("Stap 9: Rapportage-tab openen...")
            tc = await _zoek_win1_frame(page)
            await tc.click("#ICTAB_2")
            await tc.wait_for_selector("#UZFM_WO_WRK_UZ_WO_COMMENT",
                                       state="visible", timeout=20_000)
            log("Stap 9: ✓ Rapportage-tab geladen")

            # ── 10. Commentaar (oplossing als tekst) ──────────────────────
            log(f"Stap 10: Commentaar ← '{oplossing[:60]}'")
            if len(oplossing) <= 60:
                await tc.fill("#UZFM_WO_WRK_UZ_WO_COMMENT", oplossing)
                log("Stap 10: ✓ Commentaar direct ingevuld")
            else:
                await tc.fill("#UZFM_WO_WRK_UZ_WO_COMMENT", oplossing[:60])
                await asyncio.sleep(0.5)
                log("Stap 10: Tekst >60 tekens — L Omschr popup...")
                await tc.click("#UZFM_WO_WRK_UZ_L_OMSCHR_BTN")
                await asyncio.sleep(2)
                lomschr_frame = None
                for _ in range(15):
                    await asyncio.sleep(1)
                    for f in page.frames:
                        try:
                            el = await f.query_selector("#UZFM_ALG_WORK_UZ_LDESCRPAGE_W")
                            if el:
                                lomschr_frame = f
                                break
                        except Exception:
                            pass
                    if lomschr_frame:
                        break
                if not lomschr_frame:
                    log("Stap 10: ⚠ L Omschr frame niet gevonden — verkorte tekst behouden")
                else:
                    await lomschr_frame.fill("#UZFM_ALG_WORK_UZ_LDESCRPAGE_W", oplossing)
                    await asyncio.sleep(0.5)
                    await lomschr_frame.click('[id="#ICSave"]')
                    await asyncio.sleep(2)
                    log("Stap 10: ✓ Volledige tekst via L Omschr ingevuld")

            # ── 11. Oplossing aanvinken ────────────────────────────────────
            log("Stap 11: Oplossing aanvinken...")
            await tc.click("#UZFM_WO_WRK_UZ_WO_OPLOSSING")
            await asyncio.sleep(1)
            log("Stap 11: ✓ Oplossing aangevinkt")

            # ── 12. Werkuren ───────────────────────────────────────────────
            log(f"Stap 12: Werkuren ← {uren_decimaal}u op {OPRID}")
            uren_field  = '[id="UZFM_WO_WERKUUR_UZ_WO_UREN$0"]'
            oprid_field = '[id="UZFM_WO_WERKUUR_OPRID$0"]'

            # OPRID enkel invullen als het niet de ingelogde user is
            ingelogde_user = _cfg.get("username", "")
            if OPRID != ingelogde_user:
                for poging in range(10):
                    await tc.click(oprid_field)
                    await asyncio.sleep(0.2)
                    await tc.keyboard.press("Control+a")
                    await tc.keyboard.press("Delete")
                    await asyncio.sleep(2)
                    await tc.type(oprid_field, OPRID, delay=80)
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
                log(f"  Ingelogde user = {OPRID} → OPRID overslaan")

            await asyncio.sleep(1)
            await tc.click(uren_field)
            await asyncio.sleep(0.5)
            await tc.keyboard.press("Control+a")
            await tc.type(uren_field, str(uren_decimaal), delay=100)
            await asyncio.sleep(0.5)
            await tc.keyboard.press("Tab")
            await asyncio.sleep(1)
            log("Stap 12: ✓ Werkuren ingevuld")

            # ── 13. Status → UITGEV ────────────────────────────────────────
            log("Stap 13: Status instellen op UITGEV...")
            try:
                await tc.wait_for_selector('[id="UZFM_WO_WRK_UZ_WO_STAT_WZ_TECH$70$"]',
                                           state="visible", timeout=10_000)
                await tc.select_option('[id="UZFM_WO_WRK_UZ_WO_STAT_WZ_TECH$70$"]', value="UITGEV")
                await asyncio.sleep(1)
                log("Stap 13: ✓ Status 'UITGEV' ingesteld")
            except Exception as e:
                log(f"Stap 13: ⚠ Status instellen mislukt: {e}")

            # ── 14. Definitief opslaan ─────────────────────────────────────
            log("Stap 14: Definitief opslaan...")
            await page.click('[id="#ICSave"]')
            await asyncio.sleep(2)

            eindstatus = ""
            for poging in range(15):
                await asyncio.sleep(2)
                try:
                    eindstatus = (await page.locator("#UZFM_WO_UZ_WO_STATUS")
                                  .inner_text(timeout=5_000)).strip()
                    log(f"Stap 14: Status = '{eindstatus}' (poging {poging+1})")
                    if eindstatus == "UITGEV":
                        log("Stap 14: ✓ Eindstatus UITGEV bevestigd")
                        break
                except Exception as e:
                    log(f"Stap 14: Status niet leesbaar: {e}")

            await browser.close()

            resultaat["ok"]       = eindstatus == "UITGEV"
            resultaat["wo_id"]    = wo_id
            resultaat["status"]   = eindstatus
            if not resultaat["ok"]:
                resultaat["fout"] = f"Eindstatus was '{eindstatus}', verwacht 'UITGEV'"
            log(f"{'✓' if resultaat['ok'] else '❌'} WO {wo_id} — eindstatus: '{eindstatus}'")

    except Exception as e:
        log(f"❌ Onverwachte fout: {e}")
        resultaat["fout"] = str(e)

    return resultaat


# ── Hulpfunctie ───────────────────────────────────────────────────────────────

def _zet_uren_om(uren_str: str) -> float:
    """Zet urenformaat om naar decimaal. '2u30' → 2.5, '1:30' → 1.5, '2' → 2.0"""
    if not uren_str:
        return 1.0
    uren_str = uren_str.strip()
    try:
        if "u" in uren_str.lower():
            parts = uren_str.lower().split("u")
        elif ":" in uren_str:
            parts = uren_str.split(":")
        else:
            return float(uren_str.replace(",", "."))
        if len(parts) >= 2:
            return float(parts[0] or 0) + float(parts[1] or 0) / 60
        return float(uren_str)
    except (ValueError, IndexError):
        return 1.0


# ── Synchrone wrappers (voor Flask) ──────────────────────────────────────────

def zoek_tnummer(verkortingsnummer: str, stap_log=None) -> dict:
    return asyncio.run(zoek_tnummer_via_verkortingsnummer(verkortingsnummer, stap_log))


def maak_wo(t_nummer: str, probleemmelding: str, oplossing: str,
            uren: str, stap_log=None) -> dict:
    return asyncio.run(maak_toestel_werkorder(
        t_nummer, probleemmelding, oplossing, uren, stap_log))


# ── Log van aangemaakte snelle werkorders ────────────────────────────────────
# Bijgehouden in snelle_werkorder_log.json: toestelnummer (T-nr), probleemmelding,
# oplossing, datum en het aangemaakte WO-nummer. Enkel succesvolle WO's worden gelogd.

_LOG_PAD = Path(__file__).parent / "snelle_werkorder_log.json"


def tw_log_lezen() -> list[dict]:
    """Geeft alle log-regels terug, laatst aangemaakte eerst."""
    if not _LOG_PAD.exists():
        return []
    try:
        with open(_LOG_PAD, encoding="utf-8") as f:
            regels = json.load(f)
    except Exception:
        return []
    # regels staan in het bestand in aanmaak-volgorde (oudste eerst, want elke
    # tw_log_toevoegen() voegt toe aan het einde) — dus gewoon omdraaien geeft
    # altijd de laatst aangemaakte bovenaan, ook als meerdere WO's binnen
    # dezelfde minuut zijn aangemaakt (datum heeft maar minuut-precisie).
    return list(reversed(regels))


def tw_log_toevoegen(t_nummer: str, probleemmelding: str, oplossing: str, wo_id: str,
                      gebruiker: str = "") -> None:
    """Voeg een regel toe aan het log na een succesvol aangemaakte snelle WO."""
    regels = []
    if _LOG_PAD.exists():
        try:
            with open(_LOG_PAD, encoding="utf-8") as f:
                regels = json.load(f)
        except Exception:
            regels = []
    regels.append({
        "t_nummer":        t_nummer,
        "probleemmelding": probleemmelding,
        "oplossing":       oplossing,
        "wo_id":           wo_id,
        "gebruiker":       gebruiker,
        "datum":           datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    with open(_LOG_PAD, "w", encoding="utf-8") as f:
        json.dump(regels, f, ensure_ascii=False, indent=2)
