"""
comserver.py
─────────────────────────────────────────────────────────────────
Reset van een W&T Com-Server (serieel <-> netwerk) via telnet, zoals
manueel via PuTTY/WuTility: hoofdmenu "2" (SETUP System) -> "7" (Reset)
-> "Y" (Close Connection and reset Com-Server?).

Gebruikt een kleine eigen telnet-client op een gewone socket, want de
standaard 'telnetlib' bestaat niet meer vanaf Python 3.13.

Het configuratiemenu van de W&T Com-Server Highspeed (58631/58034/...) zit op
TCP-poort 1111 (zie handboek: "telnet <ip> 1111"). Poort 23 is de seriële
telnet-server-poort en niet het menu.

Let op: enkel bedoeld voor de logging-pc (zie COMSERVER_RESET_PC in app.py).
"""

from __future__ import annotations

import socket
import threading
import time
from datetime import datetime
from pathlib import Path

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240

SETUP_POORT = 1111

_slot = threading.Lock()   # nooit twee resets tegelijk

LOG_PAD = Path(__file__).parent / "logs" / "comserver.log"


def schrijf_log(msg: str) -> None:
    """Schrijft een regel naar logs/comserver.log (zelfde formaat als de andere logs)."""
    try:
        LOG_PAD.parent.mkdir(exist_ok=True)
        with open(LOG_PAD, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


class _Telnet:
    """Minimale telnet-client: weigert alle optie-onderhandelingen."""

    def __init__(self, ip: str, poort: int, timeout: float):
        self.sock = socket.create_connection((ip, poort), timeout=timeout)
        self.sock.settimeout(0.5)
        self.buf = ""
        self.gesloten = False

    def _lees(self):
        try:
            data = self.sock.recv(4096)
        except socket.timeout:
            return
        except OSError:
            self.gesloten = True
            return
        if not data:
            self.gesloten = True
            return
        tekst = bytearray()
        i = 0
        while i < len(data):
            b = data[i]
            if b == IAC and i + 1 < len(data):
                cmd = data[i + 1]
                if cmd in (DO, DONT, WILL, WONT) and i + 2 < len(data):
                    optie = data[i + 2]
                    antwoord = WONT if cmd == DO else DONT if cmd == WILL else None
                    if antwoord is not None:
                        try:
                            self.sock.sendall(bytes([IAC, antwoord, optie]))
                        except OSError:
                            pass
                    i += 3
                    continue
                if cmd == SB:   # subnegotiation overslaan tot IAC SE
                    einde = data.find(bytes([IAC, SE]), i)
                    i = einde + 2 if einde != -1 else len(data)
                    continue
                i += 2
                continue
            tekst.append(b)
            i += 1
        self.buf += tekst.decode("ascii", errors="replace")

    def wacht_op(self, test, timeout: float) -> bool:
        """Wacht tot test(self.buf) waar is. False bij timeout of gesloten verbinding."""
        einde = time.time() + timeout
        while time.time() < einde:
            if test(self.buf):
                return True
            if self.gesloten:
                return test(self.buf)
            self._lees()
        return test(self.buf)

    def stuur(self, tekst: str):
        self.buf = ""
        self.sock.sendall((tekst + "\r\n").encode("ascii"))

    def sluit(self):
        try:
            self.sock.close()
        except OSError:
            pass


def reset_comserver(ip: str, wachtwoord: str = "", poort: int = SETUP_POORT,
                    stap_log=None, timeout: float = 10.0) -> dict:
    """Reset de Com-Server op 'ip'. Geeft {"ok": bool, "fout": str|None, "log": [..]}."""
    regels: list[str] = []

    def log(msg: str):
        regels.append(msg)
        schrijf_log(f"{ip}: {msg}")
        if stap_log:
            stap_log(msg)

    if not _slot.acquire(blocking=False):
        schrijf_log(f"{ip}: reset overgeslagen — er loopt al een Com-Server reset")
        return {"ok": False, "fout": "Er loopt al een Com-Server reset", "log": regels}

    tn = None
    try:
        log(f"verbinden (telnet {poort})...")
        try:
            tn = _Telnet(ip, poort, timeout)
        except OSError as e:
            return {"ok": False, "fout": f"Verbinden mislukt: {e}", "log": regels}

        menu = lambda b: "(q=quit)" in b

        # Eventueel telnet-wachtwoord
        tn.wacht_op(lambda b: menu(b) or "password" in b.lower(), timeout)
        if "password" in tn.buf.lower() and not menu(tn.buf):
            if not wachtwoord:
                return {"ok": False, "fout": "Com-Server vraagt een telnet-wachtwoord (niet ingesteld)", "log": regels}
            log("Wachtwoord opgevraagd -> invullen")
            tn.stuur(wachtwoord)
            tn.wacht_op(menu, timeout)
        if not menu(tn.buf):
            return {"ok": False, "fout": "Hoofdmenu niet gezien", "log": regels}
        log("Hoofdmenu gezien -> '2' (SETUP System)")

        tn.stuur("2")
        if not tn.wacht_op(lambda b: "SETUP System" in b and "(q=quit)" in b[b.find("SETUP System"):], timeout):
            return {"ok": False, "fout": "SETUP System-menu niet gezien", "log": regels}
        log("SETUP System-menu gezien -> '7' (Reset)")

        tn.stuur("7")
        if not tn.wacht_op(lambda b: "(Y)" in b, timeout):
            return {"ok": False, "fout": "Bevestigingsvraag voor reset niet gezien", "log": regels}
        log("Bevestigingsvraag gezien -> 'Y'")

        tn.stuur("Y")
        # De Com-Server sluit de verbinding zodra hij herstart.
        tn.wacht_op(lambda b: False, 3.0)
        log("reset verstuurd")
        return {"ok": True, "fout": None, "log": regels}
    except Exception as e:
        return {"ok": False, "fout": str(e), "log": regels}
    finally:
        if tn:
            tn.sluit()
        _slot.release()
