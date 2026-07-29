"""
Hulpfunctie om te bepalen welke OPRID in het PeopleSoft "Uitvoerder"-veld
moet worden ingevuld voor gebruiker-gedreven taken (Thuisdialyse,
RO staalnamen, Service rapporten).

Logica:
  - Windows-gebruikersnaam (getpass.getuser()) komt bij iedereen overeen
    met de eigen PeopleSoft OPRID (rbruyn1, jrydan0, x235944, mlaerm0).
  - Uitzondering: op de master-scraper PC draait dit onder het account
    "sa_water" → dan wordt altijd "rbruyn1" gebruikt.
  - Als de Windows-gebruiker niet voorkomt in werkuren_users (onbekend
    account), wordt terugvallen op de meegegeven fallback (PS_USER).

Let op: deze functie is uitsluitend bedoeld voor gebruiker-gedreven taken.
Backend-taken (Lage Temp, CCS reset, melkkoppelingen) blijven gewoon
PS_USER gebruiken en roepen deze functie niet aan.
"""

import getpass

SA_WATER_OVERRIDE = "rbruyn1"


def bepaal_uitvoerder(cfg: dict, fallback: str) -> str:
    """Geeft de OPRID terug die in het Uitvoerder-veld moet komen."""
    try:
        win_user = getpass.getuser()
    except Exception:
        return fallback

    if win_user.lower() == "sa_water":
        return SA_WATER_OVERRIDE

    geldige_oprids = {
        u.get("oprid", "")
        for u in cfg.get("werkuren_users", [])
        if u.get("oprid")
    }
    if win_user in geldige_oprids:
        return win_user

    return fallback
