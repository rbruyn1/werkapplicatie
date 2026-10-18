"""
wo_ro_sheets.py — Werkorder aanmaken bij nieuwe ingaven in de RO-formulieren
===========================================================================

De operatoren vullen per waterinstallatie (RO1 465, RO2 465, RO 304) een
Google Formulier in. Elke ingave komt als nieuwe rij in een Google Sheet.
Deze module pollt die sheets (CSV-export) en maakt voor elke NIEUWE rij
automatisch een werkorder in PeopleSoft aan:

    omschrijving : "Aanvullen zout + algemene controle"
    werkuren     : 1 uur op rbruyn1

Veiligheid:
  - Eerste keer dat een sheet gezien wordt = BASELINE: alle bestaande rijen
    worden als 'gezien' gemarkeerd, er wordt géén WO voor de historiek gemaakt.
  - Een rij wordt maar één keer verwerkt (sleutel = Tijdstempel), ook na herstart
    (staat in ro_sheets_staat.json).
  - Bij een mislukte poging wordt NIET blind opnieuw geprobeerd (er kan al een
    half WO bestaan) — de rij wordt als 'fout' bewaard en app.py toont een alert.
  - 'dry_run' (standaard true): enkel loggen wat er zou gebeuren.

Configuratie in config.json:

    "ro_sheets": {
      "enabled": true,
      "dry_run": true,
      "interval_seconden": 300,
      "uitvoerder_oprid": "rbruyn1",
      "uren": "1",
      "taken_toevoegen": true,
      "taak_kolommen": {"0000001963": "ingangsdruk"},
      "installaties": [
        {"naam": "RO1 465", "maximo": "T......", "csv_url": "https://..."},
        ...
      ]
    }

Ontbreekt 'maximo', dan wordt het opgezocht in ro_staalname.json op 'naam'.

Taken (tekstwaarden uit het formulier → taaklijnen op het WO):
  Per TAKEN-lijn hieronder wordt de kolom in de sheet gezocht waarvan de header
  het zoekwoord bevat (hoofdletterongevoelig). Met "taak_kolommen" in config.json
  kan het zoekwoord per TaakID overschreven worden. Lege velden of "nvt" → geen taaklijn.
  "taken_toevoegen": false schakelt dit uit (enkel nog commentaar + werkuren).
"""

import asyncio
import csv
import io
import json
import logging
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

BASIS      = Path(__file__).parent
STAAT_PAD  = BASIS / "ro_sheets_staat.json"
LOG_DIR    = BASIS / "logs"
LOG_PAD    = LOG_DIR / "ro_sheets_wo.log"
RO_DATA    = BASIS / "ro_staalname.json"

OMSCHRIJVING = "Aanvullen zout + algemene controle"

TAAK_TEKST_MAX = 250   # veiligheidsgrens voor het tekstveld in PeopleSoft (werkelijke limiet onbekend)

# TaakID's (PeopleSoft) ↔ kolom in het formulier. Volgorde = volgorde van de taaklijnen.
# 'kolom' = zoekwoord in de header van de sheet (overschrijfbaar via config 'taak_kolommen').
TAKEN = [
    {"id": "0000001962", "label": "Aantal zakken zout",        "kolom": "zakken zout"},
    {"id": "0000001963", "label": "Ingangsdruk",               "kolom": "ingangsdruk"},
    {"id": "0000001964", "label": "Stand waterteller",         "kolom": "waterteller"},
    {"id": "0000001965", "label": "Soft Control",              "kolom": "softcontrol"},
    {"id": "0000001966", "label": "Geleidbaarheid",            "kolom": "geleidbaarheid"},
    {"id": "0000001967", "label": "Controle hittedesinfectie", "kolom": "hittedesinfectie"},
    {"id": "0000001968", "label": "Opmerkingen",               "kolom": "opmerking"},
]

_staat_lock = threading.Lock()
_bezig = threading.Event()   # staat aan zolang een poll-ronde loopt (sheets lezen + WO's aanmaken)


def is_bezig() -> bool:
    """True zolang een poll-ronde loopt. app.py wacht hierop vóór een geplande herstart,
    zodat er geen half WO in PeopleSoft achterblijft."""
    return _bezig.is_set()


# ── Config / staat ───────────────────────────────────────────────────────────
def lees_config() -> dict:
    """Leest de sectie 'ro_sheets' uit config.json (steeds vers)."""
    try:
        with open(BASIS / "config.json", encoding="utf-8") as f:
            return json.load(f).get("ro_sheets", {}) or {}
    except Exception:
        return {}


def _lees_staat() -> dict:
    try:
        with open(STAAT_PAD, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _schrijf_staat(staat: dict) -> None:
    tmp = STAAT_PAD.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(staat, f, ensure_ascii=False, indent=2)
    tmp.replace(STAAT_PAD)


def _schrijf_log(msg: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    regel = f"[{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}] {msg}"
    log.info(f"RO-sheets: {msg}")
    try:
        with open(LOG_PAD, "a", encoding="utf-8") as f:
            f.write(regel + "\n")
    except Exception as e:
        log.error(f"Kon niet naar {LOG_PAD.name} schrijven: {e}")


def _maximo_voor(inst: dict) -> str:
    """T-nummer uit config, anders uit ro_staalname.json (op naam)."""
    m = (inst.get("maximo") or "").strip()
    if m:
        return m
    try:
        with open(RO_DATA, encoding="utf-8") as f:
            for i in json.load(f).get("installaties", []):
                if i.get("naam") == inst.get("naam") and i.get("maximo"):
                    return i["maximo"].strip()
    except Exception:
        pass
    return ""


# ── Sheet ophalen en parsen ──────────────────────────────────────────────────
def _haal_rijen(csv_url: str) -> list[dict]:
    """Haalt de sheet op als CSV en geeft rijen terug (nieuwste eerst, zoals in de sheet).
    Rijen zonder herkenbare Tijdstempel (samenvattingsregels e.d.) worden genegeerd."""
    import requests

    r = requests.get(csv_url, timeout=30)
    r.raise_for_status()
    tekst = r.content.decode("utf-8-sig", errors="replace")
    if tekst.lstrip().lower().startswith("<!doctype html") or "<html" in tekst[:200].lower():
        raise RuntimeError("Geen CSV ontvangen (sheet niet publiek leesbaar of verkeerde URL)")

    lezer = csv.reader(io.StringIO(tekst))
    kop = next(lezer, None)
    if not kop:
        return []
    kop = [k.strip() for k in kop]

    def _compact(t: str) -> str:
        return re.sub(r"\s+", "", t.lower())

    def kol(naam_deel: str):
        """Eerste kolom waarvan de header het zoekwoord bevat (hoofdletter- en spatie-ongevoelig,
        dus 'soft control' vindt ook 'Controle SoftControl')."""
        doel = _compact(naam_deel)
        for i, k in enumerate(kop):
            if doel in _compact(k):
                return i
        return None

    i_ts, i_zout, i_door = kol("tijdstempel"), kol("zakken zout"), kol("registratie door")
    if i_ts is None:
        raise RuntimeError(f"Kolom 'Tijdstempel' niet gevonden in header: {kop[:5]}")

    # Kolom per taak zoeken (zoekwoord uit TAKEN, evt. overschreven in config)
    overschrijf = lees_config().get("taak_kolommen", {}) or {}
    i_taak = {}
    for t in TAKEN:
        zoek = str(overschrijf.get(t["id"], t["kolom"])).strip().lower()
        i_taak[t["id"]] = kol(zoek)
        if i_taak[t["id"]] is None:
            _schrijf_log(f"⚠ Geen kolom voor taak {t['id']} ({t['label']}) — zoekwoord '{zoek}' "
                         f"komt in geen header voor: {kop}")

    ts_patroon = re.compile(r"^\d{1,2}-\d{1,2}-\d{4}\s+\d{1,2}:\d{2}:\d{2}$")
    rijen = []
    for rij in lezer:
        if len(rij) <= i_ts:
            continue
        ts = rij[i_ts].strip()
        if not ts_patroon.match(ts):
            continue
        rijen.append({
            "tijdstempel": ts,
            "zakken_zout": rij[i_zout].strip() if i_zout is not None and len(rij) > i_zout else "",
            "registratie_door": rij[i_door].strip() if i_door is not None and len(rij) > i_door else "",
            "taken": {tid: (rij[i].strip() if i is not None and len(rij) > i else "")
                      for tid, i in i_taak.items()},
        })
    return rijen


def vind_nieuwe_rijen(installatie: dict) -> tuple[list[dict], bool]:
    """
    Geeft (nieuwe_rijen, baseline_gemaakt) terug voor één installatie.
    Bij de allereerste keer wordt enkel de baseline opgeslagen (nieuwe_rijen = []).
    De rijen worden NIET als verwerkt gemarkeerd — dat gebeurt per rij via markeer().
    """
    naam = installatie["naam"]
    rijen = _haal_rijen(installatie["csv_url"])

    with _staat_lock:
        staat = _lees_staat()
        sectie = staat.get(naam)
        if sectie is None:
            staat[naam] = {
                "baseline": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "gezien": [r["tijdstempel"] for r in rijen],
                "resultaten": {},
            }
            _schrijf_staat(staat)
            return [], True
        gezien = set(sectie.get("gezien", []))

    nieuw = [r for r in rijen if r["tijdstempel"] not in gezien]
    nieuw.reverse()   # oudste eerst verwerken
    return nieuw, False


def markeer(naam: str, tijdstempel: str, resultaat: dict) -> None:
    """Rij definitief als gezien markeren en het resultaat bewaren."""
    with _staat_lock:
        staat = _lees_staat()
        sectie = staat.setdefault(naam, {"gezien": [], "resultaten": {}})
        if tijdstempel not in sectie["gezien"]:
            sectie["gezien"].append(tijdstempel)
        sectie.setdefault("resultaten", {})[tijdstempel] = {
            "ok": resultaat.get("ok"),
            "wo_id": resultaat.get("wo_id"),
            "fout": resultaat.get("fout"),
            "dry_run": resultaat.get("dry_run", False),
            "verwerkt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _schrijf_staat(staat)


def bouw_taaklijst(rij: dict) -> list[dict]:
    """Taken met een ingevulde waarde, in TAKEN-volgorde. Lege velden en 'nvt' worden overgeslagen."""
    waarden = rij.get("taken", {}) or {}
    lijst = []
    for t in TAKEN:
        # Alle witruimte (ook regeleinden uit meerregelige opmerkingen) → één spatie:
        # een Enter in het enkelregelige PeopleSoft-veld zou ongewenste acties kunnen starten.
        waarde = " ".join(str(waarden.get(t["id"], "")).split())
        # Leeg of "nvt" (ook "NVT", "n vt", "n.v.t.") → géén taaklijn op het WO
        if not waarde or re.sub(r"[\s.]", "", waarde).lower() == "nvt":
            continue
        if len(waarde) > TAAK_TEKST_MAX:
            _schrijf_log(f"⚠ Taak {t['id']} ({t['label']}): tekst van {len(waarde)} tekens ingekort "
                         f"tot {TAAK_TEKST_MAX}")
            waarde = waarde[:TAAK_TEKST_MAX - 1].rstrip() + "…"
        lijst.append({"id": t["id"], "label": t["label"], "waarde": waarde})
    return lijst


# ── PeopleSoft: WO aanmaken ──────────────────────────────────────────────────
async def maak_wo_ro_controle(page, naam: str, maximo: str, rij: dict,
                              oprid: str, uren: str, naam_volledig: str = "",
                              stap_log=None, taken_toevoegen: bool = True) -> dict:
    """
    Maak en rond een WO af: "Aanvullen zout + algemene controle" met werkuren.
    Zelfde stappen als wo_aanmaken.maak_wo_ccs_container (type AO, bron MAN,
    status UITGEV), maar met T-nummer van de RO-installatie.
    """
    from wo_aanmaken import (WO_NIEUW_URL, wacht_op_element, zoek_win1_frame,
                             vul_werkuren_lijn, vul_taaklijnen_tekst, lees_taak_ids)

    def log_(msg):
        _schrijf_log(f"{naam}: {msg}")
        if stap_log:
            stap_log(msg)

    # Aantal zakken staat in de taaklijnen (0000001962), niet meer in de commentaar
    commentaar = "Zout aangevuld + algemene controle "
    commentaar += f"— ingave formulier {rij['tijdstempel']}"
    if rij.get("registratie_door"):
        commentaar += f" door {rij['registratie_door']}"

    try:
        log_(f"Stap 1: nieuw WO — T-nummer {maximo}")
        await page.goto(WO_NIEUW_URL, wait_until="domcontentloaded", timeout=30_000)

        await wacht_op_element(page, "#PTS_CFG_CL_WRK_PTS_PAGE_TOGGLE", log=log_)
        await page.click("#PTS_CFG_CL_WRK_PTS_PAGE_TOGGLE")
        await asyncio.sleep(3)
        await page.click("#PTS_CFG_CL_WRK_PTS_ADD_BTN")
        await asyncio.sleep(1)

        await wacht_op_element(page, "#UZFM_WO_UZ_OMSCHR254", log=log_)
        await page.fill("#UZFM_WO_UZ_OMSCHR254", OMSCHRIJVING)

        await page.select_option("#UZFM_WO_UZ_WO_TYPE", "AO")
        await asyncio.sleep(2)
        try:
            await page.wait_for_selector("#UZFM_WO_UZ_WO_BRON", state="visible", timeout=10_000)
            await page.select_option("#UZFM_WO_UZ_WO_BRON", value="MAN")
            await asyncio.sleep(3)
        except Exception as e:
            log_(f"⚠ WO-bron instellen mislukt: {e}")

        # Omschrijving kan door de reload gewist zijn
        await wacht_op_element(page, "#UZFM_WO_UZ_OMSCHR254", log=log_)
        if (await page.locator("#UZFM_WO_UZ_OMSCHR254").input_value()).strip() != OMSCHRIJVING:
            await page.fill("#UZFM_WO_UZ_OMSCHR254", OMSCHRIJVING)
            await asyncio.sleep(1)

        # T-nummer
        t_nr_field       = '[id="UZFM_WO_UZ_OBJ_ID$12$"]'
        omschr_div       = "#win0divUZFM_OBJECT_UZ_OMSCHR150"
        uitvoerder_field = '[id="UZFM_WO_UITVOER_OPRID$0"]'
        geladen = False
        for poging in range(10):
            await page.fill(t_nr_field, maximo)
            await page.click(uitvoerder_field)
            await asyncio.sleep(3)
            try:
                tekst = await page.locator(omschr_div).inner_text(timeout=3_000)
                if tekst and tekst.strip():
                    log_(f"Stap 2: ✓ installatie = '{tekst.strip()[:80]}'")
                    geladen = True
                    break
            except Exception:
                pass
            log_(f"Stap 2: omschrijving leeg (poging {poging+1}/10)")
        if not geladen:
            raise RuntimeError(f"T-nummer '{maximo}' werd niet herkend door PeopleSoft")

        # Uitvoerder
        await page.click(uitvoerder_field)
        await asyncio.sleep(0.2)
        await page.keyboard.press("Control+a")
        await page.type(uitvoerder_field, oprid, delay=50)
        await page.keyboard.press("Tab")
        log_("Stap 3: wachten 6s op validatie uitvoerder...")
        await asyncio.sleep(6)

        # Eerste opslaan → INUITV
        await page.click('[id="#ICSave"]')
        await asyncio.sleep(2)
        for poging in range(10):
            await asyncio.sleep(3)
            try:
                st = (await page.locator("#UZFM_WO_UZ_WO_STATUS").inner_text(timeout=5_000)).strip()
                log_(f"Stap 4: status '{st}' (poging {poging+1}/10)")
                if st == "INUITV":
                    break
            except Exception as e:
                log_(f"Stap 4: status niet leesbaar: {e}")
        else:
            raise RuntimeError("Status werd niet INUITV na opslaan (timeout 30s)")

        wo_id = None
        try:
            wo_id = (await page.locator("#UZFM_WO_UZ_WO_ID").inner_text(timeout=5_000)).strip()
        except Exception:
            m = re.search(r"UZ_WO_ID=([^&]+)", page.url)
            if m:
                wo_id = m.group(1)
        if not wo_id:
            raise RuntimeError("WO aangemaakt maar nummer niet gevonden")
        log_(f"Stap 4: ✓ WO-nummer = {wo_id}")

        # Rapportage-tab
        tc = await zoek_win1_frame(page, log=log_)
        for selector in ["#ICPanel2", "#ICTAB_2"]:
            try:
                if await tc.query_selector(selector):
                    await tc.click(selector)
                    break
            except Exception:
                pass
        await tc.wait_for_selector("#UZFM_WO_WRK_UZ_WO_COMMENT", state="visible", timeout=20_000)

        await tc.select_option("#UZFM_WO_WRK_UZ_WO_STAT_WZ_TECH\\$70\\$", "UITGEV")
        await asyncio.sleep(0.5)
        await tc.fill("#UZFM_WO_WRK_UZ_WO_COMMENT", commentaar)
        await tc.click("#UZFM_WO_WRK_UZ_WO_OPLOSSING")
        await asyncio.sleep(1)
        log_(f"Stap 5: ✓ rapportage ingevuld — '{commentaar}'")

        # Werkuren: 1 lijn op oprid
        log_(f"Stap 6: werkuren {uren}u op '{oprid}'")
        await vul_werkuren_lijn(tc, 0, {"oprid": oprid, "naam": naam_volledig},
                                log=log_, uren=uren)

        # Taaklijnen (tekstwaarden uit het formulier)
        taaklijst = bouw_taaklijst(rij) if taken_toevoegen else []
        if taaklijst:
            log_(f"Stap 6b: {len(taaklijst)} taak/taken invullen")
            tres = await vul_taaklijnen_tekst(tc, taaklijst, log=log_)
            if not tres["ok"]:
                raise RuntimeError(f"Taken invullen mislukt: {tres['fout']} — WO {wo_id} NIET afgerond, "
                                   f"nakijken in PeopleSoft")

        # Definitief opslaan → UITGEV
        await page.click('[id="#ICSave"]')
        await asyncio.sleep(2)
        eindstatus = ""
        for poging in range(100):
            await asyncio.sleep(2)
            try:
                eindstatus = (await page.locator("#UZFM_WO_UZ_WO_STATUS").inner_text(timeout=5_000)).strip()
                if eindstatus == "UITGEV":
                    break
            except Exception as e:
                log_(f"Stap 7: status niet leesbaar: {e}")
        log_(f"Stap 7: eindstatus = '{eindstatus}'")
        if eindstatus != "UITGEV":
            raise RuntimeError(f"WO {wo_id} bleef op status '{eindstatus}' i.p.v. UITGEV — nakijken in PeopleSoft")

        if taaklijst:
            tc = await zoek_win1_frame(page, log=log_)
            verwacht = [t["id"] for t in taaklijst]
            gelezen = await lees_taak_ids(tc, len(verwacht))
            log_(f"Stap 8: taken na opslaan = {gelezen}")
            if gelezen != verwacht:
                raise RuntimeError(f"WO {wo_id} UITGEV maar taken niet zoals verwacht "
                                   f"(verwacht {verwacht}, gelezen {gelezen}) — nakijken in PeopleSoft")

        log_(f"✓ WO {wo_id} afgerond")
        return {"ok": True, "wo_id": wo_id, "status": eindstatus, "fout": None}

    except Exception as exc:
        import traceback
        log_(f"❌ {exc}")
        for regel in traceback.format_exc().splitlines():
            _schrijf_log(f"  TRACEBACK: {regel}")
        return {"ok": False, "wo_id": None, "status": None, "fout": str(exc)}


async def _verwerk_batch(taken: list[dict], cfg: dict, stap_log=None) -> None:
    """Eén browser/login voor alle nieuwe rijen. Resultaten worden direct gemarkeerd."""
    from playwright.async_api import async_playwright
    from wo_aanmaken import login, PS_HEADLESS, PS_USERS

    oprid = cfg.get("uitvoerder_oprid", "rbruyn1")
    uren  = str(cfg.get("uren", "1"))
    taken_toevoegen = bool(cfg.get("taken_toevoegen", True))
    naam_volledig = next((u.get("naam", "") for u in PS_USERS if u.get("oprid") == oprid), "")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=PS_HEADLESS)
        context = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="nl-BE")
        page = await context.new_page()
        try:
            if not await login(page, lambda m: _schrijf_log(f"LOGIN: {m}")):
                for t in taken:   # login mislukt → nog niets aangemaakt → NIET markeren, volgende ronde opnieuw
                    t["resultaat"] = {"ok": False, "fout": "Login mislukt", "niet_gemarkeerd": True}
                return
            for t in taken:
                res = await maak_wo_ro_controle(
                    page, t["naam"], t["maximo"], t["rij"], oprid, uren,
                    naam_volledig=naam_volledig, stap_log=stap_log,
                    taken_toevoegen=taken_toevoegen)
                t["resultaat"] = res
                markeer(t["naam"], t["rij"]["tijdstempel"], res)
                await asyncio.sleep(1)
        finally:
            await browser.close()


# ── Eén poll-ronde ───────────────────────────────────────────────────────────
def poll_en_verwerk(on_fout=None) -> dict:
    """Eén ronde (zie _poll_en_verwerk), met 'bezig'-vlag voor een veilige herstart."""
    _bezig.set()
    try:
        return _poll_en_verwerk(on_fout)
    finally:
        _bezig.clear()


def _poll_en_verwerk(on_fout=None) -> dict:
    """
    Eén ronde: alle sheets nakijken en voor nieuwe rijen een WO aanmaken.
    on_fout(naam, rij, fout): callback voor een alert in de app.
    Geeft een samenvatting terug.
    """
    cfg = lees_config()
    samenvatting = {"nieuw": 0, "aangemaakt": 0, "fouten": 0, "baseline": [], "dry_run": False}
    if not cfg.get("enabled"):
        return samenvatting

    dry_run = bool(cfg.get("dry_run", True))
    samenvatting["dry_run"] = dry_run
    taken = []

    for inst in cfg.get("installaties", []):
        naam = inst.get("naam", "?")
        if not inst.get("csv_url"):
            _schrijf_log(f"{naam}: geen csv_url in config — overgeslagen")
            continue
        try:
            nieuw, baseline = vind_nieuwe_rijen(inst)
        except Exception as e:
            _schrijf_log(f"{naam}: ❌ sheet ophalen mislukt: {e}")
            continue
        if baseline:
            samenvatting["baseline"].append(naam)
            _schrijf_log(f"{naam}: baseline opgeslagen — bestaande rijen worden NIET verwerkt")
            continue
        if not nieuw:
            continue

        maximo = _maximo_voor(inst)
        for rij in nieuw:
            samenvatting["nieuw"] += 1
            _schrijf_log(f"{naam}: nieuwe ingave {rij['tijdstempel']} "
                         f"(zout={rij['zakken_zout'] or '?'}, door={rij['registratie_door'] or '?'})")
            if not maximo and dry_run:
                # Niet markeren: zodra het T-nummer bekend is, wordt de ingave alsnog verwerkt.
                _schrijf_log(f"{naam}: ⚠ geen T-nummer (maximo) in config.json of ro_staalname.json "
                             f"— ingave blijft open staan")
                continue
            if not maximo:
                res = {"ok": False, "fout": f"Geen T-nummer (maximo) voor '{naam}' in config.json of ro_staalname.json"}
                markeer(naam, rij["tijdstempel"], res)
                samenvatting["fouten"] += 1
                if on_fout:
                    on_fout(naam, rij, res["fout"])
                continue
            if dry_run:
                _schrijf_log(f"{naam}: DRY-RUN — zou WO '{OMSCHRIJVING}' aanmaken op {maximo} "
                             f"met {cfg.get('uren', '1')}u voor {cfg.get('uitvoerder_oprid', 'rbruyn1')}")
                if cfg.get("taken_toevoegen", True):
                    for t in bouw_taaklijst(rij):
                        _schrijf_log(f"{naam}: DRY-RUN —   taak {t['id']} [{t['label']}] = '{t['waarde']}'")
                markeer(naam, rij["tijdstempel"], {"ok": True, "dry_run": True})
                continue
            taken.append({"naam": naam, "maximo": maximo, "rij": rij})

    if taken:
        try:
            asyncio.run(_verwerk_batch(taken, cfg))
        except Exception as e:
            _schrijf_log(f"❌ Batch-fout: {e}")
            for t in taken:
                if "resultaat" not in t:
                    t["resultaat"] = {"ok": False, "fout": str(e), "niet_gemarkeerd": True}
        for t in taken:
            res = t.get("resultaat", {})
            if res.get("ok"):
                samenvatting["aangemaakt"] += 1
            else:
                samenvatting["fouten"] += 1
                if on_fout:
                    on_fout(t["naam"], t["rij"], res.get("fout"))
    return samenvatting


def start_loop(stop_event: threading.Event, on_fout=None) -> None:
    """Achtergrondthread: poll elke 'interval_seconden' (standaard 300)."""
    cfg = lees_config()
    if not cfg.get("enabled"):
        log.info("RO-sheets: uitgeschakeld (ro_sheets.enabled niet true in config.json)")
        return

    def _loop():
        _schrijf_log(f"Gestart — dry_run={cfg.get('dry_run', True)}, "
                     f"{len(cfg.get('installaties', []))} installatie(s)")
        while not stop_event.is_set():
            try:
                poll_en_verwerk(on_fout=on_fout)
            except Exception as e:
                _schrijf_log(f"❌ Poll-fout: {e}")
            interval = max(60, int(lees_config().get("interval_seconden", 300)))
            stop_event.wait(interval)

    threading.Thread(target=_loop, daemon=True, name="ro-sheets-loop").start()
