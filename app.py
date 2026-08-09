"""
Flask webserver voor Werkorder Dashboard + Thuisdialyse Staalnames + RO Staalnames
Start met: python app.py
Bereikbaar op: http://jouw-ip:5000
"""

import json
import asyncio
import threading
import logging
import socket
import os
import sys
import subprocess
import webbrowser
import tempfile
from pathlib import Path

# ──────────────────────────────────────────────────────────────────
# Zorg dat vereiste packages aanwezig zijn (auto-install indien nodig)
# ──────────────────────────────────────────────────────────────────
def _ensure_packages():
    required = {
        "flask": "flask>=3.0",
        "cryptography": "cryptography",
        "playwright": "playwright",
        "openpyxl": "openpyxl",
        "watchdog": "watchdog",
        "dateutil": "python-dateutil",
        "pdfplumber": "pdfplumber",
    }
    missing = []
    for module_name, pip_name in required.items():
        try:
            __import__(module_name)
        except ImportError:
            missing.append(pip_name)

    if missing:
        print(f"Ontbrekende packages worden geïnstalleerd: {', '.join(missing)}")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", *missing],
            check=False,
        )

_ensure_packages()

from flask import Flask, render_template, jsonify, request, redirect, send_from_directory
from datetime import datetime
from crypto_utils import encrypt_password
from scraper import (run_loop, run_once, load_config, scrape, save_data,
                     start_radio, stop_radio, _radio_stop_event)

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_OK = True
except ImportError:
    WATCHDOG_OK = False

    class FileSystemEventHandler:
        pass

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ── Hulpfuncties voor veilig lezen/schrijven van JSON ─────────────────────────

def _schrijf_json_atomisch(pad: Path, data):
    """Schrijft JSON atomisch: eerst naar .tmp, dan rename.
    Zo is het bestand nooit half-geschreven bij een crash.

    Fallback: op een netwerkshare heeft niet elk account Delete-rechten op
    het doelbestand (nodig om te overschrijven via rename — Windows/NTFS
    vereist dit, ook al is Write/Modify wel toegestaan). Als de rename
    faalt met een permissiefout, schrijven we in dat geval rechtstreeks
    (niet-atomisch) naar het doelbestand, zodat schrijven blijft werken
    voor accounts die enkel Write (geen Delete) hebben op dit bestand.
    """
    tmp = pad.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(pad)
    except PermissionError as e:
        log.warning(f"Atomische rename mislukt voor {pad} (permissie — vermoedelijk geen "
                    f"Delete-recht op doelbestand): {e}. Val terug op rechtstreeks schrijven.")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        with open(pad, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _lees_json_veilig(pad: Path, standaard):
    """Leest JSON met fallback naar standaard bij ontbrekend of corrupt bestand."""
    if not pad.exists():
        return standaard
    try:
        with open(pad, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error(f"Corrupt JSON bestand {pad}: {e} — standaard waarde gebruikt")
        return standaard

# ── Schrijf logs ook naar bestand zodat collega's ze live kunnen volgen ──
# Dagelijkse rotatie i.p.v. één eeuwig groeiend bestand: elke nacht om
# middernacht wordt app.log hernoemd naar app.log.YYYY-MM-DD en begint er
# een nieuwe. Aantal bewaarde dagen is instelbaar via config.json
# ("app_log_retentie_dagen"), standaard 5 als dat veld ontbreekt.
# load_config() bestaat op dit punt in het bestand nog niet — config.json
# wordt hier dus rechtstreeks gelezen, met een stille fallback op de
# standaardwaarde bij een ontbrekend/corrupt bestand.
def _lees_log_retentie_dagen(standaard: int = 5) -> int:
    try:
        with open(Path(__file__).parent / "config.json", encoding="utf-8") as f:
            return int(json.load(f).get("app_log_retentie_dagen", standaard))
    except Exception:
        return standaard


def _lees_log_niveau(standaard: str = "WARNING") -> int:
    """Niveau voor het PERSISTENTE logbestand (app.log) - los van wat er in
    een draaiende terminal te zien is. Standaard enkel WARNING/ERROR, zodat
    het bestand niet blijft aangroeien met routinematige INFO-regels. Live
    voortgang tijdens een WO-actie (Playwright-stappen e.d.) loopt via een
    apart, in-memory mechanisme (stap_log/job['stap_logs']) en wordt door
    dit niveau niet beïnvloed. Instelbaar via config.json, veld
    'log_niveau': "DEBUG"/"INFO"/"WARNING"/"ERROR"."""
    try:
        with open(Path(__file__).parent / "config.json", encoding="utf-8") as f:
            naam = str(json.load(f).get("log_niveau", standaard)).upper()
    except Exception:
        naam = standaard
    return getattr(logging, naam, logging.WARNING)


def _lees_debug_modus(standaard: bool = False) -> bool:
    try:
        with open(Path(__file__).parent / "config.json", encoding="utf-8") as f:
            return bool(json.load(f).get("debug_modus", standaard))
    except Exception:
        return standaard

_LOG_DIR = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
from logging.handlers import TimedRotatingFileHandler


class _StilleRotatie(TimedRotatingFileHandler):
    """Als de dagelijkse rotatie (hernoemen naar app.log.YYYY-MM-DD) even
    faalt omdat het bestand op dat moment nog door een ander proces
    vastgehouden wordt (bv. de live-logweergave in het dashboard, of een
    antivirusscan), meld dat dan éénmalig i.p.v. bij elke volgende regel
    opnieuw een volledige traceback te spammen. De rotatie wordt de
    volgende geplande gelegenheid gewoon opnieuw geprobeerd.

    LET OP - twee valkuilen die hier bewust vermeden worden (een eerdere
    versie liep hier tegenaan en veroorzaakte in één dag een logbestand
    van 1GB):
    1. Nooit via het logging-systeem zelf melden (logging.warning(),
       log.error(), ...) binnen deze except-tak: die roept opnieuw DEZE
       handler aan (aangesloten op de root-logger), wat een oneindige
       recursielus geeft. Enkel rechtstreeks naar stderr schrijven, buiten
       'logging' om.
    2. self.rolloverAt bewust zelf vooruitzetten na een mislukte poging.
       De standaardimplementatie berekent dat pas ÁCHTERAF in
       doRollover(), na de (hier mislukkende) hernoem-stap - blijft dat
       staan op het verstreken tijdstip, dan denkt shouldRollover() bij
       *elke volgende regel* opnieuw dat een rotatie nodig is, en probeert
       (en faalt, en recursie) het gewoon telkens opnieuw.
    """
    def doRollover(self):
        try:
            super().doRollover()
        except (PermissionError, OSError) as e:
            import sys
            import time
            print(f"[logrotatie] overgeslagen, bestand tijdelijk vergrendeld: {e}",
                  file=sys.stderr)
            if self.stream is None:
                try:
                    self.stream = self._open()
                except Exception:
                    pass
            self.rolloverAt = self.computeRollover(int(time.time()))


_file_handler = _StilleRotatie(
    _LOG_DIR / "app.log", when="midnight",
    backupCount=_lees_log_retentie_dagen(), encoding="utf-8"
)
_file_handler.setLevel(_lees_log_niveau())
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
class _AppLogFilter(logging.Filter):
    def filter(self, record):
        return "GET /api/logs/lees/app_log" not in record.getMessage()

_file_handler.addFilter(_AppLogFilter())
logging.getLogger().addHandler(_file_handler)

app = Flask(__name__)

# ── Globale JSON error handler: voorkomt HTML 500-pagina's op /api/ routes ──
@app.errorhandler(Exception)
def handle_exception(e):
    if request.path.startswith("/api/"):
        import traceback
        return jsonify({"ok": False, "fout": str(e), "detail": traceback.format_exc()[-500:]}), 500
    raise e

import time as _time
_SERVER_START = str(int(_time.time()))  # unieke token per opstartmoment


@app.route("/api/ping")
def api_ping():
    """Geeft de opstarttijd terug — clients detecteren herstart als deze verandert."""
    return jsonify({"ok": True, "start": _SERVER_START})


@app.route("/assets/<path:filename>")
def static_files(filename):
    return send_from_directory(os.path.join(os.path.dirname(__file__), "templates"), filename)

@app.route("/favicon.ico")
@app.route("/favicon-32x32.png")
@app.route("/favicon-16x16.png")
def favicon():
    naam = request.path.lstrip("/")
    templates_dir = os.path.join(os.path.dirname(__file__), "templates")
    if not os.path.exists(os.path.join(templates_dir, naam)):
        # favicon.ico ontbreekt vaak -> val terug op de 32x32 PNG
        if os.path.exists(os.path.join(templates_dir, "favicon-32x32.png")):
            naam = "favicon-32x32.png"
        else:
            return "", 204
    return send_from_directory(templates_dir, naam)

@app.route("/.well-known/<path:pad>")
def well_known(pad):
    from flask import abort
    abort(404)
app.config["TEMPLATES_AUTO_RELOAD"] = True

DATA_PATH   = Path(__file__).parent / "data.json"
CONFIG_PATH = Path(__file__).parent / "config.json"
LOCK_PATH   = Path(__file__).parent / "scraper.lock"
UPDATE_LOCK_PATH = Path(__file__).parent / "update.lock"
TD_PATH     = Path(__file__).parent / "thuisdialyse.json"
RO_PATH     = Path(__file__).parent / "ro_staalname.json"
DR_PATH     = Path(__file__).parent / "dialyse_resultaten_data.json"
DR_UPLOAD_DIR = Path(__file__).parent / "uploads" / "dialyse_resultaten"
DR_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ESS_BEST_PATH = Path(__file__).parent / "ess_bestellingen.json"

_scraper_stop = threading.Event()


def stuur_matrix_bericht(bericht: str):
    """Verstuurt een Matrix-notificatie. Credentials komen uit config.json
    (velden matrix_url / matrix_token / matrix_room) in plaats van
    hardcoded te staan - dat bestand wordt bewust nooit gecommit
    (zie .gitignore)."""
    try:
        cfg = load_config()
        matrix_url = cfg.get("matrix_url")
        matrix_token = cfg.get("matrix_token")
        matrix_room = cfg.get("matrix_room")
        if not (matrix_url and matrix_token and matrix_room):
            log.warning("Matrix-notificatie overgeslagen: matrix_url/matrix_token/"
                        "matrix_room ontbreken in config.json")
            return
        import time
        import requests as _req
        txn_id = str(int(time.time() * 1000))
        url = (f"{matrix_url}/_matrix/client/r0/rooms/"
               f"{matrix_room}/send/m.room.message/{txn_id}")
        _req.put(url,
                 headers={"Authorization": f"Bearer {matrix_token}"},
                 json={"msgtype": "m.text", "body": bericht},
                 timeout=10, verify=True)
        log.info(f"Matrix bericht verzonden: {bericht}")
    except Exception as e:
        log.error(f"Matrix notificatie mislukt: {e}")


# Enige pc die containerbestellingen (ESS + WO) mag uitvoeren.
# Service rapporten e.d. blijven wel lokaal op elke pc mogelijk.
CONTAINER_BESTELLING_MASTER_PC = "UZLDT11866"


def is_scraper_master() -> bool:
    """
    Bepaal of deze machine de scraper-master is.

    Regels:
    - Lock bestaat NIET → deze machine wordt master (schrijft lock).
    - Lock bestaat WEL, host = deze machine → wij zijn al master (bv. herstart).
    - Lock bestaat WEL, host = andere machine → altijd slave, nooit overnemen.

    Er is geen timeout: de lock blijft geldig zolang het bestand bestaat.
    De master verwijdert de lock bij afsluiten (zie verwijder_lock()).
    """
    hostname = socket.gethostname()
    if hostname.upper() == "UZLDT15044":
        log.info(f"Deze pc ({hostname}) is uitgesloten als scraper-master")
        return False
    if LOCK_PATH.exists():
        try:
            lock        = json.loads(LOCK_PATH.read_text())
            andere_host = lock.get("host", "")
            if andere_host != hostname:
                log.info(f"Scraper-master is {andere_host} — deze pc ({hostname}) leest alleen mee")
                return False
            # Lock staat al op onze naam (herstart) → wij zijn master
        except Exception:
            pass  # Corrupt lock-bestand → hieronder opnieuw aanmaken

    LOCK_PATH.write_text(json.dumps({"host": hostname,
                                      "ping": __import__("time").time()}))
    log.info(f"Deze pc ({hostname}) is scraper-master")
    return True


def verwijder_lock():
    """Verwijder de scraper-lock bij afsluiten zodat een andere machine master kan worden."""
    try:
        if LOCK_PATH.exists():
            lock = json.loads(LOCK_PATH.read_text())
            if lock.get("host", "") == socket.gethostname():
                LOCK_PATH.unlink()
                log.info("Scraper-lock verwijderd")
    except Exception:
        pass


def update_lock_ping():
    """Houdt de ping in de lock actueel — enkel nog voor monitoring/info, niet voor timeout.

    Herkanst hier ook elke minuut het opstarten van de OneDrive-sync.
    start_onedrive_sync() is idempotent (doet niets als de observer al
    actief is), dus dit is goedkoop — maar het zorgt ervoor dat een
    eenmalige opstart-mislukking (bv. een tijdelijke netwerk-hik) zichzelf
    binnen de minuut herstelt, in plaats van te blijven hangen tot iemand
    manueel op "Herstart" klikt.
    """
    hostname = socket.gethostname()
    while True:
        try:
            LOCK_PATH.write_text(json.dumps({"host": hostname,
                                              "ping": __import__("time").time()}))
        except Exception:
            pass
        try:
            from onedrive_sync import start_onedrive_sync
            start_onedrive_sync(load_config())
        except Exception as e:
            log.error(f"OneDrive-sync herkansing mislukt: {e}")
        __import__("time").sleep(60)


def _init_login_status():
    cfg       = load_config()
    has_creds = bool(cfg.get("username")) and cfg.get("username") != "JOU_USERNAME_HIER"
    has_data  = DATA_PATH.exists()
    if has_creds and has_data:
        return {"ok": True, "bericht": f"Aangemeld als {cfg['username']}"}
    return {"ok": None, "bericht": "Aanmelden..."}


login_status = _init_login_status()


def read_data() -> dict:
    if not DATA_PATH.exists():
        return {"laatste_update": "Nog niet geladen", "werkorders": []}
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/werkorders")
def api_werkorders():
    return jsonify(read_data())


@app.route("/api/loginstatus")
def api_loginstatus():
    return jsonify(login_status)


@app.route("/api/current_user")
def api_current_user():
    """Geeft de Windows-gebruiker van deze PC en de OPRID die als
    Uitvoerder wordt gebruikt voor gebruiker-gedreven taken
    (Thuisdialyse, RO staalnames, Service rapporten)."""
    from uitvoerder_utils import bepaal_uitvoerder
    cfg = load_config()
    try:
        import getpass
        win_user = getpass.getuser()
    except Exception:
        win_user = ""
    return jsonify({
        "windows_user": win_user,
        "uitvoerder":   bepaal_uitvoerder(cfg, cfg.get("username", "")),
        "hostname":     socket.gethostname(),
    })



# ── Stop-stubs voor achtergrond-loops ────────────────────────────────────────
# De log-loops zijn daemon-threads zonder extern stop-mechanisme.
# Bij een scraper-herstart (api_herstart) worden ze hier no-op beëindigd;
# start_background_scraper() start ze daarna opnieuw op.
def stop_ccs_log_loop():
    pass  # daemon-thread stopt vanzelf bij volgende GC-cyclus

def stop_error_log_loop():
    pass

def stop_ro_log_watchers():
    pass


@app.route("/api/herstart", methods=["POST"])
def api_herstart():
    """Herstart de scraper en herlaad config.json (zonder app.py te herstarten)."""
    try:
        stop_ccs_log_loop()
        stop_error_log_loop()
        stop_ro_log_watchers()
    except Exception:
        pass
    start_background_scraper()
    log.info("Scraper herstart via dashboard-knop")
    return jsonify({"ok": True, "bericht": "Scraper hergestart — config.json herladen"})


# ── Update vanaf GitHub: check + ophalen + herstart, vanuit de app zelf ────────
# Alternatief voor start.py's bestandswatcher, die soms hinderlijk herstart
# terwijl er net een PeopleSoft-actie loopt. Hier gebeurt niets automatisch:
# controleren mag periodiek (de frontend pollt zelf), maar effectief ophalen
# + herstarten gebeurt enkel na een expliciete klik + bevestiging.

def _git_pad():
    return str(Path(__file__).parent)


def _git_commit(ref):
    try:
        r = subprocess.run(["git", "rev-parse", ref], cwd=_git_pad(),
                            capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


@app.route("/api/update-check")
def api_update_check():
    """Doet een 'git fetch' (raakt de working tree niet aan) en vergelijkt de
    lokale HEAD met origin/main. Veilig om vanuit meerdere pc's tegelijk
    (elk met hun eigen proces op de gedeelde Z:-map) te pollen."""
    lokaal = _git_commit("HEAD")
    try:
        subprocess.run(["git", "fetch", "origin", "--quiet"], cwd=_git_pad(),
                        capture_output=True, text=True, timeout=20)
    except Exception as e:
        return jsonify({"ok": False, "fout": f"git fetch mislukt: {e}"})
    nieuwste = _git_commit("origin/main")

    if lokaal is None or nieuwste is None:
        return jsonify({"ok": False, "fout": "Kon git-status niet bepalen "
                                              "(geen git-repo, of git niet gevonden)."})
    return jsonify({
        "ok": True,
        "update_beschikbaar": lokaal != nieuwste,
        "huidig": lokaal[:7],
        "nieuwste": nieuwste[:7],
    })


def _herstart_zelf():
    """Vervangt het huidige Python-proces door een verse start met dezelfde
    argumenten (dus 'python start.py' blijft 'python start.py', 'python
    app.py' blijft 'python app.py') - zo laadt de nieuwe code effectief,
    zonder dat iemand het venster zelf moet sluiten en heropenen."""
    try:
        verwijder_lock()
    except Exception:
        pass
    log.info("Herstart nu na update (os.execv)...")
    python = sys.executable
    os.execv(python, [python] + sys.argv)


@app.route("/api/update-nu", methods=["POST"])
def api_update_nu():
    """Haalt de update op (git pull) en herstart het proces. Enkel via een
    expliciete gebruikersactie - nooit automatisch getriggerd."""
    hostname = socket.gethostname()

    # Simpele, kortlevende lock om te vermijden dat twee pc's op dezelfde
    # gedeelde Z:-map tegelijk 'git pull' doen (kan tot een git-lockconflict
    # leiden). Een lock ouder dan 30s wordt genegeerd - beschermt tegen een
    # vastgelopen/gecrashte poging die de lock nooit opruimde.
    if UPDATE_LOCK_PATH.exists():
        try:
            lock = json.loads(UPDATE_LOCK_PATH.read_text())
            leeftijd = __import__("time").time() - lock.get("tijd", 0)
            if leeftijd < 30 and lock.get("host") != hostname:
                return jsonify({"ok": False,
                                 "fout": f"{lock.get('host')} is net aan het bijwerken — "
                                         f"probeer over een halve minuut opnieuw."}), 409
        except Exception:
            pass
    UPDATE_LOCK_PATH.write_text(json.dumps({"host": hostname, "tijd": __import__("time").time()}))

    try:
        r = subprocess.run(["git", "pull", "origin", "main"], cwd=_git_pad(),
                            capture_output=True, text=True, timeout=60)
    except Exception as e:
        try:
            UPDATE_LOCK_PATH.unlink()
        except Exception:
            pass
        return jsonify({"ok": False, "fout": f"git pull mislukt: {e}"}), 500

    try:
        UPDATE_LOCK_PATH.unlink()
    except Exception:
        pass

    if r.returncode != 0:
        return jsonify({"ok": False, "fout": f"git pull gaf een fout terug: {r.stderr[-500:]}"}), 500

    log.info(f"Update opgehaald door {hostname}: {r.stdout.strip()}")
    # Herstart pas een fractie NA het versturen van deze response, anders
    # krijgt de browser geen bevestiging meer te zien (verbinding valt weg
    # zodra os.execv het huidige proces vervangt).
    threading.Timer(1.0, _herstart_zelf).start()
    return jsonify({"ok": True, "bericht": "Update opgehaald — app herstart nu..."})


@app.route("/api/login", methods=["POST"])
def api_login():
    data     = request.get_json()
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return jsonify({"ok": False,
                        "bericht": "Gebruikersnaam en wachtwoord zijn verplicht"}), 400
    cfg = load_config()
    cfg["username"] = username
    cfg["password_enc"] = encrypt_password(password)
    cfg.pop("password", None)  # verwijder eventueel oud klare-tekst wachtwoord
    save_config(cfg)
    start_background_scraper()
    return jsonify({"ok": True, "bericht": "Credentials opgeslagen, aanmelden gestart..."})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    def _run():
        asyncio.run(run_once())
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "gestart"})


def start_background_scraper():
    global login_status, _scraper_stop
    _scraper_stop.set()
    _scraper_stop = threading.Event()
    stop_event    = _scraper_stop

    cfg = load_config()
    if not cfg.get("username") or cfg.get("username") == "JOU_USERNAME_HIER":
        login_status = {"ok": False, "bericht": "Geen credentials ingesteld"}
        return
    if not is_scraper_master():
        master_host = ""
        try:
            master_host = json.loads(LOCK_PATH.read_text()).get("host", "")
        except Exception:
            pass
        bericht = f"Leesmodus (scraper op {master_host})" if master_host else "Leesmodus (scraper op andere pc)"
        login_status = {"ok": True, "bericht": bericht}
        return
    threading.Thread(target=update_lock_ping, daemon=True, name="lock-ping").start()
    try:
        from onedrive_sync import start_onedrive_sync
        start_onedrive_sync(cfg)
    except Exception as e:
        log.error(f"OneDrive-sync kon niet gestart worden: {e}")
    login_status = {"ok": None, "bericht": "Aanmelden..."}

    def _loop():
        global login_status
        try:
            werkorders = asyncio.run(scrape(cfg))
            if werkorders is None:
                login_status = {
                    "ok": False,
                    "bericht": "Aanmelden mislukt — controleer credentials via het dashboard",
                }
                return
            save_data(werkorders)
            login_status = {"ok": True, "bericht": f"Aangemeld als {cfg['username']}"}
        except Exception as e:
            login_status = {"ok": False, "bericht": f"Fout bij aanmelden: {e}"}
            return
        if not stop_event.is_set():
            asyncio.run(run_loop(stop_event=stop_event))

    threading.Thread(target=_loop, daemon=True, name="scraper-loop").start()
    cfg2     = load_config()
    interval = cfg2.get("error_log_interval_seconds", 60)
    try:
        schrijf_error_logs_json()
    except Exception as e:
        log.error(f"Fout bij initieel schrijven error_logs.json: {e}")
    start_error_log_loop(interval)
    start_ro_log_watchers()
    try:
        schrijf_ro_logs_json()
    except Exception as e:
        log.error(f"Fout bij initieel schrijven ro_log_data.json: {e}")
    start_ro_log_loop(interval)
    try:
        schrijf_hr_logs_json()
    except Exception as e:
        log.error(f"Fout bij initieel schrijven hr_log_data.json: {e}")
    start_hr_log_loop(interval)
    start_ccs_log_loop(interval)


# ══════════════════════════════════════════════════════════════════
# THUISDIALYSE
# ══════════════════════════════════════════════════════════════════

def td_lees() -> dict:
    return _lees_json_veilig(TD_PATH, {"toestellen": [], "geschiedenis": []})


def td_sla_op(data: dict):
    _schrijf_json_atomisch(TD_PATH, data)


@app.route("/thuisdialyse")
def thuisdialyse():
    return render_template("thuisdialyse.html")


@app.route("/api/thuisdialyse")
def api_thuisdialyse():
    return jsonify(td_lees())


@app.route("/api/thuisdialyse/toestellen", methods=["POST"])
def api_td_toestellen_opslaan():
    payload = request.get_json(force=True)
    data    = td_lees()
    data["toestellen"] = payload.get("toestellen", [])
    td_sla_op(data)
    return jsonify({"ok": True})


@app.route("/api/thuisdialyse/wo_aanmaken", methods=["POST"])
def api_td_wo_aanmaken():
    payload    = request.get_json(force=True)
    td_nummers = payload.get("td_nummers", [])
    if not td_nummers:
        return jsonify({"ok": False, "fout": "Geen toestellen geselecteerd"}), 400

    import time
    job_id = str(int(time.time() * 1000))
    job    = {"bezig": True, "resultaten": [], "stap_logs": [], "fout": None}
    app.config[f"td_job_{job_id}"] = job

    def _stap_log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        job["stap_logs"].append(f"[{ts}] {msg}")

    def _run():
        from wo_aanmaken import verwerk_selectie
        try:
            job["resultaten"] = asyncio.run(verwerk_selectie(td_nummers, stap_log=_stap_log))
        except Exception as e:
            job["fout"] = str(e)
        finally:
            job["bezig"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/thuisdialyse/status/<job_id>")
def api_td_job_status(job_id):
    job = app.config.get(f"td_job_{job_id}")
    if job is None:
        return jsonify({"ok": False, "fout": "Job niet gevonden"}), 404
    return jsonify(job)


@app.route("/api/thuisdialyse/toestel/<int:td_nr>", methods=["DELETE"])
def api_td_toestel_verwijderen(td_nr):
    data = td_lees()
    data["toestellen"] = [t for t in data["toestellen"] if t["td_nr"] != td_nr]
    td_sla_op(data)
    return jsonify({"ok": True})


@app.route("/api/thuisdialyse/geschiedenis/wis", methods=["POST"])
def api_td_geschiedenis_wissen():
    data = td_lees()
    data["geschiedenis"] = []
    td_sla_op(data)
    return jsonify({"ok": True})


@app.route("/api/thuisdialyse/import_excel", methods=["POST"])
def api_td_import_excel():
    if "file" not in request.files:
        return jsonify({"ok": False, "fout": "Geen bestand"}), 400
    f = request.files["file"]
    with tempfile.NamedTemporaryFile(suffix=".xlsm", delete=False) as tmp:
        f.save(tmp.name)
        tmp_pad = tmp.name
    try:
        from import_excel import importeer
        importeer(tmp_pad, str(TD_PATH))
        data = td_lees()
        return jsonify({"ok": True, "aangeleverd": len(data["toestellen"])})
    except Exception as e:
        return jsonify({"ok": False, "fout": str(e)}), 500
    finally:
        os.unlink(tmp_pad)


# ══════════════════════════════════════════════════════════════════
# RO STAALNAMES
# ══════════════════════════════════════════════════════════════════

def ro_lees() -> dict:
    return _lees_json_veilig(RO_PATH, {"installaties": [], "geschiedenis": []})


def ro_sla_op(data: dict):
    _schrijf_json_atomisch(RO_PATH, data)


@app.route("/ro-staalname")
def ro_staalname():
    return render_template("ro_staalname.html")


@app.route("/api/ro-staalname")
def api_ro_staalname():
    return jsonify(ro_lees())


@app.route("/api/ro-staalname/installaties", methods=["POST"])
def api_ro_installaties_opslaan():
    payload = request.get_json(force=True)
    data    = ro_lees()
    data["installaties"] = payload.get("installaties", [])
    ro_sla_op(data)
    return jsonify({"ok": True})


@app.route("/api/ro-staalname/wo_aanmaken", methods=["POST"])
def api_ro_wo_aanmaken():
    payload = request.get_json(force=True)
    namen   = payload.get("namen", [])
    if not namen:
        return jsonify({"ok": False, "fout": "Geen installaties geselecteerd"}), 400

    import time
    job_id = str(int(time.time() * 1000))
    job    = {"bezig": True, "resultaten": [], "stap_logs": [], "fout": None}
    app.config[f"ro_job_{job_id}"] = job

    def _stap_log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        job["stap_logs"].append(f"[{ts}] {msg}")

    def _run():
        from wo_ro_staalname import verwerk_selectie_ro
        try:
            job["resultaten"] = asyncio.run(verwerk_selectie_ro(namen, stap_log=_stap_log))
        except Exception as e:
            job["fout"] = str(e)
        finally:
            job["bezig"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/ro-staalname/wo-opzoeken", methods=["POST"])
def api_ro_wo_opzoeken():
    payload  = request.get_json(force=True)
    selectie = payload.get("selectie", [])

    data         = ro_lees()
    installaties = data.get("installaties", [])

    if selectie:
        installaties = [i for i in installaties if i.get("naam") in selectie]

    if not installaties:
        return jsonify({"ok": False, "fout": "Geen installaties geselecteerd of gevonden"}), 400

    import time
    job_id = str(int(time.time() * 1000))
    job    = {"bezig": True, "resultaten": [], "stap_logs": [], "fout": None}
    app.config[f"ro_job_{job_id}"] = job

    def _stap_log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        job["stap_logs"].append(f"[{ts}] {msg}")

    def _run():
        from wo_ro_staalname import opzoek_werkorders_ro
        try:
            resultaten = asyncio.run(opzoek_werkorders_ro(installaties, stap_log=_stap_log))
            job["resultaten"] = resultaten
            for res in resultaten:
                for inst in data["installaties"]:
                    if inst["naam"] == res["naam"]:
                        inst["laatste_wo_id"]  = res.get("wo_id")
                        inst["laatste_status"] = res.get("status")
                        break
            ro_sla_op(data)
        except Exception as e:
            job["fout"] = str(e)
        finally:
            job["bezig"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/ro-staalname/status/<job_id>")
def api_ro_job_status(job_id):
    job = app.config.get(f"ro_job_{job_id}")
    if job is None:
        return jsonify({"ok": False, "fout": "Job niet gevonden"}), 404
    return jsonify(job)


@app.route("/api/ro-staalname/wo-keuze", methods=["POST"])
def api_ro_wo_keuze():
    """Verwerk de gebruikerskeuze wanneer meerdere WO's gevonden werden."""
    payload = request.get_json(force=True)
    naam    = payload.get("naam")
    wo_id   = payload.get("wo_id")
    status  = payload.get("status")

    if not naam or not wo_id:
        return jsonify({"ok": False, "fout": "naam en wo_id zijn verplicht"}), 400

    data = ro_lees()
    bijgewerkt = False
    for inst in data["installaties"]:
        if inst["naam"] == naam:
            inst["laatste_wo_id"] = wo_id
            inst["laatste_status"] = status
            bijgewerkt = True
            break

    if not bijgewerkt:
        return jsonify({"ok": False, "fout": f"Installatie '{naam}' niet gevonden"}), 404

    ro_sla_op(data)
    return jsonify({"ok": True})


@app.route("/api/ro-staalname/maak-wo", methods=["POST"])
def api_ro_maak_wo():
    """Start een nieuwe WO aanmaken voor een RO-installatie zonder bruikbare WO."""
    payload  = request.get_json(force=True)
    naam     = payload.get("naam", "").strip()
    maximo   = payload.get("maximo", "").strip()
    locatie  = payload.get("locatie", "").strip()

    if not naam or not maximo:
        return jsonify({"ok": False, "fout": "naam en maximo zijn verplicht"}), 400

    job_id = f"maak_wo_{naam}_{datetime.now().strftime('%H%M%S')}"
    job = {"status": "bezig", "log": [], "resultaat": None}
    app.config[f"ro_job_{job_id}"] = job

    def _stap_log(msg):
        job["log"].append(msg)

    def _run():
        try:
            import asyncio
            from playwright.async_api import async_playwright
            from wo_ro_staalname import login, maak_nieuw_wo_ro, PS_HEADLESS

            installatie = {"naam": naam, "maximo": maximo, "locatie": locatie}

            async def _aanmaken():
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(headless=PS_HEADLESS)
                    context = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="nl-BE")
                    page    = await context.new_page()
                    try:
                        if not await login(page, _stap_log):
                            raise RuntimeError("Login mislukt")
                        return await maak_nieuw_wo_ro(page, installatie, stap_log=_stap_log)
                    finally:
                        await browser.close()

            res = asyncio.run(_aanmaken())
            job["status"]    = "klaar"
            job["resultaat"] = res
        except Exception as exc:
            import traceback
            job["status"]    = "fout"
            job["resultaat"] = {"ok": False, "fout": str(exc)}
            for r in traceback.format_exc().splitlines():
                job["log"].append(r)

    import threading
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/ro-staalname/maak-wo-status/<job_id>")
def api_ro_maak_wo_status(job_id):
    """Poll de status van een lopende WO aanmaak."""
    job = app.config.get(f"ro_job_{job_id}")
    if not job:
        return jsonify({"ok": False, "fout": "Job niet gevonden"}), 404
    return jsonify({
        "ok":       True,
        "bezig":    job["status"] == "bezig",
        "status":   job["status"],
        "log":      job["log"],
        "resultaat": job["resultaat"],
    })


@app.route("/api/ccs/config")
def api_ccs_config():
    """Geef CCS-configuratie terug (maximo-nummer voor WO aanmaken, vaten-mapping)."""
    cfg = _lees_config() if callable(globals().get("_lees_config")) else {}
    # Haal _lees_config op uit wo_aanmaken module
    try:
        from wo_aanmaken import _cfg as _wo_cfg
        ccs_maximo = _wo_cfg.get("ccs_maximo", "")
        ccs_vaten  = _wo_cfg.get("ccs_vaten", {})
    except Exception:
        ccs_maximo = ""
        ccs_vaten  = {}
    # Fallback: rechtstreeks uit config.json
    if not ccs_maximo or not ccs_vaten:
        import json as _json
        from pathlib import Path as _Path
        cfg_pad = _Path(__file__).parent / "config.json"
        if cfg_pad.exists():
            _raw = _json.loads(cfg_pad.read_text(encoding="utf-8"))
            ccs_maximo = ccs_maximo or _raw.get("ccs_maximo", "")
            ccs_vaten  = ccs_vaten or _raw.get("ccs_vaten", {})
    return jsonify({"ok": True, "ccs_maximo": ccs_maximo, "ccs_vaten": ccs_vaten})


# ── 1-malig overslaan van de automatische ESS-bestelling per CCS-groep ──────
# Scenario: containers x.1 en x.2 bevatten hetzelfde vat-type; als er per vergissing
# een dubbele/foutieve "leeg"-melding binnenkomt, kan je hiermee de eerstvolgende
# automatische bestelling voor die groep 1x overslaan (de WO wordt wél nog aangemaakt).
CCS_SKIP_PAD = Path(__file__).parent / "ccs_skip_bestelling.json"
CCS_GROEPEN  = ["1", "2", "3"]


def _ccs_skip_lezen() -> dict:
    data = _lees_json_veilig(CCS_SKIP_PAD, {})
    return {g: bool(data.get(g, False)) for g in CCS_GROEPEN}


def _ccs_skip_schrijven(data: dict) -> None:
    _schrijf_json_atomisch(CCS_SKIP_PAD, {g: bool(data.get(g, False)) for g in CCS_GROEPEN})


def _ccs_groep_van_container(container: str) -> str:
    """'container 2.1' -> '2'"""
    deel = (container or "").strip().split()[-1] if container else ""
    return deel.split(".")[0] if "." in deel else ""


@app.route("/api/ccs/skip-bestelling", methods=["GET", "POST"])
def api_ccs_skip_bestelling():
    if request.method == "GET":
        resp = jsonify({
            "ok": True,
            "skip": _ccs_skip_lezen(),
            "pad": str(CCS_SKIP_PAD),
            "mtime": CCS_SKIP_PAD.stat().st_mtime if CCS_SKIP_PAD.exists() else None,
            "host": socket.gethostname(),
        })
        # Nooit cachen — dit moet elke poll het live bestand teruggeven,
        # ook via eventuele netwerkproxy's tussen browser en server.
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp

    payload = request.get_json(force=True) or {}
    groep   = str(payload.get("groep", "")).strip()
    waarde  = bool(payload.get("waarde"))
    if groep not in CCS_GROEPEN:
        return jsonify({"ok": False, "fout": "Ongeldige CCS-groep"}), 400

    try:
        data = _ccs_skip_lezen()
        data[groep] = waarde
        _ccs_skip_schrijven(data)
    except Exception as e:
        log.error(f"CCS skip-bestelling schrijven mislukt (groep {groep} -> {waarde}) "
                  f"op host {socket.gethostname()}: {e}", exc_info=True)
        return jsonify({"ok": False, "fout": f"Schrijven mislukt: {e}"}), 500

    log.info(f"CCS skip-bestelling groep {groep} -> {waarde} (host {socket.gethostname()}, "
             f"pad {CCS_SKIP_PAD})")
    resp = jsonify({"ok": True, "skip": data, "host": socket.gethostname()})
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


def _voer_ccs_aanmaken_uit(container: str, maximo: str, job: dict, dedup_key: str):
    """Gemeenschappelijke logica voor CCS ESS + WO aanmaken.
    Wordt gebruikt door zowel api_ccs_maak_wo (browser-trigger) als
    _trigger_ccs_wo_backend (log-watcher trigger).
    """
    import asyncio
    from playwright.async_api import async_playwright
    from wo_aanmaken import login, maak_wo_ccs_container, PS_HEADLESS
    from ess_bestelling import maak_ess_aanvraag, vat_info as _ess_vat_info

    hostname = socket.gethostname()

    def _stap_log(msg):
        job["log"].append(msg)

    # ── Containerbestellingen mogen uitsluitend door de master-scraper-pc ──
    if hostname.upper() != CONTAINER_BESTELLING_MASTER_PC:
        fout = (f"Containerbestelling geweigerd: alleen pc '{CONTAINER_BESTELLING_MASTER_PC}' "
                f"mag dit uitvoeren (deze pc: '{hostname}')")
        log.warning(f"CCS bestelling geweigerd op pc '{hostname}' (vereist: {CONTAINER_BESTELLING_MASTER_PC}) — container={container}")
        _stap_log(f"❌ {fout}")
        job["status"]    = "fout"
        job["resultaat"] = {"ok": False, "fout": fout, "pc": hostname}
        if dedup_key:
            app.config.pop(dedup_key, None)
        return

    log.info(f"CCS bestelling gestart door pc '{hostname}' — container={container}")

    async def _aanmaken():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=PS_HEADLESS)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900}, locale="nl-BE"
            )
            page = await context.new_page()
            try:
                if not await login(page, _stap_log):
                    raise RuntimeError("Login mislukt")

                groep          = _ccs_groep_van_container(container)
                skip_data      = _ccs_skip_lezen()
                moet_overslaan = skip_data.get(groep, False)

                if moet_overslaan:
                    _stap_log(f"⏭ Automatische bestelling overgeslagen — toggle actief voor CCS {groep}.1/{groep}.2")
                    ess_res = {"ok": False, "fout": None, "aanvraag_id": None,
                               "vat_info": _ess_vat_info(container), "overgeslagen": True}
                    skip_data[groep] = False   # 1-malig: meteen terug uitzetten
                    _ccs_skip_schrijven(skip_data)
                    log.info(f"CCS skip-bestelling groep {groep} verbruikt en teruggezet")
                else:
                    _stap_log(f"ESS: Bestelling aanmaken voor '{container}'...")
                    ess_res = await maak_ess_aanvraag(page, container, stap_log=_stap_log)
                    if not ess_res["ok"]:
                        _stap_log(f"⚠ ESS aanvraag mislukt: {ess_res['fout']} — WO wordt toch aangemaakt")
                    else:
                        _stap_log(f"✓ ESS aanvraag {ess_res['aanvraag_id']} aangemaakt")

                wo_res = await maak_wo_ccs_container(page, container, maximo, stap_log=_stap_log)
                wo_res["ess_aanvraag_id"] = ess_res.get("aanvraag_id")
                wo_res["ess_ok"]          = ess_res["ok"]
                wo_res["ess_fout"]        = ess_res.get("fout")
                wo_res["vat_info"]        = ess_res.get("vat_info")
                wo_res["ess_overgeslagen"] = ess_res.get("overgeslagen", False)
                log_ess_bestelling(container, wo_res)
                return wo_res
            finally:
                await browser.close()

    try:
        res = asyncio.run(_aanmaken())
        job["status"]    = "klaar"
        job["resultaat"] = res
        if dedup_key:
            app.config[dedup_key]["status"] = "klaar"
        log.info(f"CCS aanmaken klaar op pc '{hostname}' — container={container}, wo_id={res.get('wo_id')}, ok={res.get('ok')}")
    except Exception as exc:
        import traceback
        job["status"]    = "fout"
        job["resultaat"] = {"ok": False, "fout": str(exc)}
        for r in traceback.format_exc().splitlines():
            job["log"].append(r)
        if dedup_key:
            app.config.pop(dedup_key, None)
        log.error(f"CCS aanmaken fout voor '{container}': {exc}")


@app.route("/api/ccs/maak-wo", methods=["POST"])
def api_ccs_maak_wo():
    """Start WO aanmaken voor CCS container vervanging (triggered bij herstel-popup verdwijnen).
    Deduplicatie: per container mag er maar één actieve job tegelijk zijn (max 10 min geldig).
    Meerdere browsers die tegelijk triggeren krijgen dus maar één WO.
    """
    payload   = request.get_json(force=True)
    container = payload.get("container", "").strip()   # bv. "container 1.1"
    maximo    = payload.get("maximo", "").strip()       # T-nummer CCS installatie

    if not container or not maximo:
        return jsonify({"ok": False, "fout": "container en maximo zijn verplicht"}), 400

    # ── Deduplicatie: al een actieve/recente job voor deze container? ──
    dedup_key = f"ccs_dedup_{container.replace(' ', '_')}"
    bestaand  = app.config.get(dedup_key)
    if bestaand:
        leeftijd = (datetime.now() - bestaand["gestart"]).total_seconds()
        if bestaand["status"] in ("bezig",) or (bestaand["status"] == "klaar" and leeftijd < 600):
            log.info(f"CCS WO dedup: aanvraag voor '{container}' genegeerd (job {bestaand['job_id']} is {bestaand['status']}, {int(leeftijd)}s geleden)")
            return jsonify({"ok": True, "job_id": bestaand["job_id"], "dedup": True})

    job_id = f"ccs_wo_{container.replace(' ', '_')}_{datetime.now().strftime('%H%M%S')}"
    job = {"status": "bezig", "log": [], "resultaat": None}
    app.config[f"ccs_job_{job_id}"] = job
    app.config[dedup_key] = {"job_id": job_id, "status": "bezig", "gestart": datetime.now()}

    threading.Thread(
        target=_voer_ccs_aanmaken_uit,
        args=(container, maximo, job, dedup_key),
        daemon=True,
        name=f"ccs-wo-{container}",
    ).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/ccs/maak-wo-status/<job_id>")
def api_ccs_maak_wo_status(job_id):
    """Peil de status van een CCS WO aanmaak-job."""
    job = app.config.get(f"ccs_job_{job_id}")
    if not job:
        return jsonify({"ok": False, "fout": "Job niet gevonden"}), 404
    return jsonify({
        "ok":        True,
        "status":    job["status"],
        "log":       job["log"],
        "resultaat": job["resultaat"],
    })


@app.route("/api/ccs/wo-log")
def api_ccs_wo_log():
    """Geeft de laatste N regels van logs/ccs_wo.log terug als platte tekst.
    Query param: n (default 100, max 500)
    """
    n       = min(int(request.args.get("n", 100)), 500)
    pad     = Path(__file__).parent / "logs" / "ccs_wo.log"
    if not pad.exists():
        return jsonify({"ok": False, "fout": "Logbestand bestaat nog niet"}), 404
    try:
        with open(pad, encoding="utf-8") as f:
            regels = f.readlines()
        laatste = [r.rstrip() for r in regels[-n:]]
        return jsonify({"ok": True, "regels": laatste, "totaal": len(regels)})
    except Exception as e:
        return jsonify({"ok": False, "fout": str(e)}), 500


@app.route("/api/ro-staalname/print-ticketten", methods=["POST"])
def api_ro_print_ticketten():
    """Geef printbare HTML terug voor de geselecteerde installaties."""
    payload  = request.get_json(force=True)
    namen    = payload.get("namen", [])  # lijst van geselecteerde installatienamen
    data     = ro_lees()
    installaties = [i for i in data["installaties"] if i["naam"] in namen]
    if not installaties:
        return jsonify({"ok": False, "fout": "Geen installaties geselecteerd"}), 400
    return jsonify({"ok": True, "installaties": installaties})


@app.route("/ro-staalname/ticketten")
def ro_ticketten_pagina():
    return render_template("ro_ticketten.html")


@app.route("/api/ro-staalname/installatie/<naam>", methods=["DELETE"])
def api_ro_installatie_verwijderen(naam):
    data = ro_lees()
    data["installaties"] = [i for i in data["installaties"] if i["naam"] != naam]
    ro_sla_op(data)
    return jsonify({"ok": True})


@app.route("/api/ro-staalname/geschiedenis/wis", methods=["POST"])
def api_ro_geschiedenis_wissen():
    data = ro_lees()
    data["geschiedenis"] = []
    ro_sla_op(data)
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════
# DIALYSE RESULTATEN
# ══════════════════════════════════════════════════════════════════

def dr_lees() -> dict:
    return _lees_json_veilig(DR_PATH, {"stalen": [], "geschiedenis": []})


def dr_sla_op(data: dict):
    _schrijf_json_atomisch(DR_PATH, data)


@app.route("/dialyse-resultaten")
def dialyse_resultaten_pagina():
    return render_template("dialyse_resultaten.html")


@app.route("/api/dialyse-resultaten")
def api_dr_lees():
    return jsonify(dr_lees())


@app.route("/api/dialyse-resultaten/config")
def api_dr_config():
    from dialyse_resultaten import PS_PSC_URL, PS_HEADLESS
    return jsonify({"ps_psc_url": PS_PSC_URL, "headless": PS_HEADLESS})


@app.route("/api/dialyse-resultaten/headless", methods=["POST"])
def api_dr_headless():
    import dialyse_resultaten as dr
    val = request.get_json(force=True).get("headless", False)
    dr.PS_HEADLESS = bool(val)
    return jsonify({"ok": True, "headless": dr.PS_HEADLESS})


@app.route("/api/dialyse-resultaten/upload-excel", methods=["POST"])
def api_dr_upload_excel():
    if "file" not in request.files:
        return jsonify({"ok": False, "fout": "Geen bestand meegestuurd"}), 400
    f = request.files["file"]
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        f.save(tmp.name)
        tmp_pad = tmp.name
    try:
        from dialyse_resultaten import importeer_excel
        stalen_nieuw = importeer_excel(tmp_pad)
        data     = dr_lees()
        bestaand = {s["maximo"] + "|" + s["datum"]: s for s in data.get("stalen", [])}
        toegevoegd = 0
        for s in stalen_nieuw:
            sleutel = s["maximo"] + "|" + s["datum"]
            if sleutel in bestaand:
                bestaand[sleutel].update(
                    {k: v for k, v in s.items() if k not in ("wo_id", "status")}
                )
            else:
                bestaand[sleutel] = s
                toegevoegd += 1
        data["stalen"] = list(bestaand.values())
        dr_sla_op(data)
        return jsonify({"ok": True, "aangeleverd": len(stalen_nieuw), "nieuw": toegevoegd})
    except Exception as e:
        return jsonify({"ok": False, "fout": str(e)}), 500
    finally:
        os.unlink(tmp_pad)


@app.route("/api/dialyse-resultaten/upload-pdf", methods=["POST"])
def api_dr_upload_pdf():
    """
    Upload één of meerdere Fresenius analyseverslag-PDF's, parseer en sla op
    als stalen ZONDER maximo (T-nummer wordt pas via PeopleSoft opgezocht).
    Gebruik daarna /api/dialyse-resultaten/pdf-verwerken om T-nr + WO op te zoeken.
    """
    if "bestanden" not in request.files:
        return jsonify({"ok": False, "fout": "Geen bestanden meegestuurd"}), 400

    bestanden = [b for b in request.files.getlist("bestanden")
                 if b.filename and b.filename.lower().endswith(".pdf")]
    if not bestanden:
        return jsonify({"ok": False, "fout": "Geen geldige PDF-bestanden gevonden"}), 400

    from dialyse_resultaten import extraheer_staal_uit_pdf

    data = dr_lees()
    data.setdefault("pdf_stalen", [])
    toegevoegd, fouten = [], []

    for bestand in bestanden:
        pad = DR_UPLOAD_DIR / bestand.filename
        bestand.save(str(pad))
        info = extraheer_staal_uit_pdf(pad)
        if not info["ok"]:
            fouten.append({"bestandsnaam": bestand.filename, "fout": info["fout"]})
            continue
        data["pdf_stalen"].append(info)
        toegevoegd.append(info)

    dr_sla_op(data)
    return jsonify({"ok": True, "toegevoegd": len(toegevoegd), "fouten": fouten,
                    "stalen": toegevoegd})


@app.route("/api/dialyse-resultaten/pdf-verwerken", methods=["POST"])
def api_dr_pdf_verwerken():
    """
    Start een asynchrone job die voor alle geüploade PDF-stalen (zonder maximo)
    het T-nummer opzoekt via 'Zoek Object' en daarna de WO opzoekt.
    Resultaten worden toegevoegd aan de normale stalenlijst (met pdf_pad,
    zodat verzend_resultaten() de PDF als bijlage kan opladen).
    """
    data       = dr_lees()
    pdf_stalen = data.get("pdf_stalen", [])
    te_verwerken = [s for s in pdf_stalen if not s.get("_verwerkt")]
    if not te_verwerken:
        return jsonify({"ok": False, "fout": "Geen PDF-stalen om te verwerken"}), 400

    import time
    job_id = str(int(time.time() * 1000))
    job    = {"bezig": True, "resultaten": [], "stap_logs": [], "fout": None}
    app.config[f"dr_job_{job_id}"] = job

    def _stap_log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        job["stap_logs"].append(f"[{ts}] {msg}")

    def _run():
        from dialyse_resultaten import verwerk_pdf_stalen
        try:
            pdf_paden  = [s["pdf_pad"] for s in te_verwerken]
            resultaten = asyncio.run(verwerk_pdf_stalen(pdf_paden, stap_log=_stap_log))
            job["resultaten"] = resultaten

            data2 = dr_lees()
            bestaand = {s["maximo"] + "|" + s["datum"]: s
                        for s in data2.get("stalen", []) if s.get("maximo")}
            for r in resultaten:
                if r.get("maximo"):
                    sleutel = r["maximo"] + "|" + r["datum"]
                    bestaand[sleutel] = r
            data2["stalen"] = list(bestaand.values())

            # markeer verwerkte pdf_stalen
            verwerkte_paden = {p for p in pdf_paden}
            for s in data2.get("pdf_stalen", []):
                if s.get("pdf_pad") in verwerkte_paden:
                    s["_verwerkt"] = True
            dr_sla_op(data2)
        except Exception as e:
            job["fout"] = str(e)
        finally:
            job["bezig"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})



@app.route("/api/dialyse-resultaten/handmatig-toevoegen", methods=["POST"])
def api_dr_handmatig():
    payload      = request.get_json(force=True)
    stalen_nieuw = payload.get("stalen", [])
    data         = dr_lees()
    bestaand_keys = {s["maximo"] + "|" + s["datum"] for s in data["stalen"]}
    toegevoegd = 0
    for s in stalen_nieuw:
        k = s.get("maximo", "") + "|" + s.get("datum", "")
        if k not in bestaand_keys:
            data["stalen"].append({
                "datum":       s.get("datum", ""),
                "eenheid":     s.get("eenheid", ""),
                "detail":      s.get("detail", ""),
                "maximo":      s.get("maximo", ""),
                "kiemgetal":   s.get("kiemgetal", "0"),
                "endotoxine":  s.get("endotoxine", "<0,125"),
                "commentaar":  s.get("commentaar", "*"),
                "wo_id":       None,
                "status":      None,
            })
            bestaand_keys.add(k)
            toegevoegd += 1
    dr_sla_op(data)
    return jsonify({"ok": True, "toegevoegd": toegevoegd})


@app.route("/api/dialyse-resultaten/stalen-bijwerken", methods=["POST"])
def api_dr_stalen_bijwerken():
    payload        = request.get_json(force=True)
    data           = dr_lees()
    data["stalen"] = payload.get("stalen", data["stalen"])
    dr_sla_op(data)
    return jsonify({"ok": True})


@app.route("/api/dialyse-resultaten/staal-verwijderen", methods=["POST"])
def api_dr_staal_verwijderen():
    payload = request.get_json(force=True)
    maximo  = payload.get("maximo", "")
    datum   = payload.get("datum", "")
    data    = dr_lees()
    data["stalen"] = [
        s for s in data["stalen"]
        if not (s["maximo"] == maximo and s["datum"] == datum)
    ]
    dr_sla_op(data)
    return jsonify({"ok": True})


@app.route("/api/dialyse-resultaten/alles-wissen", methods=["POST"])
def api_dr_alles_wissen():
    data           = dr_lees()
    data["stalen"] = []
    dr_sla_op(data)
    return jsonify({"ok": True})


@app.route("/api/dialyse-resultaten/wo-opzoeken", methods=["POST"])
def api_dr_wo_opzoeken():
    data   = dr_lees()
    stalen = data.get("stalen", [])
    if not stalen:
        return jsonify({"ok": False, "fout": "Geen stalen geladen"}), 400

    import time
    job_id = str(int(time.time() * 1000))
    job    = {"bezig": True, "resultaten": [], "stap_logs": [], "fout": None}
    app.config[f"dr_job_{job_id}"] = job

    def _stap_log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        job["stap_logs"].append(f"[{ts}] {msg}")

    def _run():
        from dialyse_resultaten import zoek_werkorders
        try:
            resultaten     = asyncio.run(zoek_werkorders(stalen, stap_log=_stap_log))
            job["resultaten"] = resultaten
            data["stalen"] = resultaten
            dr_sla_op(data)
        except Exception as e:
            job["fout"] = str(e)
        finally:
            job["bezig"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/dialyse-resultaten/verzenden", methods=["POST"])
def api_dr_verzenden():
    payload       = request.get_json(force=True)
    selectie_keys = set(payload.get("selectie", []))
    data          = dr_lees()
    stalen        = data.get("stalen", [])

    if selectie_keys:
        te_verz = [s for s in stalen if (s["maximo"] + "|" + s["datum"]) in selectie_keys]
    else:
        te_verz = [s for s in stalen if s.get("status") == "INUITV"]

    if not te_verz:
        return jsonify({"ok": False,
                        "fout": "Geen stalen met status INUITV geselecteerd"}), 400

    import time
    job_id = str(int(time.time() * 1000))
    job    = {"bezig": True, "resultaten": [], "stap_logs": [], "fout": None}
    app.config[f"dr_job_{job_id}"] = job

    def _stap_log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        job["stap_logs"].append(f"[{ts}] {msg}")

    def _run():
        from dialyse_resultaten import verzend_resultaten
        try:
            resultaten        = asyncio.run(verzend_resultaten(te_verz, stap_log=_stap_log))
            job["resultaten"] = resultaten
            res_map           = {r["maximo"] + "|" + r["datum"]: r for r in resultaten}
            data["stalen"]    = [
                res_map.get(s["maximo"] + "|" + s["datum"], s) for s in stalen
            ]
            for r in resultaten:
                data.setdefault("geschiedenis", []).append({
                    "datum_actie": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "datum_staal": r.get("datum"),
                    "maximo":      r.get("maximo"),
                    "eenheid":     r.get("eenheid"),
                    "wo_id":       r.get("wo_id"),
                    "status":      r.get("status"),
                    "melding":     r.get("melding", ""),
                    "ok":          r.get("ok", False),
                })
            dr_sla_op(data)
        except Exception as e:
            job["fout"] = str(e)
        finally:
            job["bezig"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/dialyse-resultaten/job/<job_id>")
def api_dr_job_status(job_id):
    job = app.config.get(f"dr_job_{job_id}")
    if job is None:
        return jsonify({"ok": False, "fout": "Job niet gevonden"}), 404
    return jsonify(job)


@app.route("/api/dialyse-resultaten/geschiedenis/wis", methods=["POST"])
def api_dr_geschiedenis_wissen():
    data = dr_lees()
    data["geschiedenis"] = []
    dr_sla_op(data)
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════
# RO RESULTATEN (ringleiding) — data opslag
# ══════════════════════════════════════════════════════════════════

RO_RES_PATH = Path(__file__).parent / "ro_resultaten_data.json"

def ro_res_lees() -> dict:
    return _lees_json_veilig(RO_RES_PATH, {"stalen": [], "geschiedenis": []})

def ro_res_sla_op(data: dict):
    _schrijf_json_atomisch(RO_RES_PATH, data)


@app.route("/api/ro-resultaten")
def api_ro_res_lees():
    return jsonify(ro_res_lees())


@app.route("/api/ro-resultaten/upload-excel", methods=["POST"])
def api_ro_res_upload():
    if "file" not in request.files:
        return jsonify({"ok": False, "fout": "Geen bestand"}), 400
    f = request.files["file"]
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        f.save(tmp.name)
        tmp_pad = tmp.name
    try:
        from dialyse_resultaten import importeer_excel_ro
        stalen_nieuw = importeer_excel_ro(tmp_pad)
        data     = ro_res_lees()
        bestaand = {s["maximo"] + "|" + s["datum"]: s for s in data.get("stalen", [])}
        toegevoegd = 0
        for s in stalen_nieuw:
            sleutel = s["maximo"] + "|" + s["datum"]
            if sleutel in bestaand:
                bestaand[sleutel].update({k: v for k, v in s.items() if k not in ("wo_id", "status")})
            else:
                bestaand[sleutel] = s
                toegevoegd += 1
        data["stalen"] = list(bestaand.values())
        ro_res_sla_op(data)
        return jsonify({"ok": True, "aangeleverd": len(stalen_nieuw), "nieuw": toegevoegd})
    except Exception as e:
        return jsonify({"ok": False, "fout": str(e)}), 500
    finally:
        os.unlink(tmp_pad)


@app.route("/api/ro-resultaten/stalen-bijwerken", methods=["POST"])
def api_ro_res_bijwerken():
    payload = request.get_json(force=True)
    data = ro_res_lees()
    data["stalen"] = payload.get("stalen", data["stalen"])
    ro_res_sla_op(data)
    return jsonify({"ok": True})


@app.route("/api/ro-resultaten/staal-verwijderen", methods=["POST"])
def api_ro_res_verwijderen():
    payload = request.get_json(force=True)
    maximo  = payload.get("maximo", "")
    datum   = payload.get("datum", "")
    data    = ro_res_lees()
    data["stalen"] = [s for s in data["stalen"] if not (s["maximo"] == maximo and s["datum"] == datum)]
    ro_res_sla_op(data)
    return jsonify({"ok": True})


@app.route("/api/ro-resultaten/alles-wissen", methods=["POST"])
def api_ro_res_wissen():
    data = ro_res_lees()
    data["stalen"] = []
    ro_res_sla_op(data)
    return jsonify({"ok": True})


@app.route("/api/ro-resultaten/wo-opzoeken", methods=["POST"])
def api_ro_res_wo_opzoeken():
    payload  = request.get_json(force=True)
    selectie = set(payload.get("selectie", []))
    data     = ro_res_lees()
    stalen   = data.get("stalen", [])
    if not stalen:
        return jsonify({"ok": False, "fout": "Geen stalen geladen"}), 400

    # Filter op selectie als opgegeven
    te_zoeken = [s for s in stalen if (s["maximo"] + "|" + s["datum"]) in selectie] if selectie else stalen

    import time
    job_id = str(int(time.time() * 1000))
    job    = {"bezig": True, "resultaten": [], "stap_logs": [], "fout": None}
    app.config[f"ro_res_job_{job_id}"] = job

    def _stap_log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        job["stap_logs"].append(f"[{ts}] {msg}")

    def _run():
        from dialyse_resultaten import zoek_werkorders_ro
        try:
            resultaten     = asyncio.run(zoek_werkorders_ro(te_zoeken, stap_log=_stap_log))
            job["resultaten"] = resultaten
            # Merge resultaten terug in volledige lijst
            res_map        = {r["maximo"] + "|" + r["datum"]: r for r in resultaten}
            data["stalen"] = [res_map.get(s["maximo"] + "|" + s["datum"], s) for s in stalen]
            ro_res_sla_op(data)
        except Exception as e:
            job["fout"] = str(e)
        finally:
            job["bezig"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/ro-resultaten/verzenden", methods=["POST"])
def api_ro_res_verzenden():
    payload       = request.get_json(force=True)
    selectie_keys = set(payload.get("selectie", []))
    data          = ro_res_lees()
    stalen        = data.get("stalen", [])

    if selectie_keys:
        te_verz = [s for s in stalen if (s["maximo"] + "|" + s["datum"]) in selectie_keys]
    else:
        te_verz = [s for s in stalen if s.get("status") == "INUITV"]

    if not te_verz:
        return jsonify({"ok": False, "fout": "Geen stalen met status INUITV geselecteerd"}), 400

    import time
    job_id = str(int(time.time() * 1000))
    job    = {"bezig": True, "resultaten": [], "stap_logs": [], "fout": None}
    app.config[f"ro_res_job_{job_id}"] = job

    def _stap_log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        job["stap_logs"].append(f"[{ts}] {msg}")

    def _run():
        from dialyse_resultaten import verzend_resultaten_ro
        try:
            resultaten        = asyncio.run(verzend_resultaten_ro(te_verz, stap_log=_stap_log))
            job["resultaten"] = resultaten
            res_map           = {r["maximo"] + "|" + r["datum"]: r for r in resultaten}
            data["stalen"]    = [res_map.get(s["maximo"] + "|" + s["datum"], s) for s in stalen]
            for r in resultaten:
                data.setdefault("geschiedenis", []).append({
                    "datum_actie": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "datum_staal": r.get("datum"),
                    "maximo":      r.get("maximo"),
                    "eenheid":     r.get("eenheid"),
                    "wo_id":       r.get("wo_id"),
                    "status":      r.get("status"),
                    "melding":     r.get("melding", ""),
                    "ok":          r.get("ok", False),
                })
            ro_res_sla_op(data)
        except Exception as e:
            job["fout"] = str(e)
        finally:
            job["bezig"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/ro-resultaten/job/<job_id>")
def api_ro_res_job_status(job_id):
    job = app.config.get(f"ro_res_job_{job_id}")
    if job is None:
        return jsonify({"ok": False, "fout": "Job niet gevonden"}), 404
    return jsonify(job)


@app.route("/api/ro-resultaten/geschiedenis/wis", methods=["POST"])
def api_ro_res_geschiedenis_wissen():
    data = ro_res_lees()
    data["geschiedenis"] = []
    ro_res_sla_op(data)
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════
# ERROR LOGS & ALERTS
# ══════════════════════════════════════════════════════════════════

ERROR_LOGS_PATH    = Path(__file__).parent / "error_logs.json"
RO_LOG_DATA_PATH   = Path(__file__).parent / "ro_log_data.json"
HR_LOG_DATA_PATH   = Path(__file__).parent / "hr_log_data.json"
CCS_LOG_DATA_PATH  = Path(__file__).parent / "ccs_log_data.json"
ALERTS_PATH     = Path(__file__).parent / "error_alerts.json"

DEFAULT_ERROR_LOGS = {
    "RO 304":  r"Z:\LOGGING LAUER\E304 - fase 4B\RO304_error.txt",
    "HR 304":  r"Z:\LOGGING LAUER\E304 - fase 4B\HR304_error.txt",
    "RO1 465": r"Z:\LOGGING LAUER\Virtual Machine Log Files\RO1\RO1_error.txt",
    "RO2 465": r"Z:\LOGGING LAUER\Virtual Machine Log Files\RO2\RO2_error.txt",
    "HR1 465": r"Z:\LOGGING LAUER\Virtual Machine Log Files\HR1\HR1_error.txt",
    "HR2 465": r"Z:\LOGGING LAUER\Virtual Machine Log Files\HR2\HR2_error.txt",
    "CCS 465": r"Z:\LOGGING LAUER\Virtual Machine Log Files\CCS\CCS_error.txt",
}

FOUT_WOORDEN     = ["error", "fout", "alarm", "leakage", "lek", "disabled"]
RO_INSTALLATIES  = {"RO 304", "RO1 465", "RO2 465"}
HR_INSTALLATIES  = {"HR 304", "HR1 465", "HR2 465"}
CCS_INSTALLATIES = {"CCS 465"}


# ══════════════════════════════════════════════════════════════════════════════
# SERVICE RAPPORTEN — PDF upload, SN extractie, T-nummer lookup, WO aanmaken
# ══════════════════════════════════════════════════════════════════════════════

SR_UPLOAD_DIR = Path(__file__).parent / "uploads" / "service_rapporten"
SR_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

SR_MAX_BESTANDSNAAM_LEN = 50  # incl. extensie — PeopleSoft attachment-limiet


def _trim_sr_bestandsnaam(naam: str, doelmap: Path, max_len: int = SR_MAX_BESTANDSNAAM_LEN) -> str:
    """Kort de bestandsnaam in tot max_len karakters (incl. extensie),
    en zorgt voor een unieke naam binnen doelmap."""
    p = Path(naam)
    ext = p.suffix
    stem = p.stem
    beschikbaar = max(1, max_len - len(ext))
    stem = stem[:beschikbaar]
    nieuwe_naam = stem + ext

    # Botsing vermijden: voeg _1, _2, ... toe indien nodig
    pad = doelmap / nieuwe_naam
    i = 1
    while pad.exists():
        suffix = f"_{i}"
        stem_kort = stem[: max(1, beschikbaar - len(suffix))]
        nieuwe_naam = stem_kort + suffix + ext
        pad = doelmap / nieuwe_naam
        i += 1

    return nieuwe_naam

_sr_jobs: dict[str, dict] = {}  # job_id → {"status", "log", "resultaat"}


def _sr_log_factory(job_id: str):
    def log(msg):
        _sr_jobs.setdefault(job_id, {"status": "bezig", "log": [], "resultaat": None})
        _sr_jobs[job_id]["log"].append(msg)
    return log


@app.route("/service-rapporten")
def service_rapporten():
    return render_template("service_rapporten.html")


@app.route("/api/service-rapporten")
def api_sr_lijst():
    from wo_service_rapport import sr_lees
    data = sr_lees()
    return jsonify(data.get("rapporten", []))


@app.route("/api/service-rapporten/upload", methods=["POST"])
def api_sr_upload():
    """Upload één of meerdere PDF's, extraheer SN en sla op."""
    try:
        from wo_service_rapport import extraheer_sn_uit_pdf, sr_voeg_toe
        if "bestanden" not in request.files:
            return jsonify({"ok": False, "fout": "Geen bestanden meegestuurd"}), 400

        bestanden = [b for b in request.files.getlist("bestanden")
                     if b.filename and b.filename.lower().endswith(".pdf")]
        if not bestanden:
            return jsonify({"ok": False, "fout": "Geen geldige PDF-bestanden gevonden"}), 400

        resultaten = []
        for bestand in bestanden:
            try:
                nieuwe_naam = _trim_sr_bestandsnaam(bestand.filename, SR_UPLOAD_DIR)
                pad = SR_UPLOAD_DIR / nieuwe_naam
                bestand.save(str(pad))
            except Exception as e:
                return jsonify({"ok": False, "fout": f"Opslaan mislukt voor '{bestand.filename}': {e}"}), 500

            try:
                info = extraheer_sn_uit_pdf(pad)
            except Exception as e:
                info = {"sn": "", "product": "", "firma": "?", "formaat": "fout",
                        "datum": "", "type_verzoek": "", "uren_arbeid": "",
                        "bestandsnaam": bestand.filename}

            try:
                rapport = sr_voeg_toe({
                    "bestandsnaam": info.get("bestandsnaam", bestand.filename),
                    "pdf_pad": str(pad),
                    "sn": info.get("sn", ""),
                    "product": info.get("product", ""),
                    "firma": info.get("firma", ""),
                    "formaat": info.get("formaat", ""),
                    "datum": info.get("datum", ""),
                    "type_verzoek": info.get("type_verzoek", ""),
                    "uren_arbeid": info.get("uren_arbeid", ""),
                    "werkorder_nr_firma": info.get("werkorder_nr", ""),
                    "probleemmelding": info.get("omschrijving_kort", ""),
                    "oplossing": info.get("activiteit_tekst", ""),
                })
                resultaten.append(rapport)
            except Exception as e:
                return jsonify({"ok": False, "fout": f"Opslaan in database mislukt: {e}"}), 500

        return jsonify({"ok": True, "rapporten": resultaten})

    except Exception as e:
        return jsonify({"ok": False, "fout": f"Onverwachte fout: {e}"}), 500


@app.route("/api/service-rapporten/<rapport_id>", methods=["PATCH"])
def api_sr_update(rapport_id):
    """Manuele update van velden (sn, t_nummer, uren_arbeid, …)."""
    from wo_service_rapport import sr_update
    velden = request.get_json(force=True) or {}
    toegelaten = {"sn", "t_nummer", "t_omschrijving", "product", "firma",
                  "datum", "type_verzoek", "uren_arbeid", "status"}
    update = {k: v for k, v in velden.items() if k in toegelaten}
    sr_update(rapport_id, **update)
    return jsonify({"ok": True})


@app.route("/api/service-rapporten/<rapport_id>", methods=["DELETE"])
def api_sr_verwijder(rapport_id):
    from wo_service_rapport import sr_verwijder
    sr_verwijder(rapport_id)
    return jsonify({"ok": True})


@app.route("/api/service-rapporten/<rapport_id>/zoek-tnummer", methods=["POST"])
def api_sr_zoek_tnummer(rapport_id):
    """Start een asynchrone PeopleSoft zoekopdracht voor het T-nummer."""
    from wo_service_rapport import sr_lees, zoek_tnummer_op_sn, sr_update
    data = sr_lees()
    rapport = next((r for r in data["rapporten"] if r["id"] == rapport_id), None)
    if not rapport:
        return jsonify({"ok": False, "fout": "Rapport niet gevonden"}), 404

    sn = rapport.get("sn", "")
    if not sn:
        return jsonify({"ok": False, "fout": "Geen serienummer beschikbaar"}), 400

    job_id = f"sr_tn_{rapport_id}"
    _sr_jobs[job_id] = {"status": "bezig", "log": [], "resultaat": None}

    def _run():
        log_fn = _sr_log_factory(job_id)
        res = zoek_tnummer_op_sn(sn, stap_log=log_fn)
        if res.get("ok"):
            sr_update(rapport_id,
                      t_nummer=res["t_nummer"],
                      t_omschrijving=res.get("omschrijving", ""),
                      status="bevestigd")
        _sr_jobs[job_id]["status"] = "klaar" if res.get("ok") else "fout"
        _sr_jobs[job_id]["resultaat"] = res

    import threading
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/service-rapporten/job/<job_id>")
def api_sr_job_status(job_id):
    job = _sr_jobs.get(job_id)
    if not job:
        return jsonify({"status": "onbekend", "log": [], "resultaat": None})
    return jsonify(job)


@app.route("/api/service-rapporten/<rapport_id>/wo-aanmaken", methods=["POST"])
def api_sr_wo_aanmaken(rapport_id):
    """Start WO-aanmaak voor een service-rapport."""
    from wo_service_rapport import sr_lees
    data = sr_lees()
    rapport = next((r for r in data["rapporten"] if r["id"] == rapport_id), None)
    if not rapport:
        return jsonify({"ok": False, "fout": "Rapport niet gevonden"}), 404
    if not rapport.get("t_nummer"):
        return jsonify({"ok": False, "fout": "Geen T-nummer — zoek eerst op"}), 400

    body = request.get_json(silent=True) or {}
    zet_uitgev = bool(body.get("zet_uitgev", True))

    job_id = f"sr_wo_{rapport_id}"
    _sr_jobs[job_id] = {"status": "bezig", "log": [], "resultaat": None}

    def _run():
        from wo_service_rapport import maak_wo_voor_service_rapport
        log_fn = _sr_log_factory(job_id)
        res = maak_wo_voor_service_rapport(rapport_id, stap_log=log_fn, zet_uitgev=zet_uitgev)
        _sr_jobs[job_id]["status"] = "klaar" if res.get("ok") else "fout"
        _sr_jobs[job_id]["resultaat"] = res

    import threading
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


# ── einde SERVICE RAPPORTEN ────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════
# TOESTEL WERKORDER (snelle WO, voor iedereen)
# ══════════════════════════════════════════════════════════════════

_tw_jobs: dict[str, dict] = {}  # job_id → {"bezig", "klaar", "log", "ok", "wo_id", "fout"}


@app.route("/toestel-werkorder")
def toestel_werkorder_pagina():
    return render_template("toestel_werkorder.html")


@app.route("/api/toestel-werkorder/zoek-tnummer", methods=["POST"])
def api_tw_zoek_tnummer():
    """Zoek het T-nummer op via verkortingsnummer (verrekijker in WO-formulier)."""
    payload           = request.get_json(force=True) or {}
    verkortingsnummer = (payload.get("verkortingsnummer") or "").strip()
    if not verkortingsnummer:
        return jsonify({"ok": False, "fout": "Geen verkortingsnummer opgegeven"}), 400

    from wo_toestel_werkorder import zoek_tnummer
    res = zoek_tnummer(verkortingsnummer)
    return jsonify(res)


@app.route("/api/toestel-werkorder/maak-wo", methods=["POST"])
def api_tw_maak_wo():
    """Start een asynchrone job die de snelle toestel-WO aanmaakt."""
    payload         = request.get_json(force=True) or {}
    t_nummer        = (payload.get("t_nummer") or "").strip()
    probleemmelding = (payload.get("probleemmelding") or "").strip()
    oplossing       = (payload.get("oplossing") or "").strip()
    uren            = (payload.get("uren") or "").strip()
    zet_uitgev      = bool(payload.get("zet_uitgev", True))

    if not (t_nummer and probleemmelding and oplossing and uren):
        return jsonify({"ok": False,
                        "fout": "t_nummer, probleemmelding, oplossing en uren zijn verplicht"}), 400

    try:
        from uitvoerder_utils import bepaal_uitvoerder
        cfg = load_config()
        gebruiker = bepaal_uitvoerder(cfg, cfg.get("username", ""))
    except Exception:
        gebruiker = ""

    import time
    job_id = str(int(time.time() * 1000))
    job    = {"bezig": True, "klaar": False, "log": [], "ok": False, "wo_id": None, "fout": None}
    _tw_jobs[job_id] = job

    def _stap_log(msg):
        job["log"].append(msg)

    def _run():
        from wo_toestel_werkorder import maak_wo, tw_log_toevoegen
        try:
            res = maak_wo(t_nummer, probleemmelding, oplossing, uren, stap_log=_stap_log, zet_uitgev=zet_uitgev)
            job["ok"]    = res.get("ok", False)
            job["wo_id"] = res.get("wo_id")
            if not res.get("ok"):
                job["fout"] = res.get("fout", "WO aanmaken mislukt")
            elif job["wo_id"]:
                tw_log_toevoegen(t_nummer, probleemmelding, oplossing, job["wo_id"], gebruiker)
        except Exception as e:
            job["fout"] = str(e)
        finally:
            job["bezig"] = False
            job["klaar"] = True

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/toestel-werkorder/direct", methods=["POST"])
def api_tw_direct():
    """Start een asynchrone job die zoeken + WO aanmaken in één stap doet."""
    payload           = request.get_json(force=True) or {}
    verkortingsnummer = (payload.get("verkortingsnummer") or "").strip()
    probleemmelding   = (payload.get("probleemmelding") or "").strip()
    oplossing         = (payload.get("oplossing") or "").strip()
    uren              = (payload.get("uren") or "").strip()
    zet_uitgev        = bool(payload.get("zet_uitgev", True))

    if not (verkortingsnummer and probleemmelding and oplossing and uren):
        return jsonify({"ok": False,
                        "fout": "verkortingsnummer, probleemmelding, oplossing en uren zijn verplicht"}), 400

    try:
        from uitvoerder_utils import bepaal_uitvoerder
        cfg = load_config()
        gebruiker = bepaal_uitvoerder(cfg, cfg.get("username", ""))
    except Exception:
        gebruiker = ""

    import time
    job_id = str(int(time.time() * 1000))
    job    = {"bezig": True, "klaar": False, "log": [], "ok": False,
              "wo_id": None, "fout": None, "t_nummer": None}
    _tw_jobs[job_id] = job

    def _stap_log(msg):
        job["log"].append(msg)

    def _run():
        from wo_toestel_werkorder import zoek_tnummer, maak_wo, tw_log_toevoegen
        try:
            # Stap 1: T-nummer opzoeken
            zoek_res = zoek_tnummer(verkortingsnummer, stap_log=_stap_log)
            if not zoek_res.get("ok"):
                job["fout"] = zoek_res.get("fout", f"Toestel '{verkortingsnummer}' niet gevonden")
                return
            t_nummer = zoek_res["t_nummer"]
            job["t_nummer"] = t_nummer  # frontend kan dit tonen terwijl WO loopt

            # Stap 2: WO aanmaken
            res = maak_wo(t_nummer, probleemmelding, oplossing, uren, stap_log=_stap_log, zet_uitgev=zet_uitgev)
            job["ok"]    = res.get("ok", False)
            job["wo_id"] = res.get("wo_id")
            if not res.get("ok"):
                job["fout"] = res.get("fout", "WO aanmaken mislukt")
            elif job["wo_id"]:
                tw_log_toevoegen(t_nummer, probleemmelding, oplossing, job["wo_id"], gebruiker)
        except Exception as e:
            job["fout"] = str(e)
        finally:
            job["bezig"] = False
            job["klaar"] = True

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/toestel-werkorder/job/<job_id>")
def api_tw_job_status(job_id):
    job = _tw_jobs.get(job_id)
    if job is None:
        return jsonify({"ok": False, "fout": "Job niet gevonden", "klaar": True}), 404
    return jsonify(job)


@app.route("/api/toestel-werkorder/log")
def api_tw_log():
    """Log van alle succesvol aangemaakte snelle werkorders (toestelnr, probleem, oplossing, datum, WO-nr)."""
    from wo_toestel_werkorder import tw_log_lezen
    return jsonify(tw_log_lezen())


# ── einde TOESTEL WERKORDER ─────────────────────────────────────────────────


def lees_laatste_log_regels(pad: str, n: int = 5) -> list[str]:
    try:
        p = Path(pad)
        if not p.exists():
            return ["(bestand niet gevonden)"]
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            blok = min(size, 8192)
            f.seek(-blok, 2)
            inhoud = f.read().decode("utf-8", errors="replace")
        regels = [r.strip() for r in inhoud.splitlines() if r.strip()]
        return regels[-n:] if regels else ["(leeg)"]
    except Exception as e:
        return [f"(fout: {e})"]


def lees_alerts() -> list:
    return _lees_json_veilig(ALERTS_PATH, [])


def _container_uit_regel(regel: str) -> str | None:
    r = regel.lower()
    for i in ["1.1", "1.2", "2.1", "2.2", "3.1", "3.2"]:
        if f"container {i}" in r:
            return f"container {i}"
    return None


def _trigger_ccs_wo_backend(container: str):
    """Start WO + ESS aanmaken vanuit de backend, zonder dat een browser open moet zijn."""
    try:
        cfg    = load_config()
        maximo = cfg.get("ccs_maximo", "").strip()
        if not maximo:
            log.warning(f"CCS backend trigger: ccs_maximo niet ingesteld in config.json — overgeslagen")
            return

        # Deduplicatie: max 1 actieve job per container
        dedup_key = f"ccs_dedup_{container.replace(' ', '_')}"
        bestaand  = app.config.get(dedup_key)
        if bestaand:
            leeftijd = (datetime.now() - bestaand["gestart"]).total_seconds()
            if bestaand["status"] in ("bezig",) or (bestaand["status"] == "klaar" and leeftijd < 600):
                log.info(f"CCS backend trigger: dedup — job {bestaand['job_id']} is {bestaand['status']}, {int(leeftijd)}s geleden")
                return

        job_id = f"ccs_wo_{container.replace(' ', '_')}_{datetime.now().strftime('%H%M%S')}"
        job    = {"status": "bezig", "log": [], "resultaat": None}
        app.config[f"ccs_job_{job_id}"] = job
        app.config[dedup_key] = {"job_id": job_id, "status": "bezig", "gestart": datetime.now()}
        log.info(f"CCS backend trigger: starten voor '{container}', maximo='{maximo}', job_id={job_id}")

        threading.Thread(
            target=_voer_ccs_aanmaken_uit,
            args=(container, maximo, job, dedup_key),
            daemon=True,
            name=f"ccs-wo-{container}",
        ).start()

    except Exception as e:
        log.error(f"CCS backend trigger setup fout: {e}")


# ── ESS bestellingen overzicht (bijbestelde vaten bij container-enabled) ────
def lees_ess_bestellingen() -> list:
    return _lees_json_veilig(ESS_BEST_PATH, [])


def log_ess_bestelling(container: str, wo_res: dict):
    """Voeg een regel toe aan het overzicht van automatisch bijbestelde vaten
    n.a.v. een 'enabled' container-event. Houdt zowel ESS- als WO-resultaat bij."""
    bestellingen = lees_ess_bestellingen()
    vat_info = wo_res.get("vat_info") or {}
    entry = {
        "id":             int(datetime.now().timestamp() * 1000),
        "tijdstip":       datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "pc":             socket.gethostname(),
        "container":      container,
        "label":          container,
        "vat_type":       vat_info.get("type"),
        "bestelnummer":   vat_info.get("bestelnummer"),
        "ess_ok":         wo_res.get("ess_ok"),
        "ess_aanvraag_id": wo_res.get("ess_aanvraag_id"),
        "ess_fout":       wo_res.get("ess_fout"),
        "wo_ok":          wo_res.get("ok"),
        "wo_id":          wo_res.get("wo_id"),
        "ess_overgeslagen": wo_res.get("ess_overgeslagen", False),
    }
    bestellingen.append(entry)
    _schrijf_json_atomisch(ESS_BEST_PATH, bestellingen)
    log.info(f"ESS bestelling overzicht: {entry}")

    # ── Als ESS-aanvraag NIET gelukt is: popup-alert zodat manueel besteld wordt ──
    # (niet als de bestelling bewust 1x overgeslagen werd via de toggle — dat is geen fout)
    if not wo_res.get("ess_ok") and not wo_res.get("ess_overgeslagen"):
        alerts = lees_alerts()
        alert = {
            "id":            int(datetime.now().timestamp() * 1000) + 1,
            "naam":          "ESS BESTELLING",
            "regel":         f"⚠ ESS-aanvraag mislukt voor {container} — manueel bestellen! "
                             f"({wo_res.get('ess_fout') or 'onbekende fout'})",
            "tijdstip":      datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            "persistent":    False,
            "ccs_container": None,
            "herstel":       False,
            "bevestigd":     False,
        }
        alerts.append(alert)
        _schrijf_json_atomisch(ALERTS_PATH, alerts)
        log.warning(f"ESS bestelling mislukt voor {container} — popup-alert toegevoegd")


def sla_alert_op(naam: str, regel: str):
    alerts = lees_alerts()


    if naam in CCS_INSTALLATIES:
        r         = regel.lower()
        container = _container_uit_regel(regel)
        if container and "enabled" in r:
            for a in alerts:
                if (a["naam"] == naam and not a.get("bevestigd")
                        and a.get("ccs_container") == container
                        and "disabled" in a["regel"].lower()):
                    a["bevestigd"] = True
            herstel = {
                "id":            int(datetime.now().timestamp() * 1000),
                "naam":          naam,
                "regel":         regel,
                "tijdstip":      datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                "persistent":    False,
                "ccs_container": container,
                "herstel":       True,
                "bevestigd":     False,
            }
            alerts.append(herstel)
            _schrijf_json_atomisch(ALERTS_PATH, alerts)
            # ── Backend trigger: WO + ESS aanmaken zonder browser nodig ──
            _trigger_ccs_wo_backend(container)
            return
        if not container or "disabled" not in r:
            return

    alert = {
        "id":            int(datetime.now().timestamp() * 1000),
        "naam":          naam,
        "regel":         regel,
        "tijdstip":      datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "persistent":    naam in RO_INSTALLATIES,
        "ccs_container": _container_uit_regel(regel) if naam in CCS_INSTALLATIES else None,
        "bevestigd":     False,
    }
    alerts.append(alert)
    _schrijf_json_atomisch(ALERTS_PATH, alerts)
    stuur_matrix_bericht(f"\u26a0\ufe0f ALARM [{naam}]\n{regel}\n\U0001f550 {alert['tijdstip']}")
    log.info(f"Alert opgeslagen: [{naam}] {regel}")


def bevestig_alert(alert_id: int):
    alerts = lees_alerts()
    for a in alerts:
        if a["id"] == alert_id:
            a["bevestigd"] = True
    _schrijf_json_atomisch(ALERTS_PATH, alerts)


class LogBestandWatcher(FileSystemEventHandler):
    def __init__(self, naam: str, pad: Path):
        self.naam      = naam
        self.pad       = pad
        self._positie  = pad.stat().st_size if pad.exists() else 0
        self._lock     = threading.Lock()

    def on_modified(self, event):
        if Path(event.src_path).resolve() != self.pad.resolve():
            return
        with self._lock:
            try:
                grootte = self.pad.stat().st_size
                if grootte <= self._positie:
                    return
                with open(self.pad, "rb") as f:
                    f.seek(self._positie)
                    nieuw = f.read(grootte - self._positie).decode("utf-8", errors="replace")
                self._positie = grootte
                for regel in nieuw.splitlines():
                    regel = regel.strip()
                    if not regel:
                        continue
                    log.info(f"Nieuwe logregel [{self.naam}]: {regel}")
                    sla_alert_op(self.naam, regel)
            except Exception as e:
                log.error(f"Fout bij lezen logbestand {self.naam}: {e}")


def start_ro_log_watchers():
    if not WATCHDOG_OK:
        log.warning("watchdog niet geinstalleerd — gebruik polling als fallback")
        return
    cfg        = load_config()
    log_config = cfg.get("error_logs", DEFAULT_ERROR_LOGS)
    observer   = Observer()
    for naam, pad_str in log_config.items():
        pad = Path(pad_str)
        if not pad.parent.exists():
            log.warning(f"Log map niet gevonden, watcher overgeslagen: {pad.parent}")
            continue
        handler = LogBestandWatcher(naam, pad)
        observer.schedule(handler, str(pad.parent), recursive=False)
        log.info(f"Log watcher actief: [{naam}] {pad}")
    observer.start()
    log.info("RO/HR/CCS log watchers gestart")


def schrijf_error_logs_json():
    cfg        = load_config()
    log_config = cfg.get("error_logs", DEFAULT_ERROR_LOGS)
    resultaat  = {}
    for naam, pad in log_config.items():
        regels     = lees_laatste_log_regels(pad, 5)
        laatste    = regels[-1] if regels else ""
        heeft_fout = any(w in laatste.lower() for w in FOUT_WOORDEN)
        resultaat[naam] = {"pad": pad, "regels": regels, "heeft_fout": heeft_fout}
    payload = {
        "laatste_update": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "installaties":   resultaat,
    }
    with open(ERROR_LOGS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log.info("error_logs.json bijgewerkt")


def lees_error_logs_json() -> dict:
    if not ERROR_LOGS_PATH.exists():
        return {"laatste_update": None, "installaties": {}}
    with open(ERROR_LOGS_PATH, encoding="utf-8") as f:
        return json.load(f)


def start_error_log_loop(interval_seconden: int = 60):
    def _loop():
        while True:
            try:
                schrijf_error_logs_json()
            except Exception as e:
                log.error(f"Fout bij schrijven error_logs.json: {e}")
            __import__("time").sleep(interval_seconden)
    threading.Thread(target=_loop, daemon=True, name="error-log-loop").start()
    log.info(f"Error log watcher gestart (elke {interval_seconden}s)")


@app.route("/api/error_logs")
def api_error_logs():
    data = lees_error_logs_json()
    return jsonify(data.get("installaties", {}))


@app.route("/api/ro_log_timestamp")
def api_ro_log_timestamp():
    """Lichtgewicht endpoint — geeft enkel de laatste_update timestamp terug.
    De frontend gebruikt dit om te beslissen of een volledige fetch nodig is."""
    data = lees_ro_log_data_json()
    return jsonify({"laatste_update": data.get("laatste_update")})


@app.route("/api/ro_log")
def api_ro_log():
    """Real-time log voor één RO installatie — leest uit ro_log_data.json (geschreven door master).

    Query params:
      naam  — installatienaam (bv. "RO 304" of "RO304" — spaties worden genormaliseerd)
      n     — aantal laatste rijen terug te geven (default 60, max 500)
    """
    naam_raw = request.args.get("naam", "").strip()
    n        = min(int(request.args.get("n", 60)), 500)

    data = lees_ro_log_data_json()
    inst = data.get("installaties", {})

    # Zoek installatie: exact of spatie-insensitief
    gevonden = None
    for cfg_naam, waarde in inst.items():
        if cfg_naam == naam_raw or cfg_naam.replace(" ", "") == naam_raw.replace(" ", ""):
            gevonden  = (cfg_naam, waarde)
            break

    if gevonden is None:
        return jsonify({"error": f"Onbekende installatie: {naam_raw}", "rows": []}), 404

    naam_match, waarde = gevonden
    rows   = waarde.get("rows", [])[-n:]
    latest = rows[-1] if rows else None
    return jsonify({"naam": naam_match, "rows": rows, "latest": latest})


def _parse_ro_log_regels(regels_raw: list[str]) -> list[dict]:
    """Parst ruwe data-logregels naar gestructureerde dicts.

    Twee formaten worden auto-gedetecteerd per regel:

    FORMAAT A — RO304 (kolom 7 = op_mode, kolom 8 = op_phase, gescheiden velden):
      0: datum+tijd  (DD.MM.YYYY HH:MM:SS)
      1: cond_rw     [µS/cm]
      2: cond_conc   [µS/cm]
      3: cond_perm   [µS/cm]
      4: temp_conc   [°C]
      5: TIS2  6: TIS4
      7: op_mode     (Operation / Standby / Hot Disinfection / System OFF)
      8: op_phase
      9+: bits

    FORMAAT B — RO1/RO2 (kolom 11 = "Hoofd/Sub", kolom 6 = cond_perm met komma):
      0: datum+tijd  (DD.MM.YYYY HH:MM:SS)
      1: flow upstream  2: flow downstream  3: flow conc. dis.
      4: perm. CIS1 [uS/cm]
      5: temperature RO2
      6: cond. perm [µS/cm]   ← decimaal met komma (bv. 7,0)
      7: temperature RO1
      8: pressure conc. RO1   9: pressure perm. RO1   10: pressure conc. RO2
      11: Op. mode  (bv. "Operation/Operation", "Standby/HWD2", "Hot RO I+II/heating")
      12+: bits
    """
    rows = []

    def _float_komma(v):
        """Parst decimalen met komma (bv. '7,0') of punt."""
        try:
            return float(v.replace(",", "."))
        except Exception:
            return None

    def _int(v):
        try:
            return int(v)
        except Exception:
            return None

    for r in regels_raw:
        delen = [d.strip() for d in r.split("\t")]

        # Minimaal 9 kolommen vereist
        if len(delen) < 9:
            continue

        ts = delen[0]

        # ── Formaat-detectie ──────────────────────────────────────────────────
        # Formaat B: kolom 11 bestaat en bevat een slash (bv. "Operation/Operation")
        if len(delen) > 11 and "/" in delen[11]:
            combo     = delen[11]          # bv. "Operation/Operation", "Standby/HWD2"
            slash_pos = combo.find("/")
            op_mode   = combo[:slash_pos].strip()
            op_phase  = combo[slash_pos + 1:].strip()

            cond_perm_f = _float_komma(delen[6]) if len(delen) > 6 else None
            cond_perm   = round(cond_perm_f, 1) if cond_perm_f is not None else None
            temp_conc   = _float_komma(delen[5]) if len(delen) > 5 else None  # temp RO2
            temp_ro1    = _float_komma(delen[7]) if len(delen) > 7 else None

        else:
            # Formaat A: RO304
            op_mode   = delen[7] if len(delen) > 7 else ""
            op_phase  = delen[8] if len(delen) > 8 else ""
            cond_perm = _int(delen[3]) if len(delen) > 3 else None
            temp_conc = _int(delen[4]) if len(delen) > 4 else None
            temp_ro1  = None

        # ── Mode-normalisatie voor kaartje-weergave ───────────────────────────
        # Standby/HWD2 of Hot RO.../... → "Hitte"
        # Overige Standby → "Nacht"
        mode_lower = op_mode.lower()
        phase_lower = op_phase.lower()
        if "hot ro" in mode_lower or "hwd" in phase_lower:
            label = "Hitte RO" if "hot ro" in mode_lower else "Hitte"
        elif mode_lower == "operation":
            label = "RUN"
        elif mode_lower == "standby":
            label = "Nacht"
        elif "system off" in mode_lower:
            label = "OFF"
        elif "initial" in mode_lower:
            label = "Init"
        else:
            label = op_mode

        # Alarm: cond_perm > 15 µS/cm tijdens actieve werking
        alarm = (cond_perm is not None and cond_perm > 15
                 and mode_lower == "operation")

        rows.append({
            "ts":        ts,
            "op_mode":   op_mode,
            "op_phase":  op_phase,
            "label":     label,
            "cond_perm": cond_perm,
            "temp_conc": temp_conc,
            "temp_ro1":  temp_ro1,
            "alarm":     alarm,
        })
    return rows


def schrijf_ro_logs_json(n: int = 200):
    """Leest alle ro_logs paden en schrijft geparsede data naar ro_log_data.json.
    Wordt enkel aangeroepen op de scraper-master.
    """
    cfg        = load_config()
    ro_logs    = cfg.get("ro_logs", {})
    resultaat  = {}
    for naam, pad in ro_logs.items():
        regels_raw = lees_laatste_log_regels(pad, n)
        rows       = _parse_ro_log_regels(regels_raw)
        latest     = rows[-1] if rows else None
        resultaat[naam] = {"pad": pad, "rows": rows, "latest": latest}
    payload = {
        "laatste_update": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "installaties":   resultaat,
    }
    with open(RO_LOG_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log.info(f"ro_log_data.json bijgewerkt ({len(resultaat)} installaties)")


def lees_ro_log_data_json() -> dict:
    if not RO_LOG_DATA_PATH.exists():
        return {"laatste_update": None, "installaties": {}}
    with open(RO_LOG_DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


def start_ro_log_loop(interval_seconden: int = 60):
    """Polling loop die ro_log_data.json periodiek bijwerkt. Enkel op de master."""
    def _loop():
        while True:
            try:
                schrijf_ro_logs_json()
            except Exception as e:
                log.error(f"Fout bij schrijven ro_log_data.json: {e}")
            __import__("time").sleep(interval_seconden)
    threading.Thread(target=_loop, daemon=True, name="ro-log-loop").start()
    log.info(f"RO log data loop gestart (elke {interval_seconden}s)")


# ── HR (Heat Disinfection / Hot Rinse) log parsing ──────────────────────────

HR_TEMP_ALARM     = 15   # °C — drempel temp einde ring voor HR1/HR2 465 (kolom 3)
HR_TEMP_ALARM_304 = 80   # °C — drempel voor HR 304 (kolom 1/2 = werkelijke ringtemperatuur)

# Installatienamen waarvan de log het HR304-formaat heeft:
#   kolom 3 is altijd 0; de werkelijke temperaturen zitten in kolom 1 en 2.
HR_FORMAAT_304 = {"HR 304", "HR304"}


def _parse_hr_log_regels(regels_raw: list[str], naam: str = "") -> list[dict]:
    """Parst ruwe HR-data-logregels naar gestructureerde dicts.

    Formaat HR1/HR2 465 (kolommen 0-indexed, tab-gescheiden):
      0: datum+tijd  (DD.MM.YYYY HH:MM:SS)
      1: temperatuur 1
      2: temperatuur 2
      3: temperatuur einde ring  [°C]   ← gebruikt als temp_ring
      4: volume/flow
      5: waarde
      6: cond/decimaal 1 (komma)
      7: cond/decimaal 2 (komma)
      8: Op. mode   (bv. "System OFF", "Heat disinfection (A)")
      9: Op. phase  (bv. "System OFF", "Heating", "PHD1", "Inline1", "Cooling phase", "Active")
      10+: status-bits

    Formaat HR 304 (kolom 3 is altijd 0 — geen aparte temp-einde-ring sensor):
      0: datum+tijd
      1: temp1 [°C]  ← max(temp1, temp2) wordt gebruikt als temp_ring
      2: temp2 [°C]
      3: 0  (ongebruikt)
      4: 0  (ongebruikt)
      5: flow
      6: cond 1
      7: cond 2
      8: Op. mode
      9: Op. phase
      10+: status-bits
    """
    is_304 = naam.replace(" ", "") in {n.replace(" ", "") for n in HR_FORMAAT_304}
    drempel = HR_TEMP_ALARM_304 if is_304 else HR_TEMP_ALARM

    rows = []

    def _int(v):
        try:
            return int(v)
        except Exception:
            return None

    for r in regels_raw:
        delen = [d.strip() for d in r.split("\t")]
        if len(delen) < 10:
            continue

        ts      = delen[0]
        op_mode = delen[8] if len(delen) > 8 else ""
        op_phase = delen[9] if len(delen) > 9 else ""

        if is_304:
            # Geen dedicated temp-einde-ring sensor: gebruik max van kolom 1 en 2
            temp1 = _int(delen[1]) if len(delen) > 1 else None
            temp2 = _int(delen[2]) if len(delen) > 2 else None
            kandidaten = [t for t in [temp1, temp2] if t is not None]
            temp_ring  = max(kandidaten) if kandidaten else None
        else:
            temp_ring = _int(delen[3]) if len(delen) > 3 else None

        # Alarm: temp boven drempel tijdens actieve werking (niet System OFF)
        alarm = (temp_ring is not None and temp_ring > drempel
                 and op_mode.lower() != "system off")

        rows.append({
            "ts":        ts,
            "op_mode":   op_mode,
            "op_phase":  op_phase,
            "temp_ring": temp_ring,
            "alarm":     alarm,
        })
    return rows


def schrijf_hr_logs_json(n: int = 200):
    """Leest alle hr_logs paden en schrijft geparsede data naar hr_log_data.json.
    Wordt enkel aangeroepen op de scraper-master.
    """
    cfg        = load_config()
    hr_logs    = cfg.get("hr_logs", {})
    resultaat  = {}
    for naam, pad in hr_logs.items():
        regels_raw = lees_laatste_log_regels(pad, n)
        rows       = _parse_hr_log_regels(regels_raw, naam=naam)
        latest     = rows[-1] if rows else None
        resultaat[naam] = {"pad": pad, "rows": rows, "latest": latest}
    payload = {
        "laatste_update": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "installaties":   resultaat,
    }
    with open(HR_LOG_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log.info(f"hr_log_data.json bijgewerkt ({len(resultaat)} installaties)")


def lees_hr_log_data_json() -> dict:
    if not HR_LOG_DATA_PATH.exists():
        return {"laatste_update": None, "installaties": {}}
    with open(HR_LOG_DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


def start_hr_log_loop(interval_seconden: int = 60):
    """Polling loop die hr_log_data.json periodiek bijwerkt. Enkel op de master."""
    def _loop():
        while True:
            try:
                schrijf_hr_logs_json()
            except Exception as e:
                log.error(f"Fout bij schrijven hr_log_data.json: {e}")
            __import__("time").sleep(interval_seconden)
    threading.Thread(target=_loop, daemon=True, name="hr-log-loop").start()
    log.info(f"HR log data loop gestart (elke {interval_seconden}s)")


@app.route("/api/hr_log_timestamp")
def api_hr_log_timestamp():
    """Lichtgewicht endpoint — geeft enkel de laatste_update timestamp terug."""
    data = lees_hr_log_data_json()
    return jsonify({"laatste_update": data.get("laatste_update")})


@app.route("/api/hr_log")
def api_hr_log():
    """Real-time log voor één HR installatie — leest uit hr_log_data.json (geschreven door master).

    Query params:
      naam  — installatienaam (bv. "HR 304" of "HR304" — spaties worden genormaliseerd)
      n     — aantal laatste rijen terug te geven (default 60, max 500)
    """
    naam_raw = request.args.get("naam", "").strip()
    n        = min(int(request.args.get("n", 60)), 500)

    data = lees_hr_log_data_json()
    inst = data.get("installaties", {})

    gevonden = None
    for cfg_naam, waarde in inst.items():
        if cfg_naam == naam_raw or cfg_naam.replace(" ", "") == naam_raw.replace(" ", ""):
            gevonden  = (cfg_naam, waarde)
            break

    if gevonden is None:
        return jsonify({"error": f"Onbekende installatie: {naam_raw}", "rows": []}), 404

    naam_match, waarde = gevonden
    rows   = waarde.get("rows", [])[-n:]
    latest = rows[-1] if rows else None
    return jsonify({"naam": naam_match, "rows": rows, "latest": latest})


@app.route("/api/hr_logs_config", methods=["GET"])
def api_hr_logs_config_get():
    """Geeft de huidige hr_logs configuratie terug als lijst van {naam, pad} objecten."""
    cfg     = load_config()
    hr_logs = cfg.get("hr_logs", {})
    items   = [{"naam": naam, "pad": pad} for naam, pad in hr_logs.items()]
    return jsonify({"items": items})


@app.route("/api/hr_logs_config", methods=["POST"])
def api_hr_logs_config_post():
    """Slaat een nieuwe hr_logs configuratie op.

    Body: { "items": [{"naam": "HR 304", "pad": "Z:\\..."}, ...] }
    """
    payload = request.get_json(force=True)
    items   = payload.get("items", [])
    cfg     = load_config()
    cfg["hr_logs"] = {i["naam"].strip(): i["pad"].strip() for i in items if i.get("naam") and i.get("pad")}
    save_config(cfg)
    log.info(f"hr_logs config opgeslagen: {list(cfg['hr_logs'].keys())}")
    return jsonify({"ok": True})


def _parse_ccs_log_regels(regels_raw: list[str]) -> list[dict]:
    """Parst CCS-logregels.

    Kolommen (0-indexed, tab-gescheiden):
      0:    datum+tijd
      1..6: rpm (6 velden)
      7..12: pressure (6 velden)
      13: Op. mode 1.1   14: Op. mode 1.2
      15: Op. mode 2.1   16: Op. mode 2.2
      17: Op. mode 3.1   18: Op. mode 3.2
      19+: status-bits

    Vaten: 1=K2 Ca1.25, 2=K3 Ca1.25, 3=K3 Ca1.50
    Verdiep: .1=verdiep 1, .2=verdiep 2
    Status: "On" → AAN, "Saver circuit" → PAUZE
    """
    rows = []
    for r in regels_raw:
        delen = [d.strip() for d in r.split("\t")]
        if len(delen) < 19:
            continue
        rows.append({
            "ts": delen[0],
            "circuits": {
                "1.1": delen[13],
                "1.2": delen[14],
                "2.1": delen[15],
                "2.2": delen[16],
                "3.1": delen[17],
                "3.2": delen[18],
            },
        })
    return rows


def schrijf_ccs_log_json(n: int = 200):
    cfg = load_config()
    pad = cfg.get("ccs_log", r"Z:\LOGGING LAUER\Virtual Machine Log Files\CCS\CCS_data.txt")
    regels_raw = lees_laatste_log_regels(pad, n)
    rows   = _parse_ccs_log_regels(regels_raw)
    latest = rows[-1] if rows else None
    payload = {
        "laatste_update": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "pad":    pad,
        "rows":   rows,
        "latest": latest,
    }
    with open(CCS_LOG_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log.info("ccs_log_data.json bijgewerkt")


def lees_ccs_log_data_json() -> dict:
    if not CCS_LOG_DATA_PATH.exists():
        return {"laatste_update": None, "pad": "", "rows": [], "latest": None}
    with open(CCS_LOG_DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


def start_ccs_log_loop(interval_seconden: int = 60):
    def _loop():
        while True:
            try:
                schrijf_ccs_log_json()
            except Exception as e:
                log.error(f"Fout bij schrijven ccs_log_data.json: {e}")
            __import__("time").sleep(interval_seconden)
    threading.Thread(target=_loop, daemon=True, name="ccs-log-loop").start()
    log.info(f"CCS log data loop gestart (elke {interval_seconden}s)")


@app.route("/api/ccs_log_data")
def api_ccs_log_data():
    return jsonify(lees_ccs_log_data_json())


@app.route("/api/ro_logs_config", methods=["GET"])
def api_ro_logs_config_get():
    """Geeft de huidige ro_logs configuratie terug als lijst van {naam, pad} objecten."""
    cfg     = load_config()
    ro_logs = cfg.get("ro_logs", {})
    items   = [{"naam": naam, "pad": pad} for naam, pad in ro_logs.items()]
    return jsonify({"items": items})


@app.route("/api/ro_logs_config", methods=["POST"])
def api_ro_logs_config_post():
    """Slaat een nieuwe ro_logs configuratie op.

    Body: { "items": [{"naam": "RO 304", "pad": "Z:\\..."}, ...] }
    """
    payload = request.get_json(force=True)
    items   = payload.get("items", [])
    cfg     = load_config()
    cfg["ro_logs"] = {i["naam"].strip(): i["pad"].strip() for i in items if i.get("naam") and i.get("pad")}
    save_config(cfg)
    log.info(f"ro_logs config opgeslagen: {list(cfg['ro_logs'].keys())}")
    return jsonify({"ok": True})


@app.route("/api/alerts")
def api_alerts():
    alerts = [a for a in lees_alerts() if not a["bevestigd"]]
    return jsonify(alerts)


@app.route("/api/ess-bestellingen")
def api_ess_bestellingen():
    """Overzicht van automatisch bijbestelde vaten (ESS) bij container-enabled events."""
    bestellingen = lees_ess_bestellingen()
    bestellingen.sort(key=lambda b: b.get("id", 0), reverse=True)
    return jsonify(bestellingen)


@app.route("/api/alerts/bevestig/<int:alert_id>", methods=["POST"])
def api_alert_bevestig(alert_id):
    bevestig_alert(alert_id)
    return jsonify({"ok": True})


@app.route("/api/alerts/bevestig_alle", methods=["POST"])
def api_alerts_bevestig_alle():
    alerts = lees_alerts()
    for a in alerts:
        # CCS container alerts die nog actief zijn (geen herstel) NIET bevestigen —
        # die moeten blijven staan tot de container terug enabled is.
        if a.get("ccs_container") and not a.get("herstel"):
            continue
        a["bevestigd"] = True
    _schrijf_json_atomisch(ALERTS_PATH, alerts)
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════
# RADIO
# ══════════════════════════════════════════════════════════════════

STATIONS = {
    "qmusic":         ["https://streams.radio.dpgmedia.cloud/redirect/qmusic_be/aac",
                       "https://streams.radio.dpgmedia.cloud/redirect/qmusic_be/mp3"],
    "foute":          ["https://streams.radio.dpgmedia.cloud/redirect/foute_radio_be/aac",
                       "https://streams.radio.dpgmedia.cloud/redirect/foute_radio_be/mp3"],
    "allstars":       ["https://streams.radio.dpgmedia.cloud/redirect/qbe_allstars/aac",
                       "https://streams.radio.dpgmedia.cloud/redirect/qbe_allstars/mp3"],
    "radio1":         ["http://icecast.vrtcdn.be/radio1-high.mp3",
                       "http://icecast.vrtcdn.be/radio1.aac"],
    "radio1classics": ["http://icecast.vrtcdn.be/radio1_classics-high.mp3"],
    "radio2vlb":      ["http://icecast.vrtcdn.be/ra2vlb-high.mp3"],
    "tijdloze":       ["http://icecast.vrtcdn.be/stubru_tijdloze-high.mp3"],
    "stubru":         ["http://icecast.vrtcdn.be/stubru-high.mp3"],
    "mnm":            ["http://icecast.vrtcdn.be/mnm-high.mp3"],
    "mnmhits":        ["http://icecast.vrtcdn.be/mnm_hits-high.mp3"],
    "nostalgie":      ["https://playerservices.streamtheworld.com/api/livestream-redirect/"
                       "NOSTALGIEWHATAFEELING.mp3"],
    "joefm":          ["https://streams.radio.dpgmedia.cloud/redirect/joe_fm/aac",
                       "https://streams.radio.dpgmedia.cloud/redirect/joe_fm/mp3"],
    "oneworldradio":  ["https://playerservices.streamtheworld.com/api/livestream-redirect/"
                       "OWR_DAB.mp3"],
}


@app.route("/api/radio/stream")
def api_radio_stream():
    import requests as req_lib
    from flask import Response, stream_with_context

    station    = request.args.get("station", "qmusic")
    kandidaten = STATIONS.get(station, STATIONS["qmusic"])
    HEADERS    = {"User-Agent": "Mozilla/5.0", "Icy-MetaData": "0"}

    resp = None
    for url in kandidaten:
        try:
            r = req_lib.get(url, headers=HEADERS, stream=True, timeout=8, allow_redirects=True)
            if r.status_code == 200:
                resp = r
                break
            r.close()
        except Exception as e:
            log.warning(f"Stream {url} mislukt: {e}")

    if resp is None:
        return jsonify({"error": f"Geen werkende stream gevonden voor {station}"}), 503

    def generate():
        try:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        finally:
            resp.close()

    return Response(
        stream_with_context(generate()),
        mimetype=resp.headers.get("Content-Type", "audio/mpeg"),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/radio/metadata")
def api_radio_metadata():
    import requests as req_lib

    METADATA_STATIONS = {k: [u for u in v if u.endswith(".mp3")]
                         for k, v in STATIONS.items()}

    station = request.args.get("station", "qmusic")
    urls    = METADATA_STATIONS.get(station, [])
    HEADERS = {"User-Agent": "Mozilla/5.0", "Icy-MetaData": "1"}

    for url in urls:
        try:
            r = req_lib.get(url, headers=HEADERS, stream=True, timeout=8, allow_redirects=True)
            if r.status_code != 200:
                r.close()
                continue
            metaint = int(r.headers.get("icy-metaint", 0))
            if metaint == 0:
                r.close()
                return jsonify({"title": "", "artist": ""})
            audio_data = b""
            for chunk in r.iter_content(chunk_size=4096):
                audio_data += chunk
                if len(audio_data) >= metaint + 1:
                    break
            r.close()
            if len(audio_data) < metaint + 1:
                return jsonify({"title": "", "artist": ""})
            meta_length = audio_data[metaint] * 16
            if meta_length == 0:
                return jsonify({"title": "", "artist": ""})
            meta_start = metaint + 1
            meta_end   = meta_start + meta_length
            if len(audio_data) < meta_end:
                return jsonify({"title": "", "artist": ""})
            meta_raw     = audio_data[meta_start:meta_end].rstrip(b"\x00").decode("utf-8", errors="replace")
            stream_title = ""
            for part in meta_raw.split(";"):
                if part.strip().startswith("StreamTitle="):
                    stream_title = part.strip()[len("StreamTitle="):].strip("'\"")
                    break
            if " - " in stream_title:
                artist, title = stream_title.split(" - ", 1)
            else:
                artist, title = "", stream_title
            return jsonify({"title": title.strip(), "artist": artist.strip(), "raw": stream_title})
        except Exception as e:
            log.warning(f"Metadata fetch mislukt voor {station}: {e}")

    return jsonify({"title": "", "artist": ""})


@app.route("/api/radio", methods=["GET", "POST"])
def api_radio():
    import scraper as _scraper
    import time

    def _is_playing() -> bool:
        p = _scraper._radio_process
        return p is not None and p.poll() is None

    if request.method == "GET":
        return jsonify({"playing": _is_playing()})

    actie = (request.get_json(force=True) or {}).get("actie", "")
    if actie == "start":
        if not _is_playing():
            start_radio()
            for _ in range(15):
                time.sleep(0.2)
                if _is_playing():
                    break
    elif actie == "stop":
        if _is_playing():
            stop_radio()
            for _ in range(15):
                time.sleep(0.2)
                if not _is_playing():
                    break
    return jsonify({"playing": _is_playing()})


# ══════════════════════════════════════════════════════════════════
# LOGS PAGINA
# ══════════════════════════════════════════════════════════════════

# Alle bekende logbestanden: id → {naam, pad}
# Paden relatief aan de projectmap, behalve ess/ccs die in logs/ zitten.
def _log_bestanden() -> list[dict]:
    basis = Path(__file__).parent
    bestanden = [
        {"id": "app_log",          "naam": "App Log (live)",      "pad": basis / "logs" / "app.log"},
        {"id": "lage_temp",        "naam": "Lage Temperatuur",    "pad": basis / "lage_temp_log.txt"},
        {"id": "melkkoppelingen",  "naam": "Melkkoppelingen",     "pad": basis / "melkkoppelingen_log.txt"},
        {"id": "ccs_wo",           "naam": "CCS Werkorders",      "pad": basis / "logs" / "ccs_wo.log"},
        {"id": "ess_bestelling",   "naam": "ESS Bestellingen",    "pad": basis / "logs" / "ess_bestelling.log"},
    ]
    return bestanden


@app.route("/logs")
def logs_pagina():
    return render_template("logs.html")


@app.route("/api/logs/lijst")
def api_logs_lijst():
    """Geeft metadata van alle logbestanden terug."""
    resultaat = []
    for b in _log_bestanden():
        pad = b["pad"]
        if pad.exists():
            try:
                stat      = pad.stat()
                grootte   = stat.st_size
                with open(pad, encoding="utf-8", errors="replace") as f:
                    regels = f.readlines()
                laatste_regel = regels[-1].strip() if regels else ""
                # Probeer timestamp te extraheren uit laatste regel
                import re as _re
                ts_match = _re.match(r'\[(\d{2}[/:]\d{2}[/:]\d{4}[^]]*)\]', laatste_regel)
                laatste_ts = ts_match.group(1) if ts_match else ""
                resultaat.append({
                    "id":           b["id"],
                    "naam":         b["naam"],
                    "pad":          str(pad),
                    "bestaat":      True,
                    "grootte_kb":   round(grootte / 1024, 1),
                    "regels":       len(regels),
                    "laatste_regel": laatste_regel[:80],
                    "laatste_ts":   laatste_ts,
                })
            except Exception as e:
                resultaat.append({
                    "id": b["id"], "naam": b["naam"], "pad": str(pad),
                    "bestaat": True, "grootte_kb": 0, "regels": 0,
                    "laatste_regel": f"(leesfout: {e})", "laatste_ts": "",
                })
        else:
            resultaat.append({
                "id": b["id"], "naam": b["naam"], "pad": str(pad),
                "bestaat": False, "grootte_kb": 0, "regels": 0,
                "laatste_regel": "", "laatste_ts": "",
            })
    return jsonify({"ok": True, "logs": resultaat})


@app.route("/api/logs/lees/<log_id>")
def api_logs_lees(log_id):
    """Geeft de laatste N regels van een logbestand terug.
    Query param: n (default 100, 0 = alles, max 2000)
    """
    bestand = next((b for b in _log_bestanden() if b["id"] == log_id), None)
    if not bestand:
        return jsonify({"ok": False, "fout": f"Onbekend log-id: {log_id}"}), 404
    pad = bestand["pad"]
    if not pad.exists():
        return jsonify({"ok": False, "fout": "Logbestand bestaat nog niet"}), 404
    try:
        n = int(request.args.get("n", 100))
        with open(pad, encoding="utf-8", errors="replace") as f:
            alle_regels = [r.rstrip() for r in f.readlines()]
        if n == 0:
            regels = alle_regels
        else:
            n = min(n, 2000)
            regels = alle_regels[-n:]
        return jsonify({
            "ok":     True,
            "regels": regels,
            "totaal": len(alle_regels),
            "pad":    str(pad),
        })
    except Exception as e:
        return jsonify({"ok": False, "fout": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# SHUTDOWN
# ══════════════════════════════════════════════════════════════════

@app.route("/api/shutdown", methods=["GET", "POST"])
def api_shutdown():
    """Stop de server — wordt aangeroepen door een nieuwe instantie om de vorige te sluiten."""
    def _stop():
        import time, os, signal, subprocess
        time.sleep(0.3)
        # Sluit eventuele achtergebleven headless Edge/Chromium processen
        try:
            subprocess.call(
                ["taskkill", "/F", "/IM", "msedge.exe", "/FI", "WINDOWTITLE eq *headless*"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception:
            pass
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_stop, daemon=True).start()
    return "Afsluiten...", 200


# ══════════════════════════════════════════════════════════════════
# START
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import atexit
    atexit.register(verwijder_lock)   # lock opruimen bij normaal afsluiten
    try:
        from onedrive_sync import stop_onedrive_sync
        atexit.register(stop_onedrive_sync)
    except Exception:
        pass

    cfg = load_config()
    port = cfg.get("server_port", 5000)

    # ── Vorige instantie stoppen ──────────────────────────────────────────────
    try:
        import urllib.request
        urllib.request.urlopen(f"http://localhost:{port}/api/shutdown", timeout=2)
        import time
        time.sleep(1)  # even wachten tot die poort vrij is
    except Exception:
        pass  # geen vorige instantie actief, gewoon doorgaan

    start_background_scraper()
    log.info(f"Dashboard beschikbaar op http://0.0.0.0:{port}")
    log.info(f"Thuisdialyse beschikbaar op http://0.0.0.0:{port}/thuisdialyse")
    log.info(f"RO Staalnames beschikbaar op http://0.0.0.0:{port}/ro-staalname")
    log.info("Collega's kunnen verbinden via http://<jouw-ip>:5000")

    # Browser openen — zoek Chrome of Edge, open als nieuw tabblad in bestaand venster
    def _open_browser():
        import subprocess, shutil
        url = f"http://localhost:{port}"
        # Chrome: --new-tab opent tabblad in bestaand venster
        chrome = (
            shutil.which("chrome") or
            shutil.which("google-chrome") or
            r"C:\Program Files\Google\Chrome\Application\chrome.exe" if __import__("os.path", fromlist=["exists"]).exists(r"C:\Program Files\Google\Chrome\Application\chrome.exe") else None or
            shutil.which("msedge") or
            shutil.which("microsoft-edge")
        )
        if chrome and __import__("os.path", fromlist=["exists"]).exists(chrome):
            subprocess.Popen([chrome, f"--new-tab", url], shell=False)
        else:
            webbrowser.open_new_tab(url)

    threading.Timer(1.5, _open_browser).start()
    try:
        app.run(
            host=cfg.get("server_host", "0.0.0.0"),
            port=port,
            debug=_lees_debug_modus(),
            use_reloader=False,
        )
    finally:
        verwijder_lock()   # ook bij Ctrl+C of crash
        try:
            from onedrive_sync import stop_onedrive_sync
            stop_onedrive_sync()
        except Exception:
            pass
