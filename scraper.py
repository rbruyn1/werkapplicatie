"""
PeopleSoft Werkorder Scraper
Gebruikt Playwright (geen driver updates nodig!)

Extra: speelt QMusic België af op de achtergrond.
       Detecteert automatisch VLC, mpv, ffplay of Windows Media Player.
       Als de speler vastloopt, wordt hij automatisch herstart.

Gebruik:
  python scraper.py              → scrape éénmalig (geen radio)
  python scraper.py loop         → scrape in loop (geen radio)
  python scraper.py loop --radio → scrape in loop + radio aan
  python scraper.py radio-start  → alleen radio starten (blijft draaien tot Ctrl+C)
  python scraper.py radio-stop   → stopt een lopende radio-instantie (via PID-bestand)
"""

import json
import asyncio
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from crypto_utils import get_ps_password

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.json"
DATA_PATH   = Path(__file__).parent / "data.json"

# Statuskleur mapping (zelfde logica als jouw VBA)
STATUS_COLORS = {
    "GOED":   "#c49500",
    "GESLTN": "#e26b0a",
    "WPLAN":  "#e26b0a",
    "INUITV": "#ffd757",
    "WMATL":  "#f6bb00",
}
TYPE_OVERRIDES = {
    ("GOED",   "PO"): "#009999",
    ("GOED",   "ALM"): "#ff6961",
    ("INUITV", "QA"): "#83c767",
    ("INUITV", "PO"): "#83e867",
    ("GOED",   "QA"): "#cb5c0d",
}

SKIP_LOCATIES = {"UZL-GB 617", "UZL-GB 988 > 404.23.02.02"}
SKIP_TYPES    = {"JO", "WE", "CO"}
PO_SKIP_LOC   = {"UZL-GB 514", "UZL-GB 516", "UZL-GB 910"}
PO_SKIP_OMSCHR = {"CELSEPARATOR", "THERMAX", "PRISMAX", "SPECTRA OPTIA"}


# ---------------------------------------------------------------------------
# QMusic radio speler met automatische speler-detectie en watchdog
# ---------------------------------------------------------------------------

QMUSIC_STREAM_URL = "https://icecast.qmusic.be/qmusic_be.mp3"
RADIO_PID_FILE    = Path(__file__).parent / ".radio.pid"

_radio_process: subprocess.Popen | None = None
_radio_stop_event = threading.Event()
_radio_thread: threading.Thread | None = None


def _detect_player() -> list[str] | None:
    """
    Zoekt de eerste beschikbare mediaspeler op het systeem.
    Geeft de commandoregel terug als lijst, of None als er geen gevonden wordt.

    Volgorde: VLC → mpv → ffplay → Windows Media Player (wmplayer)
    """
    candidates = [
        # VLC (stil, geen GUI)
        (["vlc", "--intf", "dummy", "--no-video", "--quiet", QMUSIC_STREAM_URL], "vlc"),
        # mpv (geen video-venster)
        (["mpv", "--no-video", "--really-quiet", QMUSIC_STREAM_URL], "mpv"),
        # ffplay (onderdeel van ffmpeg)
        (["ffplay", "-nodisp", "-loglevel", "quiet", QMUSIC_STREAM_URL], "ffplay"),
        # Windows Media Player (GUI zichtbaar, maar werkt altijd op Windows)
        (["wmplayer", QMUSIC_STREAM_URL], "wmplayer"),
    ]

    for cmd, exe in candidates:
        if shutil.which(exe):
            log.info(f"Mediaspeler gevonden: {exe}")
            return cmd

    return None


def _spawn_player() -> subprocess.Popen | None:
    """Start de mediaspeler als achtergrondproces."""
    cmd = _detect_player()
    if cmd is None:
        log.error(
            "Geen mediaspeler gevonden (VLC, mpv, ffplay of wmplayer).\n"
            "Installeer er één om QMusic te kunnen afspelen.\n"
            "  VLC:   https://www.videolan.org/\n"
            "  mpv:   https://mpv.io/\n"
            "  ffmpeg: https://ffmpeg.org/"
        )
        return None

    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _radio_watchdog():
    """
    Draait in een aparte thread.
    Controleert elke seconde of stop gevraagd is, en elke 10s of de speler nog leeft.
    """
    global _radio_process

    _radio_process = _spawn_player()
    if _radio_process is None:
        return

    RADIO_PID_FILE.write_text(str(os.getpid()))

    teller = 0
    while not _radio_stop_event.is_set():
        time.sleep(1)
        teller += 1
        if _radio_stop_event.is_set():
            break
        if teller >= 10:
            teller = 0
            if _radio_process.poll() is not None:
                log.warning("Mediaspeler gestopt, herstart QMusic stream...")
                _radio_process = _spawn_player()
                if _radio_process is None:
                    break

    # Onmiddellijk stoppen
    p = _radio_process
    _radio_process = None
    if p and p.poll() is None:
        log.info("QMusic radio stoppen...")
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()

    RADIO_PID_FILE.unlink(missing_ok=True)
    log.info("QMusic radio gestopt")


def start_radio() -> threading.Thread | None:
    """Start de QMusic radio + watchdog. Geeft None als geen speler beschikbaar."""
    global _radio_thread
    # Al bezig? Niets doen.
    if _radio_thread and _radio_thread.is_alive():
        log.info("Radio draait al")
        return _radio_thread
    if _detect_player() is None:
        return None
    _radio_stop_event.clear()
    _radio_thread = threading.Thread(target=_radio_watchdog, daemon=True, name="qmusic-watchdog")
    _radio_thread.start()
    log.info("QMusic watchdog gestart")
    return _radio_thread


def stop_radio():
    """Stopt de radio onmiddellijk — zet het event én doodt het proces direct."""
    global _radio_process
    _radio_stop_event.set()
    # Meteen afsluiten, niet wachten op de watchdog-sleep
    p = _radio_process
    if p and p.poll() is None:
        try:
            p.terminate()
        except Exception:
            pass


def cmd_radio_start():
    """
    Commando: python scraper.py radio-start
    Start de radio en blokkeert tot Ctrl+C.
    """
    log.info("QMusic België starten — druk Ctrl+C om te stoppen")
    t = start_radio()
    if t is None:
        sys.exit(1)
    try:
        while t.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Ctrl+C ontvangen, radio stoppen...")
        stop_radio()
        t.join(timeout=8)


def cmd_radio_stop():
    """
    Commando: python scraper.py radio-stop
    Stuurt een SIGTERM naar het proces dat het PID-bestand heeft aangemaakt.
    Werkt ook als de scraper in een andere terminal draait.
    """
    if not RADIO_PID_FILE.exists():
        print("Geen actieve radio gevonden (geen PID-bestand).")
        return

    pid = int(RADIO_PID_FILE.read_text().strip())
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=True)
        else:
            os.kill(pid, 15)  # SIGTERM
        print(f"Stopsignaal gestuurd naar PID {pid}")
        RADIO_PID_FILE.unlink(missing_ok=True)
    except (ProcessLookupError, subprocess.CalledProcessError):
        print(f"Proces {pid} niet gevonden (al gestopt?)")
        RADIO_PID_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Bestaande scraper logica (ongewijzigd)
# ---------------------------------------------------------------------------

def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def get_row_color(status: str, wo_type: str) -> str:
    override = TYPE_OVERRIDES.get((status, wo_type))
    if override:
        return override
    return STATUS_COLORS.get(status, "#ffffff")


def should_include(wo_type: str, locatie: str, omschrijving: str = "") -> bool:
    if locatie in SKIP_LOCATIES:
        return False
    if wo_type in SKIP_TYPES:
        return False
    if wo_type == "PO" and locatie in PO_SKIP_LOC:
        return False
    if wo_type == "PO" and any(kw in omschrijving.upper() for kw in PO_SKIP_OMSCHR):
        return False
    return wo_type in {"PO", "AO", "QA"}


async def scrape(cfg: dict) -> list[dict]:
    log.info("Playwright scraper gestart")
    werkorders = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel="msedge", args=["--ie-mode-force"])
        page = await browser.new_page()

        # --- Inloggen ---
        log.info("Aanmelden bij PeopleSoft...")
        await page.goto(cfg["ps_base_url"] + "?cmd=login", timeout=30_000)
        await page.fill("#userid", cfg["username"])
        await page.fill("#pwd",    get_ps_password(cfg))
        await page.click("[name='Submit']")

        try:
            await page.wait_for_url("**/EMPLOYEE/**", timeout=60_000)
        except PlaywrightTimeout:
            log.error("Aanmelden mislukt (timeout of verkeerde credentials)")
            await browser.close()
            return None

        if "errorCode" in page.url:
            log.error("Aanmelden mislukt: verkeerde gebruikersnaam of wachtwoord")
            await browser.close()
            return None

        log.info("Aanmelden gelukt!")

        # --- Navigeer naar werkorder zoekscherm ---
        wo_url = (
            cfg["ps_psc_url"]
            + "EMPLOYEE/ERP/c/UZFM_MENU.UZFM_WO_ZK.GBL"
            "?FolderPath=PORTAL_ROOT_OBJECT.UZFM.UZFM_WERKORDER.UZFM_WO_ZK_GBL"
            "&IsFolder=false&PortalHostNode=ERP&NoCrumbs=yes&PortalKeyStruct=yes"
        )
        await page.goto(wo_url, timeout=30_000)

        log.info("Wacht op zoekformulier...")
        try:
            await page.wait_for_selector("#UZFM_WO_ZK_WRK_UZ_OMSCHR254", timeout=30_000)
        except PlaywrightTimeout:
            log.error("Zoekformulier niet geladen")
            await browser.close()
            return []

        # --- Vakgroep invullen en zoeken ---
        await page.fill("#UZFM_WO_ZK_WRK_UZ_VAKGROEP_ID", cfg["vakgroep"])
        await page.click("#UZFM_WO_ZK_WRK_UZ_ZOEK")

        log.info("Wacht op zoekresultaten...")
        try:
            await page.wait_for_selector("#UZFM_WO_ZK_UZ_OMSCHR254\\$0", timeout=30_000)
        except PlaywrightTimeout:
            log.warning("Geen werkorders gevonden")
            await browser.close()
            return []

        # --- Rijen uitlezen ---
        log.info("Werkorders uitlezen...")
        i = 0
        while i < 249:
            status_el = await page.query_selector(f"#UZFM_WO_ZK_UZ_WO_STATUS\\${i}")
            if not status_el:
                break

            async def txt(sel):
                el = await page.query_selector(sel)
                return (await el.inner_text()).strip() if el else ""

            wo_type  = await txt(f"#UZFM_WO_ZK_UZ_WO_TYPE\\${i}")
            locatie  = await txt(f"#UZFM_WO_ZK_UZ_LOCATIE\\${i}")
            status   = await txt(f"#UZFM_WO_ZK_UZ_WO_STATUS\\${i}")
            omschr     = await txt(f"#UZFM_WO_ZK_UZ_OMSCHR254\\${i}")
            omschr150  = await txt(f"#UZFM_WO_ZK_UZ_OMSCHR150\\${i}")
            omschrijving_vol = f"{omschr} - {omschr150}".strip(" -")

            if should_include(wo_type, locatie, omschrijving_vol):
                wo_id      = await txt(f"#UZ_WO_ID\\${i}")
                obj_label  = await txt(f"#UZFM_WO_ZK_UZ_OBJ_LABEL_ID\\${i}")
                begindatum = await txt(f"#UZFM_WO_ZK_BEGINDTTM\\${i}")
                uitvoerder = await txt(f"#UZFM_WO_ZK_UZ_WO_UITVOERDER\\${i}")
                comment    = await txt(f"#UZFM_WO_ZK_UZ_COMMENT_LOG\\${i}")
                obj_id_el  = await page.query_selector(f"#UZ_OBJ_ID\\${i}")
                obj_id     = (await obj_id_el.inner_text()).strip() if obj_id_el else ""

                # Datum formatteren (eerste 10 tekens)
                datum_kort = begindatum[:10] if len(begindatum) >= 10 else begindatum

                werkorders.append({
                    "status":     status,
                    "wo_id":      wo_id,
                    "wo_url":     f"https://peoplesoftlogistiek.uz.kuleuven.ac.be/psp/FS9PROD_1/EMPLOYEE/ERP/c/UZFM_MENU.UZFM_WERKORDER.GBL?Page=UZFM_WO_ALG&Action=U&BUSINESS_UNIT=POUZL&UZ_WO_ID={wo_id}&TargetFrameName=None",
                    "obj_label":  obj_label,
                    "locatie":    locatie,
                    "datum":      datum_kort,
                    "omschrijving": omschrijving_vol,
                    "wo_type":    wo_type,
                    "uitvoerder": f"{uitvoerder} - {comment}".strip(" -"),
                    "obj_id":     obj_id,
                    "obj_url":    f"https://peoplesoftlogistiek.uz.kuleuven.ac.be/psp/FS9PROD_2/EMPLOYEE/ERP/c/UZFM_MENU.UZFM_OBJ_LOGB.GBL?Page=UZFM_OBJ_LOGB&Action=U&ExactKeys=Y&SETID=UZSET&UZ_OBJ_ID={obj_id}&TargetFrameName=None" if obj_id else "",
                    "kleur":      get_row_color(status, wo_type),
                })

            i += 1

        log.info(f"{len(werkorders)} werkorders gevonden")
        await browser.close()

    # Sorteren: type (AO > QA > PO), dan status (INUITV > WMATL > GOED), dan WO-ID
    type_order   = {"AO": 0, "QA": 1, "PO": 2}
    status_order = {"INUITV": 0, "WMATL": 1, "GOED": 2}
    werkorders.sort(key=lambda r: (
        type_order.get(r["wo_type"], 9),
        status_order.get(r["status"], 9),
        r["wo_id"]
    ))

    return werkorders


def save_data(werkorders: list[dict]):
    payload = {
        "laatste_update": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "versie": int(datetime.now().timestamp()),  # uniek getal bij elke scrape
        "werkorders": werkorders,
    }
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log.info(f"Data opgeslagen: {DATA_PATH}")

    # ── Lage Temperatuur WO's automatisch afwerken op di/do/za ───────────────
    try:
        from wo_lage_temperatuur import filter_te_verwerken, verwerk_alle
        if filter_te_verwerken(werkorders):
            def _run():
                try:
                    asyncio.run(verwerk_alle(werkorders))
                except Exception as e:
                    log.error(f"Lage Temperatuur verwerking fout: {e}")
            import threading as _threading
            _threading.Thread(target=_run, daemon=True, name="lage-temp-wo").start()
            log.info("Lage Temperatuur WO verwerking gestart op achtergrond")
    except Exception as e:
        log.error(f"Lage Temperatuur check fout: {e}")

    # ── Melkkoppelingen WO's automatisch afwerken ─────────────────────────────
    try:
        from wo_melkkoppelingen import (filter_te_verwerken as melk_filter,
                                        verwerk_alle as melk_verwerk_alle)
        if melk_filter(werkorders):
            def _run_melk():
                try:
                    asyncio.run(melk_verwerk_alle(werkorders))
                except Exception as e:
                    log.error(f"Melkkoppelingen verwerking fout: {e}")
            import threading as _threading
            _threading.Thread(target=_run_melk, daemon=True, name="melk-wo").start()
            log.info("Melkkoppelingen WO verwerking gestart op achtergrond")
    except Exception as e:
        log.error(f"Melkkoppelingen check fout: {e}")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

async def run_once():
    cfg = load_config()
    werkorders = await scrape(cfg)
    save_data(werkorders)


async def run_loop(with_radio: bool = False, stop_event=None):
    cfg = load_config()
    interval = cfg.get("refresh_interval_minutes", 5) * 60
    start_h, start_m = map(int, cfg.get("active_hours_start", "06:30").split(":"))
    end_h,   end_m   = map(int, cfg.get("active_hours_end",   "17:00").split(":"))

    log.info(f"Automatische loop gestart — elke {cfg.get('refresh_interval_minutes', 5)} min, actief {cfg['active_hours_start']}–{cfg['active_hours_end']}")

    if with_radio:
        start_radio()

    try:
        while True:
            # Stop als de app herstart wordt
            if stop_event and stop_event.is_set():
                log.info("Scraper-loop gestopt (herstart signaal ontvangen)")
                break

            now = datetime.now()
            start = now.replace(hour=start_h, minute=start_m, second=0)
            end   = now.replace(hour=end_h,   minute=end_m,   second=0)

            if start <= now <= end:
                try:
                    werkorders = await scrape(cfg)
                    save_data(werkorders)
                except Exception as e:
                    log.error(f"Fout tijdens scrapen: {e}")
            else:
                log.info(f"Buiten actieve uren ({cfg['active_hours_start']}–{cfg['active_hours_end']}), wacht...")

            # Wacht in kleine stappen zodat stop_event snel opgepikt wordt
            for _ in range(interval):
                if stop_event and stop_event.is_set():
                    break
                await asyncio.sleep(1)
    finally:
        if with_radio:
            stop_radio()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="PeopleSoft Werkorder Scraper + QMusic radio",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commando's:
  (geen)        Scrape éénmalig, geen radio
  loop          Scrape automatisch herhalen (actieve uren uit config.json)
  loop --radio  Zelfde als loop, maar met QMusic radio op de achtergrond
  radio-start   Start alleen de radio (Ctrl+C om te stoppen)
  radio-stop    Stopt een lopende radio-instantie in een andere terminal
        """,
    )
    parser.add_argument(
        "commando",
        nargs="?",
        default="once",
        choices=["once", "loop", "radio-start", "radio-stop"],
        help="Wat moet er gebeuren? (standaard: once)",
    )
    parser.add_argument(
        "--radio",
        action="store_true",
        help="Start de QMusic radio mee (alleen bij 'loop')",
    )
    args = parser.parse_args()

    if args.commando == "radio-start":
        cmd_radio_start()

    elif args.commando == "radio-stop":
        cmd_radio_stop()

    elif args.commando == "loop":
        if args.radio:
            asyncio.run(run_loop(with_radio=True))
        else:
            asyncio.run(run_loop(with_radio=False))

    else:  # once
        if args.radio:
            print("Tip: --radio werkt alleen samen met het 'loop' commando.")
        asyncio.run(run_once())