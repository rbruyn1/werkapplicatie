"""
onedrive_sync.py
=================
Spiegelt de *.json- en *.txt-databestanden uit de root van de applicatie
naar een OneDrive-map, zodat je dezelfde applicatie (incl. actuele data)
ook op een pc buiten het UZ Leuven-netwerk kan draaien.

Voorbeeld-opstelling:
  - Master PC + collega's draaien vanaf  Z:\\APPLICATIE WO\\werkorder-dashboard
  - De Master PC (scraper-master, zie scraper.lock) kopieert na elke
    wijziging van een *.json/*.txt-bestand een verse versie naar
    C:\\Users\\rbruyn1\\OneDrive - UZ Leuven\\werkorder-dashboard
  - In die OneDrive-map staat een volledige, aparte kopie van de applicatie
    (bv. via git). Die tweede kopie draait op een pc buiten het netwerk,
    kan niet aanmelden bij PeopleSoft, maar toont via de gesyncte data wel
    de actuele werkorders/logs (automatisch "Leesmodus", zie app.py).

Veiligheid:
  - Draait ENKEL op de scraper-master. Alle andere pc's (leesmodus) doen
    niets — die lezen toch al dezelfde Z:\\...-map.
  - config.json en secret.key worden NOOIT gekopieerd: dat zijn de
    (versleutelde) PeopleSoft-inloggegevens. Zet "sync_config": true
    in config.json → onedrive_sync als je dat toch bewust wil.
  - Enkel bestanden in de root van de app worden gesynct (geen logs/,
    uploads/, __pycache__/, .git/, templates/, ...).

Config (config.json):
{
  "onedrive_sync": {
    "enabled": true,
    "pad": "C:\\Users\\rbruyn1\\OneDrive - UZ Leuven\\werkorder-dashboard",
    "sync_config": false
  }
}
"""

import json
import logging
import shutil
import socket
import threading
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

log = logging.getLogger(__name__)

_APP_DIR  = Path(__file__).parent
_LOCK_PAD = _APP_DIR / "scraper.lock"
_HOSTNAME = socket.gethostname()

# Bestanden die NOOIT gesynct worden, ook al is de extensie .json/.txt
_NOOIT_SYNCEN = {"secret.key"}

_DEBOUNCE_SECONDEN = 1.5   # wacht even na een wijziging (atomische schrijf = 2 fs-events)


def _is_scraper_master() -> bool:
    """True als deze pc de scraper-master is — zelfde check als elders in de app."""
    try:
        with open(_LOCK_PAD, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("host", "").lower() == _HOSTNAME.lower()
    except Exception:
        return False


def _mag_gesynct_worden(pad: Path, sync_config: bool) -> bool:
    if pad.name in _NOOIT_SYNCEN:
        return False
    if pad.name == "config.json" and not sync_config:
        return False
    if pad.suffix not in (".json", ".txt"):
        return False
    if pad.parent != _APP_DIR:           # enkel root, geen submappen
        return False
    return True


def _kopieer(pad: Path, doel_map: Path):
    try:
        doel_map.mkdir(parents=True, exist_ok=True)
        doel = doel_map / pad.name
        tmp  = doel.with_name(doel.name + ".tmp")
        shutil.copy2(pad, tmp)
        tmp.replace(doel)                 # atomisch op de doelschijf
        log.debug(f"OneDrive-sync: {pad.name} -> {doel}")
    except Exception as e:
        log.error(f"OneDrive-sync: kopieren van {pad.name} mislukt: {e}")


class _SyncHandler(FileSystemEventHandler):
    def __init__(self, doel_map: Path, sync_config: bool):
        self._doel_map    = doel_map
        self._sync_config = sync_config
        self._timers      = {}      # str(pad) -> Timer
        self._lock        = threading.Lock()

    def _plan_kopie(self, pad: Path):
        if not _mag_gesynct_worden(pad, self._sync_config):
            return
        if not _is_scraper_master():
            return   # veiligheid: nooit vanaf een leesmodus-pc syncen

        with self._lock:
            bestaande = self._timers.get(str(pad))
            if bestaande:
                bestaande.cancel()
            t = threading.Timer(_DEBOUNCE_SECONDEN, self._doe_kopie, args=(pad,))
            t.daemon = True
            self._timers[str(pad)] = t
            t.start()

    def _doe_kopie(self, pad: Path):
        with self._lock:
            self._timers.pop(str(pad), None)
        if pad.exists():
            _kopieer(pad, self._doel_map)

    def on_modified(self, event):
        if not event.is_directory:
            self._plan_kopie(Path(event.src_path))

    def on_created(self, event):
        if not event.is_directory:
            self._plan_kopie(Path(event.src_path))


_observer     = None
_poll_thread  = None
_poll_stop    = None

# Fallback-interval voor de periodieke volledige rescan (zie _poll_loop).
# Nodig omdat watchdog/ReadDirectoryChangesW wijzigingen die door EEN ANDERE
# pc op de netwerkshare gebeuren, soms gewoon niet detecteert (geen event).
# De poll-loop vergelijkt gewoon de mtime van elk bestand en kopieert opnieuw
# als het bestand op de share nieuwer is dan wat al in de OneDrive-map staat —
# dit werkt dus onafhankelijk van of er ooit een filesystem-event gevuurd is.
_POLL_SECONDEN = 15


def _kopieer_indien_nieuwer(pad: Path, doel_map: Path):
    """Kopieert enkel als bron nieuwer is dan bestaande kopie (of kopie ontbreekt)."""
    try:
        doel = doel_map / pad.name
        if doel.exists() and doel.stat().st_mtime >= pad.stat().st_mtime:
            return
        _kopieer(pad, doel_map)
    except Exception as e:
        log.error(f"OneDrive-sync (poll): vergelijken/kopieren van {pad.name} mislukt: {e}")


def _poll_loop(doel_map: Path, sync_config: bool, stop_event: threading.Event):
    while not stop_event.wait(_POLL_SECONDEN):
        if not _is_scraper_master():
            continue
        try:
            for pad in _APP_DIR.glob("*"):
                if pad.is_file() and _mag_gesynct_worden(pad, sync_config):
                    _kopieer_indien_nieuwer(pad, doel_map)
        except Exception as e:
            log.error(f"OneDrive-sync (poll): rescan mislukt: {e}")


def start_onedrive_sync(cfg: dict):
    """
    Start de OneDrive-sync-watcher. Doet niets als 'enabled' niet aanstaat
    of 'pad' leeg is.

    LET OP: doet BEWUST geen eigen _is_scraper_master()-check meer als
    opstart-voorwaarde. app.py (start_background_scraper) heeft dat via
    is_scraper_master() al bevestigd vlak vóórdat deze functie wordt
    aangeroepen — een tweede, onafhankelijke herlezing van hetzelfde
    scraper.lock-bestand (op de netwerkshare) bleek in de praktijk soms
    een paar milliseconden later een ander resultaat te geven dan de
    eerste check (race met andere pc's die tegelijk herstarten via
    start.py se file-watcher), waardoor de sync onterecht niet startte
    en daarna nooit meer herkanst werd. _is_scraper_master() blijft wel
    gebruikt in de poll-loop en de event-handler, als doorlopende
    veiligheidscheck (bv. als deze pc later de master-rol verliest).
    """
    global _observer, _poll_thread, _poll_stop

    sync_cfg = cfg.get("onedrive_sync", {}) or {}
    if not sync_cfg.get("enabled"):
        return
    pad_str = (sync_cfg.get("pad") or "").strip()
    if not pad_str:
        log.warning("OneDrive-sync: 'enabled' staat aan maar 'pad' is leeg — sync overgeslagen")
        return
    if _observer is not None:
        return  # al actief

    doel_map    = Path(pad_str)
    sync_config = bool(sync_cfg.get("sync_config", False))

    # Initiële volledige kopie, zodat de doelmap meteen up-to-date is
    aantal = 0
    for pad in _APP_DIR.glob("*"):
        if pad.is_file() and _mag_gesynct_worden(pad, sync_config):
            _kopieer(pad, doel_map)
            aantal += 1
    log.info(f"OneDrive-sync: initiele kopie van {aantal} bestand(en) naar {doel_map}")

    handler   = _SyncHandler(doel_map, sync_config)
    _observer = Observer()
    _observer.schedule(handler, str(_APP_DIR), recursive=False)
    _observer.start()

    # Fallback-poller: vangt wijzigingen op die door een ANDERE pc op de
    # netwerkshare gebeurden en waarvoor watchdog geen event kreeg.
    _poll_stop   = threading.Event()
    _poll_thread = threading.Thread(
        target=_poll_loop, args=(doel_map, sync_config, _poll_stop), daemon=True
    )
    _poll_thread.start()

    log.info(f"OneDrive-sync actief: {_APP_DIR} -> {doel_map} (*.json, *.txt, "
             f"events + {_POLL_SECONDEN}s fallback-poll)")


def stop_onedrive_sync():
    global _observer, _poll_thread, _poll_stop
    if _poll_stop is not None:
        _poll_stop.set()
    if _poll_thread is not None:
        _poll_thread.join(timeout=5)
        _poll_thread = None
    if _observer is not None:
        try:
            _observer.stop()
            _observer.join(timeout=5)
        except Exception:
            pass
        _observer = None
