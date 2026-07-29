"""
start.py — Auto-restart wrapper voor het Werkorder Dashboard
============================================================
Start dit script in plaats van app.py:

    python start.py

Het herstart app.py automatisch zodra een .py of .html bestand
in de map gewijzigd wordt (bv. na een git pull of handmatige aanpassing).

Genegeerd (triggeren nooit een herstart):
  - Alle .json bestanden  (app schrijft continu: data.json, config.json,
                           thuisdialyse.json, ro_staalname.json,
                           dialyse_resultaten_data.json, ess_bestellingen.json,
                           ro_resultaten_data.json, error_logs.json,
                           ro_log_data.json, hr_log_data.json,
                           ccs_log_data.json, error_alerts.json,
                           service_rapporten.json)
  - Alle .log bestanden   (app schrijft continu naar logs/)
  - Alle .txt bestanden   (lage_temp_log.txt, melkkoppelingen_log.txt, ...)
  - Alle .lock bestanden  (scraper.lock)
  - Alle .key bestanden   (secret.key)
  - Alle .pyc bestanden   (__pycache__)
  - De mappen:            logs/, __pycache__/, uploads/
"""

import sys
import time
import threading
import subprocess
import logging
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WATCHER] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

WATCH_DIR     = Path(__file__).parent
RESTART_DELAY = 1.5   # seconden wachten na wijziging (voorkomt dubbele herstart bij opslaan)

# Enkel .py en .html bestanden triggeren een herstart.
# Alles wat de app zelf schrijft wordt uitgesloten via NEGEER_EXTS of NEGEER_MAPPEN.
WATCH_EXTS = {".py", ".html"}

# Extensies die NOOIT een herstart triggeren (app schrijft deze zelf)
NEGEER_EXTS = {
    ".json",   # alle datajson bestanden
    ".log",    # logbestanden in logs/
    ".txt",    # lage_temp_log.txt, melkkoppelingen_log.txt, command.txt, ...
    ".lock",   # scraper.lock
    ".key",    # secret.key
    ".pyc",    # gecompileerde Python bytecode
}

# Mappen die volledig genegeerd worden
NEGEER_MAPPEN = {
    "logs",
    "__pycache__",
    "uploads",
    ".git",
}

# Extra zekerheid: specifieke bestandsnamen in de root die nooit triggeren
NEGEER_BESTANDEN = {
    "config.json",
    "data.json",
    "thuisdialyse.json",
    "ro_staalname.json",
    "dialyse_resultaten_data.json",
    "ess_bestellingen.json",
    "ro_resultaten_data.json",
    "error_logs.json",
    "ro_log_data.json",
    "hr_log_data.json",
    "ccs_log_data.json",
    "error_alerts.json",
    "service_rapporten.json",
    "scraper.lock",
    "secret.key",
    "lage_temp_log.txt",
    "melkkoppelingen_log.txt",
}


class HerlaadHandler(FileSystemEventHandler):
    def __init__(self, herstart_fn):
        self._herstart       = herstart_fn
        self._wacht          = False
        self._laatste_mtime  = {}  # str(pad) → float mtime

    def on_modified(self, event):
        if event.is_directory:
            return
        pad = Path(event.src_path)

        # Negeer bestanden in uitgesloten mappen
        if any(deel in NEGEER_MAPPEN for deel in pad.parts):
            return

        # Negeer op extensie (snelste check — doe dit vóór stat)
        if pad.suffix in NEGEER_EXTS:
            return

        # Negeer als het niet in de WATCH_EXTS zit
        if pad.suffix not in WATCH_EXTS:
            return

        # Negeer specifieke bestandsnamen
        if pad.name in NEGEER_BESTANDEN:
            return

        # Controleer of het bestand écht gewijzigd is via mtime
        # (bescherming tegen valse triggers van OneDrive / Windows Defender)
        try:
            mtime = pad.stat().st_mtime
        except FileNotFoundError:
            return
        if self._laatste_mtime.get(str(pad)) == mtime:
            return  # inhoud niet veranderd, negeer
        self._laatste_mtime[str(pad)] = mtime

        if not self._wacht:
            self._wacht = True
            log.info(f"Wijziging gedetecteerd: {pad.name} — herstart over {RESTART_DELAY}s...")
            threading.Timer(RESTART_DELAY, self._doe_herstart).start()

    def _doe_herstart(self):
        self._wacht = False
        self._herstart()


def run():
    process    = [None]
    gestart_op = [0.0]  # timestamp van laatste start, voor crash-detectie

    def start_proces():
        if process[0] and process[0].poll() is None:
            log.info("Bezig proces stoppen...")
            process[0].terminate()
            try:
                process[0].wait(timeout=10)
            except subprocess.TimeoutExpired:
                process[0].kill()
        log.info("app.py starten...")
        process[0]    = subprocess.Popen(
            [sys.executable, str(WATCH_DIR / "app.py")],
            cwd=str(WATCH_DIR),
        )
        gestart_op[0] = time.time()

    start_proces()

    observer = Observer()
    observer.schedule(HerlaadHandler(start_proces), str(WATCH_DIR), recursive=True)
    observer.start()
    log.info(f"Bestandswatcher actief op {WATCH_DIR}")
    log.info("Druk op Ctrl+C om te stoppen.")

    try:
        while True:
            # Herstart als app.py onverwacht crasht,
            # maar wacht minstens 8 seconden na de laatste start
            # (voorkomt valse crash-detectie terwijl app.py nog opstart)
            if process[0] and process[0].poll() is not None:
                if time.time() - gestart_op[0] > 8:
                    log.warning("app.py is gestopt — herstart...")
                    start_proces()
            time.sleep(20)
    except KeyboardInterrupt:
        log.info("Stoppen...")
        observer.stop()
        if process[0] and process[0].poll() is None:
            process[0].terminate()

    observer.join()


if __name__ == "__main__":
    run()
