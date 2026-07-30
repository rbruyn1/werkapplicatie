"""
Prototype: generieke, JSON-gestuurde extractie van servicerapport-PDF's.

NIET gekoppeld aan de live app (wo_service_rapport.py) - dit is een losse
proof-of-concept om te tonen hoe een nieuw fabrikant toegevoegd zou kunnen
worden via enkel een JSON-bestand in vendor_templates/, zonder Python-code
te schrijven. Zie vendor_templates/README.md voor het sjabloonformaat en
de openstaande ontwerpvragen voor of/hoe dit de bestaande
_extraheer_<vendor>()-functies zou vervangen.

Gebruik:
    from template_engine import laad_templates, herken_en_extraheer
    templates = laad_templates("vendor_templates")
    info = herken_en_extraheer(tekst, templates)
"""
from __future__ import annotations
import json
import re
from pathlib import Path


# Naamregistratie voor post-processing-stappen die met platte regex niet
# uit te drukken zijn (eenheid-omzetting e.d.). Bewust een kleine, expliciete
# lijst - geen vrije 'eval', om nieuwe sjablonen veilig extern bewerkbaar te
# houden zonder codewijzigingen.
def _hhmm_naar_decimaal_str(hhmm: str) -> str:
    uren, minuten = hhmm.split(":")
    return f"{int(uren) + int(minuten) / 60:.2f}".replace(".", ",")


POSTPROCESSORS = {
    "hhmm_naar_decimaal": _hhmm_naar_decimaal_str,
    "strip": lambda s: s.strip(),
    "join_whitespace": lambda s: " ".join(s.split()),
}


def laad_templates(map_pad: str | Path) -> list[dict]:
    """Leest elk *.json-bestand in map_pad in als sjabloon."""
    templates = []
    for pad in sorted(Path(map_pad).glob("*.json")):
        with open(pad, "r", encoding="utf-8") as f:
            sjabloon = json.load(f)
            sjabloon["_bestand"] = pad.name
            templates.append(sjabloon)
    return templates


def _herken(tekst: str, sjabloon: dict) -> bool:
    """Alle strings in detectie.bevat_alle moeten voorkomen (AND-logica,
    zelfde principe als de huidige _detecteer_formaat())."""
    vereist = sjabloon.get("detectie", {}).get("bevat_alle", [])
    return all(s in tekst for s in vereist)


def _extraheer_veld(tekst: str, veld_def) -> str:
    """veld_def is ofwel één patroon-string, ofwel een lijst van
    kandidaat-patronen die na elkaar geprobeerd worden (eerste match wint) -
    handig omdat rapportlay-outs van eenzelfde fabrikant licht kunnen
    verschillen tussen apparaattypes/versies."""
    patronen = veld_def if isinstance(veld_def, list) else [veld_def]
    for patroon in patronen:
        vlag = re.DOTALL if "\\n" in patroon or patroon.startswith("(?s)") else 0
        m = re.search(patroon, tekst, vlag)
        if m:
            return m.group(1)
    return ""


def extraheer_via_template(tekst: str, sjabloon: dict) -> dict:
    info = {
        "sn": "", "product": "", "firma": sjabloon.get("firma", "Onbekend"),
        "werkorder_nr": "", "datum": "", "type_verzoek": "", "uren_arbeid": "",
        "omschrijving_kort": "", "activiteit_tekst": "",
        "formaat": sjabloon.get("naam", "onbekend"),
    }
    for veldnaam, veld_def in sjabloon.get("velden", {}).items():
        ruw = _extraheer_veld(tekst, veld_def.get("patroon"))
        for stap in veld_def.get("verwerking", ["strip"]):
            if ruw:
                ruw = POSTPROCESSORS[stap](ruw)
        info[veldnaam] = ruw

    if not info.get("oplossing"):
        info["oplossing"] = info.get("activiteit_tekst", "")

    extra_velden = sjabloon.get("bonus_velden", {})
    for veldnaam, veld_def in extra_velden.items():
        waarde = _extraheer_veld(tekst, veld_def.get("patroon"))
        if waarde:
            info[veldnaam] = waarde.strip()

    return info


def herken_en_extraheer(tekst: str, templates: list[dict]) -> dict | None:
    for sjabloon in templates:
        if _herken(tekst, sjabloon):
            return extraheer_via_template(tekst, sjabloon)
    return None
