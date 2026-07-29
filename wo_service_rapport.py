"""
wo_service_rapport.py
=====================
Module voor het verwerken van externe service-rapporten (PDF):
  1. PDF uploaden → serienummer automatisch extraheren
  2. PeopleSoft doorzoeken op serienummer → T-nummer ophalen
  3. Werkorder aanmaken op basis van gevonden T-nummer

Ondersteunde PDF-formaten (automatisch herkend):
  - Vantive/Baxter "Detail Servicerapport" (Werkorder # ...)
  - Nipro "Service Report" (Serial-No.: ...)
  - B.Braun "Service rapport" (Serienummer + Artikelomschrijving tabel)
  - Fluke/Ansur ESI-rapporten (Serial number: ...)
  - Fresenius Medical Care "Interventierapport"

Configuratie via config.json (gedeeld met wo_aanmaken.py):
    ps_base_url, ps_psc_url, username, password, werkuren_users

Aanroep vanuit app.py:
    from wo_service_rapport import extraheer_sn_uit_pdf, zoek_tnummer_op_sn, maak_wo_service_rapport
"""

import asyncio
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

import pdfplumber
from playwright.async_api import async_playwright

# ── Configuratie ─────────────────────────────────────────────────────────────

def _lees_config() -> dict:
    pad = Path(__file__).parent / "config.json"
    if pad.exists():
        with open(pad, encoding="utf-8") as f:
            return json.load(f)
    return {}

_cfg = _lees_config()

PS_BASE_URL = _cfg.get("ps_base_url",
    "https://peoplesoftlogistiek.uz.kuleuven.ac.be/psp/FS9PROD/")
PS_PSC_URL  = _cfg.get("ps_psc_url",
    "https://peoplesoftlogistiek.uz.kuleuven.ac.be/psc/FS9PROD/")
PS_USER     = _cfg.get("username", "")
PS_PASS     = _cfg.get("password", "")
PS_HEADLESS = False

def _lees_werkuren_users() -> list[dict]:
    users_cfg = _cfg.get("werkuren_users", [])
    return [{"oprid": u.get("oprid", ""), "naam": u.get("naam", "")}
            for u in users_cfg if u.get("oprid")]

PS_USERS = _lees_werkuren_users()

from uitvoerder_utils import bepaal_uitvoerder
UITVOERDER = bepaal_uitvoerder(_cfg, PS_USER)

LOGIN_URL  = PS_BASE_URL.rstrip("/") + "?cmd=login&languageCd=ENG"
WO_NIEUW_URL = (
    PS_PSC_URL.rstrip("/") + "/"
    + "EMPLOYEE/ERP/c/UZFM_MENU.UZFM_WERKORDER.GBL"
    "?BUSINESS_UNIT=POUZL&UZ_WO_ID=NEXT&PAGE=UZFM_WO_ALG"
)
SR_DATA_PATH = Path(__file__).parent / "service_rapporten.json"

# ── Data helpers ─────────────────────────────────────────────────────────────

def _zet_uren_om_naar_decimaal(uren_str: str) -> float:
    """
    Zet urenformat om naar decimaal.
    - "2u30" → 2.5
    - "2:30" → 2.5
    - "1.5" → 1.5
    - "" of invalid → 0.5 (default)
    """
    if not uren_str:
        return 0.5
    
    uren_str = uren_str.strip()
    try:
        # Probeer eerst direct decimaal: "1.5" → 1.5
        if "." in uren_str and "u" not in uren_str.lower() and ":" not in uren_str:
            return float(uren_str)
        
        # Format: "2u30" of "2:30"
        if "u" in uren_str.lower():
            parts = uren_str.lower().split("u")
        elif ":" in uren_str:
            parts = uren_str.split(":")
        else:
            # Alleen getal
            return float(uren_str)
        
        if len(parts) >= 2:
            uren = float(parts[0].strip())
            minuten = float(parts[1].strip())
            return uren + (minuten / 60)
        else:
            return float(uren_str)
    except (ValueError, IndexError):
        return 0.5


def _hhmm_naar_decimaal_str(uren_str: str) -> str:
    """
    Zet 'HH:MM' om naar decimaal getal met komma, bv. '02:30' -> '2,5'.
    Als de invoer al puur numeriek is (geen ':'), wordt die ongewijzigd
    teruggegeven (na trim). Bij lege/ongeldige invoer: lege string.
    """
    if not uren_str:
        return ""
    uren_str = uren_str.strip()
    if ":" not in uren_str:
        return uren_str
    try:
        uren, minuten = uren_str.split(":", 1)
        decimaal = float(uren) + float(minuten) / 60
        # Trim onnodige decimalen: 2.50 -> 2,5 ; 2.00 -> 2
        tekst = f"{decimaal:.2f}".rstrip("0").rstrip(".")
        return tekst.replace(".", ",")
    except (ValueError, ZeroDivisionError):
        return uren_str


def _uren_voor_veld(uren_str: str) -> str:
    """
    Zet de (evt. met komma) uren_arbeid-waarde uit het rapport om naar een
    string met een PUNT, geschikt om in het PeopleSoft-urenveld te typen
    (bv. '0,75' -> '0.75', '1' -> '1'). Een komma wordt daar foutief verwerkt.
    Als de uren niet uit het service rapport konden worden uitgelezen
    (leeg of ongeldig), wordt standaard '1' (1 uur) ingevuld.
    """
    if not uren_str:
        return "1"
    tekst = str(uren_str).strip().replace(",", ".")
    try:
        float(tekst)
    except ValueError:
        return "1"
    return tekst


def sr_lees() -> dict:
    if SR_DATA_PATH.exists():
        with open(SR_DATA_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"rapporten": [], "geschiedenis": []}


def sr_sla_op(data: dict):
    with open(SR_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def sr_voeg_toe(rapport: dict) -> dict:
    """Voeg een nieuw rapport toe en geef het terug met id."""
    data = sr_lees()
    rapport.setdefault("id", str(uuid.uuid4())[:8])
    rapport.setdefault("aangemaakt", datetime.now().strftime("%Y-%m-%d %H:%M"))
    rapport.setdefault("status", "nieuw")   # nieuw | bevestigd | wo_aangemaakt | fout
    rapport.setdefault("t_nummer", "")
    rapport.setdefault("t_omschrijving", "")
    rapport.setdefault("wo_id", "")
    rapport.setdefault("probleemmelding", "")
    rapport.setdefault("oplossing", "")
    rapport.setdefault("log", [])
    data["rapporten"].append(rapport)
    sr_sla_op(data)
    return rapport


def sr_update(rapport_id: str, **velden):
    data = sr_lees()
    for r in data["rapporten"]:
        if r["id"] == rapport_id:
            r.update(velden)
    sr_sla_op(data)


def sr_verwijder(rapport_id: str):
    data = sr_lees()
    data["rapporten"] = [r for r in data["rapporten"] if r["id"] != rapport_id]
    sr_sla_op(data)


# ── SN-extractie uit PDF ──────────────────────────────────────────────────────

def _pdf_tekst(pdf_pad: str | Path) -> str:
    """Haal alle tekst op uit een PDF via pdfplumber."""
    tekst = ""
    try:
        with pdfplumber.open(str(pdf_pad)) as pdf:
            for page in pdf.pages:
                tekst += (page.extract_text() or "") + "\n"
    except Exception as e:
        tekst = f"[PDF LEESFOUT: {e}]"
    return tekst


def _detecteer_formaat(tekst: str) -> str:
    """Detecteer het PDF-formaat op basis van sleutelwoorden."""
    if "Werkorder #" in tekst and "Detail Servicerapport" in tekst:
        return "vantive"
    if "Serial-No.:" in tekst and ("Nipro" in tekst or "DSURDIAL" in tekst or "QMS ID" in tekst):
        return "nipro"
    if "B.Braun" in tekst and "Service rapport" in tekst:
        return "bbraun"
    if "Fluke Biomedical Ansur" in tekst or "ESA612" in tekst:
        return "fluke_ansur"
    if "Fresenius Medical Care" in tekst and "INTERVENTIERAPPORT" in tekst:
        return "fresenius"
    if "TERUMO BCT" in tekst and "SERIENUMMER:" in tekst and "WERKORDERNUMMER:" in tekst:
        return "terumo"
    return "onbekend"


def _extraheer_vantive(tekst: str, _pdf_pad: str | Path = "") -> dict:
    """Vantive/Baxter 'Detail Servicerapport' — Serienummer : XXXXX"""
    sn = ""
    product = ""
    firma = "Vantive"
    werkorder_nr = ""
    datum = ""
    type_verzoek = ""
    uren_arbeid = ""

    # Serienummer
    m = re.search(r"Serienummer\s*:\s*([A-Z0-9]+)", tekst)
    if m:
        sn = m.group(1).strip()

    # Product omschrijving
    m = re.search(r"Product Omschrijving\s*:\s*(.+?)(?:\n|Software)", tekst)
    if m:
        product = m.group(1).strip()

    # Werkorder nr
    m = re.search(r"Werkorder #\s*(\d+)", tekst)
    if m:
        werkorder_nr = m.group(1).strip()

    # Datum
    m = re.search(r"(?:START ACTIVITEIT|Afsluitdatum)\s*:\s*(\d{2}/\w+/\d{4})", tekst)
    if m:
        datum = m.group(1).strip()

    # Type verzoek
    m = re.search(r"Type Verzoek\s*:\s*(.+?)(?:\n|aangemaakt)", tekst)
    if m:
        type_verzoek = m.group(1).strip()

    # Werktijd
    m = re.search(r"Labor\s+(\d+:\d+)", tekst)
    if m:
        uren_arbeid = _hhmm_naar_decimaal_str(m.group(1).strip())

    # Probleemomschrijving klant -> WO omschrijvingsveld
    # pdfplumber rendert kolommen naast elkaar: "Probleemomschrijving :\nklant Error 24\n"
    # Bij een meerregelige omschrijving lekken rechterkolom-velden (Garantie, Te factureren, ...)
    # mee tussen de regels van de linkerkolom, en de tekst loopt door tot ORDERNUMMER/START ACTIVITEIT.
    omschrijving_kort = ""
    m = re.search(
        r"Probleemomschrijving\s*:?\s*\nklant\s+(.+?)(?:\nORDERNUMMER|\nSTART ACTIVITEIT|\Z)",
        tekst, re.DOTALL
    )
    if m:
        ruwe_tekst = m.group(1)
        # Rechterkolom-velden die tussen de regels geplakt zijn eruit knippen
        ruwe_tekst = re.sub(
            r"\b(?:Garantie|Te\s+factureren|Contractnummer|aangemaakt|ORDERNUMMER|Type\s+Werkorder|Type\s+Verzoek)\s*:\s*\S*",
            "", ruwe_tekst
        )
        omschrijving_kort = " ".join(ruwe_tekst.split())

    # Opmerkingen uit interventietabel -> WO commentaarveld
    # Pagina 1 apart lezen zodat pagina 2 niet mee lekt.
    activiteit_tekst = ""
    try:
        import pdfplumber as _plb
        with _plb.open(str(_pdf_pad)) as _pdf:
            tekst_p1 = _pdf.pages[0].extract_text() or "" if _pdf.pages else tekst
    except Exception:
        tekst_p1 = tekst
    
    # Probeer eerst het oude format: "Opmerkingen Eigendom" headers
    m = re.search(
        r"Opmerkingen\s+Eigendom\s*\n(?:Repair|Preventive Maintenance|Correctief|PM)?\s*\n(.+?)(?:\nTest Aparatuur|\nWERKTIJD EN REISTIJD|\nHandtekening|\Z)",
        tekst_p1, re.DOTALL
    )
    if m:
        blok = m.group(1)
        eerste, *rest = blok.split("\n")
        eerste = re.sub(r'\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\s*$', '', eerste).strip()
        activiteit_tekst = " ".join(("\n".join([eerste] + rest)).split())
    
    # Fallback: nieuw format met tabel (Device Evaluation / Repair / enz)
    if not activiteit_tekst:
        m = re.search(
            r"(?:Device Evaluation|Repair)\s*\n(.+?)(?:\nTest Aparatuur|\nWERKTIJD EN REISTIJD|\nHandtekening|\Z)",
            tekst_p1, re.DOTALL
        )
        if m:
            blok = m.group(1).strip()
            # Eerste regel: verwijder Eigendom naam
            regels = blok.split("\n")
            if regels:
                regels[0] = re.sub(r'\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\s*$', '', regels[0])
            activiteit_tekst = " ".join(" ".join(regels).split())
    
    if not activiteit_tekst:
        m = re.search(r"Preventive Maintenance\s*\n(.+?)(?:\n(?:Erwin|Gebruikte|Test|WERKTIJD))", tekst, re.DOTALL)
        if m:
            activiteit_tekst = " ".join(m.group(1).split())

    return {
        "sn": sn, "product": product, "firma": firma,
        "werkorder_nr": werkorder_nr, "datum": datum,
        "type_verzoek": type_verzoek, "uren_arbeid": uren_arbeid,
        "omschrijving_kort": omschrijving_kort,
        "activiteit_tekst": activiteit_tekst,
        "oplossing": activiteit_tekst,
        "formaat": "vantive"
    }


def _extraheer_nipro(tekst: str) -> dict:
    """Nipro 'Service Report' — Serial-No.: XXXXX"""
    sn = ""
    product = ""
    firma = "Nipro"
    datum = ""
    type_verzoek = "Herstelling"
    uren_arbeid = ""

    m = re.search(r"Serial-No\.\s*:\s*([A-Z0-9]+)", tekst)
    if m:
        sn = m.group(1).strip()

    m = re.search(r"Device Type\s*:\s*([A-Z0-9\-]+)", tekst)
    if m:
        product = m.group(1).strip()

    m = re.search(r"Service-Date\s*:\s*([\d\.]+)", tekst)
    if m:
        datum = m.group(1).strip()

    m = re.search(r"Working time \(h\)\s*:\s*([\d:]+)", tekst)
    if m:
        uren_arbeid = _hhmm_naar_decimaal_str(m.group(1).strip())

    # Report Error -> WO omschrijvingsveld
    omschrijving_kort = ""
    m = re.search(r"Report Error:\s*(.+?)(?:\nError-Codes|$)", tekst, re.DOTALL)
    if m:
        omschrijving_kort = " ".join(m.group(1).split())

    # Activity -> WO commentaarveld
    activiteit_tekst = ""
    m = re.search(r"Activity\n(.+?)(?=Working time)", tekst, re.DOTALL)
    if m:
        activiteit_tekst = " ".join(m.group(1).split())

    return {
        "sn": sn, "product": product, "firma": firma,
        "werkorder_nr": "", "datum": datum,
        "type_verzoek": type_verzoek, "uren_arbeid": uren_arbeid,
        "omschrijving_kort": omschrijving_kort,
        "activiteit_tekst": activiteit_tekst,
        "oplossing": activiteit_tekst,
        "formaat": "nipro"
    }


def _extraheer_bbraun(tekst: str) -> dict:
    """B.Braun 'Service rapport' — Serienummer in tabel"""
    sn = ""
    product = ""
    firma = "B.Braun"
    datum = ""
    type_verzoek = "Preventief onderhoud"
    uren_arbeid = ""

    # Serienummer staat in de tabel: "Artikelomschrijving  Serienummer  Working hours"
    # Dan op de volgende regel: "HOT RINSE SMART 40   110373   21389"
    # Probeer eerst de label-gebaseerde aanpak
    m = re.search(r"(?:Artikelomschrijving\s+Serienummer\s+Working hours\s*\n)([^\n]+)", tekst)
    if m:
        delen = m.group(1).split()
        # Laatste 2 kolommen zijn serienummer en working hours (numeriek)
        # Serienummer is het eerste niet-numerieke / gemengde stuk aan het einde
        # Eenvoudige heuristiek: zoek het eerste all-numerieke deel dat niet 'working hours' is
        if len(delen) >= 2:
            # Serienummer is typisch 2de-van-laatste token
            sn = delen[-2]

    # Fallback: zoek "Serienummer\n<waarde>"
    if not sn:
        m = re.search(r"Serienummer\s*\n\s*([A-Z0-9]+)", tekst)
        if m:
            sn = m.group(1).strip()

    # Product
    m = re.search(r"Artikelomschrijving\s*\n([^\n]+)", tekst)
    if m:
        product = m.group(1).strip()
    if not product:
        # tabel-rij aanpak: neem alles voor het serienummer
        if sn:
            m = re.search(r"([A-Z][A-Z0-9 ]+?)\s+" + re.escape(sn), tekst)
            if m:
                product = m.group(1).strip()

    # Datum
    m = re.search(r"Datum\s*:\s*([\d/]+)", tekst)
    if m:
        datum = m.group(1).strip()

    # Omschrijving activiteit
    m = re.search(r"Omschrijving activiteit\s*\n(.+?)(?:\n|Omschrijving)", tekst)
    if m:
        type_verzoek = m.group(1).strip()

    # Omschrijving activiteit = ook de omschrijving_kort (bvb "6 Month Maintenance")
    omschrijving_kort = type_verzoek

    # Omschrijving werkzaamheden -> activiteit tekst
    activiteit_tekst = ""
    m = re.search(r"Omschrijving werkzaamheden\s*\n([^\n]+)", tekst)
    if m:
        activiteit_tekst = m.group(1).strip()

    return {
        "sn": sn, "product": product, "firma": firma,
        "werkorder_nr": "", "datum": datum,
        "type_verzoek": type_verzoek, "uren_arbeid": uren_arbeid,
        "omschrijving_kort": omschrijving_kort,
        "activiteit_tekst": activiteit_tekst,
        "oplossing": activiteit_tekst,
        "formaat": "bbraun"
    }


def _extraheer_fluke(tekst: str) -> dict:
    """Fluke/Ansur ESI-rapport — Serial number XXXXX"""
    sn = ""
    product = ""
    firma = "Baxter/Vantive"
    datum = ""
    type_verzoek = "Elektrische veiligheidsinspectie"

    m = re.search(r"Serial number\s*[:\s]+([A-Z0-9]+)", tekst)
    if m:
        sn = m.group(1).strip()

    m = re.search(r"Model\s+([^\n]+)", tekst)
    if m:
        product = m.group(1).strip()

    m = re.search(r"Date:\s*([\d/]+)", tekst)
    if m:
        datum = m.group(1).strip()

    return {
        "sn": sn, "product": product, "firma": firma,
        "werkorder_nr": "", "datum": datum,
        "type_verzoek": type_verzoek, "uren_arbeid": "",
        "oplossing": "",
        "formaat": "fluke_ansur"
    }


def _extraheer_fresenius(tekst: str) -> dict:
    """Fresenius Medical Care 'Interventierapport'"""
    sn = ""
    product = ""
    firma = "Fresenius"
    datum = ""
    type_verzoek = "Herstelling"
    uren_arbeid = ""

    m = re.search(r"Serienummer:\s*([A-Z0-9]+)", tekst)
    if m:
        sn = m.group(1).strip()

    m = re.search(r"Omschrijving:\s*(.+?)(?:\n|Inventaris)", tekst)
    if m:
        product = m.group(1).strip()

    m = re.search(r"Datum:\s*(\d{2}/\d{2}/\d{4})", tekst)
    if m:
        datum = m.group(1).strip()

    m = re.search(r"Werkuren:\s*([\d:]+)", tekst)
    if m:
        uren_arbeid = _hhmm_naar_decimaal_str(m.group(1).strip())

    # Reden oproep -> WO omschrijvingsveld
    omschrijving_kort = ""
    m = re.search(r"Reden oproep:\s*(.+?)(?:\nVastgesteld probleem|$)", tekst, re.DOTALL)
    if m:
        omschrijving_kort = " ".join(m.group(1).split())

    # Uitgevoerde werken -> WO commentaarveld
    activiteit_tekst = ""
    m = re.search(r"Uitgevoerde werken:\s*(.+?)(?:\nMeetinstrumenten|\nOpmerking|$)", tekst, re.DOTALL)
    if m:
        activiteit_tekst = " ".join(m.group(1).split())

    return {
        "sn": sn, "product": product, "firma": firma,
        "werkorder_nr": "", "datum": datum,
        "type_verzoek": type_verzoek, "uren_arbeid": uren_arbeid,
        "omschrijving_kort": omschrijving_kort,
        "activiteit_tekst": activiteit_tekst,
        "oplossing": activiteit_tekst,
        "formaat": "fresenius"
    }


def _extraheer_terumo(tekst: str) -> dict:
    """Terumo BCT 'Geschatte prijs' (offerte/SR) — SERIENUMMER: XXXXX"""
    sn = ""
    product = ""
    firma = "Terumo"
    werkorder_nr = ""
    datum = ""
    type_verzoek = ""
    uren_arbeid = ""

    m = re.search(r"SERIENUMMER:\s*([A-Z0-9]+)", tekst)
    if m:
        sn = m.group(1).strip()

    # Model XXXXX: PRODUCTNAAM
    m = re.search(r"Model\s+\d+:\s*(.+?)(?:\n|Melding)", tekst)
    if m:
        product = m.group(1).strip()

    m = re.search(r"WERKORDERNUMMER:\s*(\S+)", tekst)
    if m:
        werkorder_nr = m.group(1).strip()

    m = re.search(r"DATUM:\s*(\d{2}/\d{2}/\d{4})", tekst)
    if m:
        datum = m.group(1).strip()

    # Melding -> type verzoek (bv. "Post Warranty Repair")
    m = re.search(r"Melding\s+(.+?)(?:\nPROBLEEMBESCHRIJVING|\n)", tekst)
    if m:
        type_verzoek = m.group(1).strip()

    # Tijd in uren uit ARBEIDSUREN-regel (bv. "OPTIA FIELD SERVICE\n1 NA € 207,00 € 207,00 1\nLABOUR")
    m = re.search(r"\n\s*\d+\s+\S+\s+€\s*[\d.,]+\s+€\s*[\d.,]+\s+(\d+(?:[.,]\d+)?)\s*\n\s*LABOUR", tekst)
    if m:
        uren_arbeid = m.group(1).strip()

    # Probleembeschrijving -> WO omschrijvingsveld
    omschrijving_kort = ""
    m = re.search(r"PROBLEEMBESCHRIJVING\s*\n(.+?)(?:\nBENODIGDE ONDERDELEN|\n[A-Z ]{5,}\n|\Z)", tekst, re.DOTALL)
    if m:
        omschrijving_kort = " ".join(m.group(1).split())

    return {
        "sn": sn, "product": product, "firma": firma,
        "werkorder_nr": werkorder_nr, "datum": datum,
        "type_verzoek": type_verzoek, "uren_arbeid": uren_arbeid,
        "omschrijving_kort": omschrijving_kort,
        "activiteit_tekst": omschrijving_kort,
        "oplossing": omschrijving_kort,
        "formaat": "terumo"
    }


def extraheer_sn_uit_pdf(pdf_pad: str | Path) -> dict:
    """
    Hoofd-functie: extraheer serienummer en metadata uit een service-rapport PDF.
    Geeft dict terug met: sn, product, firma, datum, type_verzoek, uren_arbeid, formaat
    """
    tekst = _pdf_tekst(pdf_pad)
    formaat = _detecteer_formaat(tekst)

    if formaat == "vantive":
        info = _extraheer_vantive(tekst, pdf_pad)
    elif formaat == "nipro":
        info = _extraheer_nipro(tekst)
    elif formaat == "bbraun":
        info = _extraheer_bbraun(tekst)
    elif formaat == "fluke_ansur":
        info = _extraheer_fluke(tekst)
    elif formaat == "fresenius":
        info = _extraheer_fresenius(tekst)
    elif formaat == "terumo":
        info = _extraheer_terumo(tekst)
    else:
        # Generieke fallback: probeer alle bekende patronen
        info = {"sn": "", "product": "", "firma": "Onbekend", "formaat": "onbekend",
                "datum": "", "type_verzoek": "", "uren_arbeid": "", "werkorder_nr": "",
                "omschrijving_kort": "", "activiteit_tekst": ""}
        for patroon in [
            r"Serienummer\s*:\s*([A-Z0-9]+)",
            r"Serial[-\s]?No\.?\s*:\s*([A-Z0-9]+)",
            r"Serial number\s*[:\s]+([A-Z0-9]+)",
            r"S/N\s*:\s*([A-Z0-9]+)",
        ]:
            m = re.search(patroon, tekst, re.IGNORECASE)
            if m:
                info["sn"] = m.group(1).strip()
                break

    info["bestandsnaam"] = Path(pdf_pad).name
    info["pdf_tekst_preview"] = tekst[:500]
    return info


# ── PeopleSoft: serienummer → T-nummer via WO-formulier verrekijker popup ─────

async def zoek_tnummer_via_ps(serienummer: str, stap_log=None) -> dict:
    """
    Zoek het T-nummer op via de 'Look up Object' verrekijker in het WO-formulier.

    Exacte flow:
      1. Login + navigeer naar nieuw WO
      2. PTS toggle (vereenvoudigd scherm activeren)
      3. Klik op de verrekijker naast het Object-veld
         → popup opent in een nieuw browser-venster (win1)
      4. In de popup: vul serienummer in '#UZFM_OBJ_ACTIEF_UZ_OBJ_SERIENR'
      5. Klik 'Look Up' (#ICSearch)
      6. Lees eerste zoekresultaat '#SEARCH_RESULT1' → T-nummer
      7. Klik op het resultaat → popup sluit, T-nummer staat in WO-formulier
      8. Lees bevestigd T-nummer + omschrijving uit het WO-formulier
    """
    log_lijnen = []

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        regel = f"[{ts}] SN-ZOEK: {msg}"
        log_lijnen.append(regel)
        print(regel)
        if stap_log:
            stap_log(msg)

    resultaat = {"t_nummer": "", "omschrijving": "", "ok": False, "log": log_lijnen}

    if not serienummer:
        log("❌ Geen serienummer opgegeven")
        return resultaat

    log(f"T-nummer zoeken voor SN '{serienummer}' via WO-formulier verrekijker...")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=PS_HEADLESS)
            context = await browser.new_context()
            page = await context.new_page()

            # ── 1. Login — gebruik dezelfde login() functie als wo_aanmaken ──
            log(f"Stap 1: Inloggen op PeopleSoft...")
            from wo_aanmaken import login
            ok = await login(page, log)
            if not ok:
                log("❌ Login mislukt")
                await browser.close()
                resultaat["fout"] = "Login mislukt"
                return resultaat
            log("Stap 1: ✓ Ingelogd")
            await asyncio.sleep(2)

            # ── 2. Navigeer naar nieuw WO ──────────────────────────────────
            log(f"Stap 2: Navigeren naar nieuw WO-formulier: {WO_NIEUW_URL}")
            await page.goto(WO_NIEUW_URL, wait_until="domcontentloaded", timeout=30_000)
            log(f"Stap 2: Pagina geladen — {page.url}")
            if "//" in page.url.split("FS9PROD")[1] if "FS9PROD" in page.url else False:
                log("⚠ Dubbele slash gedetecteerd in URL — PeopleSoft sessie nog niet klaar, opnieuw proberen...")
                await asyncio.sleep(3)
                await page.goto(WO_NIEUW_URL, wait_until="domcontentloaded", timeout=30_000)
                log(f"Stap 2: Herpoging — {page.url}")

            # ── 3. PTS toggle — vereenvoudigd scherm activeren ─────────────
            log("Stap 3: PTS toggle activeren...")
            try:
                await page.wait_for_selector("#PTS_CFG_CL_WRK_PTS_PAGE_TOGGLE",
                                             state="visible", timeout=15_000)
                await page.click("#PTS_CFG_CL_WRK_PTS_PAGE_TOGGLE")
                await asyncio.sleep(3)
                await page.click("#PTS_CFG_CL_WRK_PTS_ADD_BTN")
                await asyncio.sleep(1)
                log("Stap 3: ✓ PTS toggle klaar")
            except Exception as e:
                log(f"Stap 3: PTS toggle niet gevonden ({e}) — doorgaan zonder")

            # ── 4. Wacht op verrekijker en klik erop ──────────────────────
            # De verrekijker heeft id: UZFM_WO_UZ_OBJ_ID$12$$prompt$img
            verrekijker = '[id="UZFM_WO_UZ_OBJ_ID$12$$prompt$img"]'
            log(f"Stap 4: Wachten op verrekijker '{verrekijker}'...")
            await page.wait_for_selector(verrekijker, state="visible", timeout=20_000)

            log("Stap 4: Klikken op verrekijker...")
            await page.click(verrekijker)
            await asyncio.sleep(2)

            # Modal iframe heet 'ptModFrame_0' in deze PeopleSoft omgeving
            modal = next((f for f in page.frames if f.name == "ptModFrame_0"), None)
            if not modal:
                log("❌ Modal iframe 'ptModFrame_0' niet gevonden")
                await browser.close()
                resultaat["fout"] = "Modal iframe niet gevonden"
                return resultaat
            log(f"Stap 4: ✓ Modal iframe gevonden")

            # ── 5. Serienummer invullen in modal iframe ────────────────────
            await asyncio.sleep(1)

            # SN-veld: bevestigd id='UZFM_OBJ_ACTIEF_UZ_OBJ_SERIENR'
            sn_veld = "#UZFM_OBJ_ACTIEF_UZ_OBJ_SERIENR"
            await modal.wait_for_selector(sn_veld, state="visible", timeout=10_000)
            await modal.fill(sn_veld, serienummer)
            log(f"Stap 5: ✓ SN '{serienummer}' ingevuld")

            # ── 6. Look Up klikken in modal ────────────────────────────────
            log("Stap 6: Klikken op 'Look Up' knop '[id=\"#ICSearch\"]' in modal...")
            await modal.click('[id="#ICSearch"]')
            await asyncio.sleep(3)
            log(f"Stap 6: ✓ Zoekresultaten geladen")

            # ── 7. Eerste resultaat lezen en aanklikken ────────────────────
            # Resultaat-link: <a id="SEARCH_RESULT1" ...>T64952</a>
            resultaat_sel = "#SEARCH_RESULT1"
            log(f"Stap 7: Zoeken naar eerste resultaat '{resultaat_sel}' in modal...")
            try:
                await modal.wait_for_selector(resultaat_sel, state="visible", timeout=10_000)
                t_nr_tekst = (await modal.locator(resultaat_sel).inner_text()).strip()
                log(f"Stap 7: ✓ Eerste resultaat = '{t_nr_tekst}'")
            except Exception as e:
                log(f"Stap 7: ⚠ Geen resultaat gevonden voor SN '{serienummer}': {e}")
                try:
                    body = await modal.locator("body").inner_text()
                    log(f"Stap 7: Modal inhoud (200 tekens): {body[:200]}")
                except Exception:
                    pass
                await browser.close()
                resultaat["fout"] = f"Geen toestel gevonden voor serienummer '{serienummer}'"
                return resultaat

            # Klik op resultaat → modal sluit, T-nummer wordt ingevuld in WO-formulier
            log(f"Stap 7: Klikken op '{t_nr_tekst}'...")
            await modal.click(resultaat_sel)
            log("Stap 7: ✓ Resultaat aangeklikt — wachten op modal sluiting...")

            # Wacht tot de modal verdwijnt
            try:
                await page.wait_for_selector(
                    "#ptModTable_2", state="hidden", timeout=10_000
                )
                log("Stap 7: ✓ Modal gesloten")
            except Exception:
                log("Stap 7: Modal-sluiting timeout — doorgaan")
            await asyncio.sleep(2)

            # ── 8. T-nummer + omschrijving bevestigen uit WO-formulier ─────
            t_nr_field = '[id="UZFM_WO_UZ_OBJ_ID$12$"]'
            omschr_div = "#win0divUZFM_OBJECT_UZ_OMSCHR150"

            log("Stap 8: T-nummer lezen uit WO-formulier...")
            t_nr_bevestigd = ""
            try:
                t_nr_bevestigd = await page.locator(t_nr_field).input_value(timeout=5_000)
                log(f"Stap 8: ✓ T-nummer in WO-veld = '{t_nr_bevestigd}'")
            except Exception as e:
                t_nr_bevestigd = t_nr_tekst  # fallback: waarde uit modal-link
                log(f"Stap 8: WO-veld niet leesbaar ({e}) — gebruik modal-waarde '{t_nr_bevestigd}'")

            omschrijving = ""
            try:
                omschrijving = (await page.locator(omschr_div).inner_text(timeout=5_000)).strip()
                log(f"Stap 8: ✓ Omschrijving = '{omschrijving[:80]}'")
            except Exception as e:
                log(f"Stap 8: Omschrijving niet leesbaar ({e})")

            await browser.close()

            if t_nr_bevestigd:
                resultaat["t_nummer"]     = t_nr_bevestigd
                resultaat["omschrijving"] = omschrijving
                resultaat["ok"]           = True
                log(f"✓ T-nummer gevonden: '{t_nr_bevestigd}' — '{omschrijving[:60]}'")
            else:
                log("⚠ T-nummer leeg na alle stappen")
                resultaat["fout"] = "T-nummer kon niet worden bevestigd"

    except Exception as e:
        log(f"❌ Onverwachte fout: {e}")
        resultaat["fout"] = str(e)

    return resultaat


# ── WO aanmaken voor service-rapport ─────────────────────────────────────────

async def maak_wo_service_rapport(rapport_id: str, stap_log=None, zet_uitgev: bool = True) -> dict:
    """
    Maak een werkorder aan voor een service-rapport.
    Het rapport moet al een t_nummer hebben (na zoek_tnummer_via_ps).

    zet_uitgev: als True (standaard) wordt de status na afloop op 'UITGEV' gezet.
    Als False blijft de WO op 'INUITV' staan (status wordt niet gewijzigd).
    """
    data = sr_lees()
    rapport = next((r for r in data["rapporten"] if r["id"] == rapport_id), None)

    log_lijnen = []

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        regel = f"[{ts}] WO-SR: {msg}"
        log_lijnen.append(regel)
        print(regel)
        if stap_log:
            stap_log(msg)

    resultaat = {"ok": False, "wo_id": None, "log": log_lijnen}

    if not rapport:
        log(f"❌ Rapport '{rapport_id}' niet gevonden")
        return resultaat

    t_nummer = rapport.get("t_nummer", "")
    sn = rapport.get("sn", "")
    product = rapport.get("product", "")
    firma = rapport.get("firma", "")
    type_verzoek = rapport.get("type_verzoek", "Herstelling")
    datum_str = rapport.get("datum", "")
    uren = rapport.get("uren_arbeid", "")
    uren_veld = _uren_voor_veld(uren)

    if not t_nummer:
        log("❌ Geen T-nummer — zoek eerst het T-nummer op via PeopleSoft")
        resultaat["fout"] = "Geen T-nummer beschikbaar"
        return resultaat

    # WO-omschrijving: probleemmelding uit het SR
    # Fallback: type interventie + firma + SN
    omschrijving_kort = (rapport.get("probleemmelding") or rapport.get("omschrijving_kort", "")).strip()
    if omschrijving_kort:
        omschrijving = omschrijving_kort[:254]
    else:
        omschrijving = f"{type_verzoek} - {firma} - SN {sn}" if type_verzoek else f"Extern SR - {firma} - SN {sn}"
        omschrijving = omschrijving[:254]

    # Commentaar: oplossing uit het SR (volledige tekst — via L Omschr indien >60 tekens)
    # Fallback: beknopte SR-samenvatting
    commentaar = (rapport.get("oplossing") or rapport.get("activiteit_tekst", "")).strip()
    if not commentaar:
        commentaar = f"Extern SR — {firma} | SN {sn} | {type_verzoek}"

    pdf_pad = rapport.get("pdf_pad", "")

    log(f"WO aanmaken: T-nummer={t_nummer}")
    log(f"  → Omschrijving uit PDF: '{omschrijving[:60] if omschrijving else 'LEEG'}'")
    log(f"  → Commentaar uit PDF: '{commentaar[:60]}'")


    try:
        # Importeer login/helper functies uit wo_aanmaken
        from wo_aanmaken import (login, wacht_op_element, zoek_win1_frame,
                                  vul_werkuren_lijn)

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

            # ── 2. Navigeer naar nieuw WO ──────────────────────────────────
            log(f"Stap 2: Navigeren naar nieuw WO: {WO_NIEUW_URL}")
            await page.goto(WO_NIEUW_URL, wait_until="domcontentloaded", timeout=30_000)
            log(f"Stap 2: Pagina geladen — URL: {page.url}")
            if "//" in page.url.split("FS9PROD")[1] if "FS9PROD" in page.url else False:
                log("⚠ Dubbele slash gedetecteerd — opnieuw proberen...")
                await asyncio.sleep(3)
                await page.goto(WO_NIEUW_URL, wait_until="domcontentloaded", timeout=30_000)
                log(f"Stap 2: Herpoging — {page.url}")

            # ── 3. PTS toggle ──────────────────────────────────────────────
            log("Stap 3: PTS toggle...")
            await wacht_op_element(page, "#PTS_CFG_CL_WRK_PTS_PAGE_TOGGLE", log=log)
            await page.click("#PTS_CFG_CL_WRK_PTS_PAGE_TOGGLE")
            await asyncio.sleep(3)
            await page.click("#PTS_CFG_CL_WRK_PTS_ADD_BTN")
            await asyncio.sleep(1)
            log("Stap 3: ✓ PTS toggle klaar")

            # ── 4. WO-type = AO ────────────────────────────────────────────
            log("Stap 4: WO-type 'AO' instellen...")
            try:
                await page.wait_for_selector("#UZFM_WO_UZ_WO_TYPE", state="visible", timeout=10_000)
                await page.select_option("#UZFM_WO_UZ_WO_TYPE", value="AO")
                await asyncio.sleep(0.5)
                log("Stap 4: ✓ WO-type 'AO' geselecteerd")
            except Exception as e:
                log(f"Stap 4: ⚠ select_option mislukt ({e}) — fallback via toetsenbord")
                await page.click("#UZFM_WO_UZ_WO_TYPE")
                await page.keyboard.press("a")
                await asyncio.sleep(0.3)
                log("Stap 4: ✓ WO-type fallback klaar")

            # ── 4b. WO-bron = MAN ─────────────────────────────────────────
            log("Stap 4b: WO-bron 'MAN' instellen...")
            try:
                await page.wait_for_selector("#UZFM_WO_UZ_WO_BRON", state="visible", timeout=10_000)
                await page.select_option("#UZFM_WO_UZ_WO_BRON", value="MAN")
                await asyncio.sleep(0.5)
                log("Stap 4b: ✓ WO-bron 'MAN' geselecteerd")
            except Exception as e:
                log(f"Stap 4b: ⚠ WO-bron instellen mislukt: {e}")

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
                        log(f"Stap 5: ✓ PeopleSoft omschrijving = '{tekst.strip()[:60]}'")
                        break
                    log(f"Stap 5: Omschrijving leeg (poging {poging+1}/10)...")
                except Exception as e:
                    log(f"Stap 5: Exceptie (poging {poging+1}/10): {e}")

            # ── 6. Omschrijving invullen — NA T-nummer lookup (anders reset PS het veld) ──
            log("Stap 6: Omschrijving invullen...")
            try:
                await wacht_op_element(page, "#UZFM_WO_UZ_OMSCHR254", log=log)
                await page.fill("#UZFM_WO_UZ_OMSCHR254", omschrijving)
                log(f"Stap 6: ✓ Omschrijving '{omschrijving[:60]}'")
            except Exception as e:
                log(f"Stap 6: ⚠ Omschrijving mislukt: {e}")

            # ── 7. Uitvoerder ──────────────────────────────────────────────
            log(f"Stap 7: Uitvoerder '{UITVOERDER}' invullen...")
            await page.click(uitvoerder_field)
            await asyncio.sleep(0.2)
            await page.keyboard.press("Control+a")
            await page.type(uitvoerder_field, UITVOERDER, delay=50)
            await page.keyboard.press("Tab")
            await asyncio.sleep(2)
            log("Stap 7: ✓ Uitvoerder klaar")

            # ── 8. Eerste opslaan ──────────────────────────────────────────
            wo_id = None
            log("Stap 8: Eerste opslaan...")
            await page.click('[id="#ICSave"]')
            await asyncio.sleep(2)

            for poging in range(10):
                await asyncio.sleep(3)
                try:
                    status = await page.locator("#UZFM_WO_UZ_WO_STATUS").inner_text(timeout=5_000)
                    log(f"Stap 8: Status = '{status.strip()}' (poging {poging+1})")
                    if status.strip() in ("INUITV", "NIEUW", "OPEN"):
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

            # URL-fallback voor WO-ID
            if not wo_id:
                m = re.search(r"UZ_WO_ID=(\d+)", page.url)
                if m:
                    wo_id = m.group(1)

            log(f"Stap 8: ✓ WO-ID = '{wo_id}'")

            # ── 9. Rapportage tab: commentaar + oplossing ─────────────────
            log("Stap 9: Rapportage-tab openen...")
            tc = await zoek_win1_frame(page, log=log)
            await tc.click("#ICTAB_2")
            await tc.wait_for_selector("#UZFM_WO_WRK_UZ_WO_COMMENT",
                                       state="visible", timeout=20_000)
            log("Stap 9: ✓ Rapportage-tab geladen")

            log(f"Stap 10: Commentaar invullen: '{commentaar[:80]}{'...' if len(commentaar)>80 else ''}'")
            if len(commentaar) <= 60:
                await tc.fill("#UZFM_WO_WRK_UZ_WO_COMMENT", commentaar)
                log("Stap 10: ✓ Commentaar direct ingevuld (≤60 tekens)")
            else:
                await tc.fill("#UZFM_WO_WRK_UZ_WO_COMMENT", commentaar[:60])
                await asyncio.sleep(0.5)
                log("Stap 10: Tekst >60 tekens — L Omschr popup openen...")
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
                    await lomschr_frame.fill("#UZFM_ALG_WORK_UZ_LDESCRPAGE_W", commentaar)
                    await asyncio.sleep(0.5)
                    await lomschr_frame.click('[id="#ICSave"]')
                    await asyncio.sleep(2)
                    log("Stap 10: ✓ Volledige tekst via L Omschr ingevuld en popup gesloten")

            log("Stap 11: Oplossing aanvinken...")
            await tc.click("#UZFM_WO_WRK_UZ_WO_OPLOSSING")
            await asyncio.sleep(1)
            log("Stap 11: ✓ Oplossing aangevinkt")

            # ── 10. Werkuren ───────────────────────────────────────────────
            if UITVOERDER == "rbruyn1":
                actieve_users = [u for u in PS_USERS if u.get("oprid") == "rbruyn1"]
                log(f"Stap 12: Indiener is rbruyn1 → enkel eigen uren invullen ({len(actieve_users)} lijn(en))")
            else:
                actieve_users = PS_USERS
            log(f"Stap 12: {len(actieve_users)} werkuren-gebruiker(s) invullen (uren='{uren_veld}', uit rapport: '{uren}')...")
            for idx, user in enumerate(actieve_users):
                if idx == 0:
                    await vul_werkuren_lijn(tc, 0, user, log=log, uren=uren_veld)
                else:
                    nieuwe_lijn_btn = f'[id="UZFM_WO_WERKUUR$new${idx-1}$$0"]'
                    await tc.click(nieuwe_lijn_btn)
                    await asyncio.sleep(2)
                    uren_field = f'[id="UZFM_WO_WERKUUR_UZ_WO_UREN${idx}"]'
                    await tc.wait_for_selector(uren_field, state="visible", timeout=15_000)
                    await vul_werkuren_lijn(tc, idx, user, log=log, uren=uren_veld)
            log("Stap 12: ✓ Werkuren klaar")
            log("Stap 12: ✓ Werkuren klaar")

            # ── 13. Definitief opslaan na werkuren ────────────────────────
            log("Stap 13: Definitief opslaan...")
            await page.click('[id="#ICSave"]')
            await asyncio.sleep(2)

            eindstatus = ""
            for poging in range(15):
                await asyncio.sleep(2)
                try:
                    eindstatus = (await page.locator("#UZFM_WO_UZ_WO_STATUS")
                                  .inner_text(timeout=5_000)).strip()
                    log(f"Stap 13: Status = '{eindstatus}' (poging {poging+1})")
                    if eindstatus == "INUITV":
                        break
                except Exception as e:
                    log(f"Stap 13: Status niet leesbaar: {e}")


            # ── 14. Bijlagen-tab openen ────────────────────────────────────
            pdf_upload_succesvol = False  # Track of PDF upload went well
            if pdf_pad and Path(pdf_pad).exists():
                log("Stap 14: Bijlagen-tab openen...")
                tc = await zoek_win1_frame(page, log=log)
                await tc.click("#ICTAB_4")
                await asyncio.sleep(2)
                log("Stap 14: ✓ Bijlagen-tab geladen")

                # ── 15. Klik op Add-knop (+ icoontje) ─────────────────────
                log("Stap 15: Add-knop klikken...")
                await tc.wait_for_selector("#UZ_ATTACH_BTN\\$0", state="visible", timeout=10_000)
                await tc.click("#UZ_ATTACH_BTN\\$0")
                await asyncio.sleep(3)
                log("Stap 15: ✓ Add geklikt")

                # ── 16. Load Attachment knop klikken → opent ptModFrame_0 popup ──
                log("Stap 16: Load Attachment klikken...")
                await tc.wait_for_selector("#UZ_ATTACH_WRK_UZ_ATTACH_ADD", state="visible", timeout=10_000)
                await tc.click("#UZ_ATTACH_WRK_UZ_ATTACH_ADD")
                await asyncio.sleep(2)
                log("Stap 16: ✓ Load Attachment geklikt")

                # ── 17. Bestand kiezen via iframe ptModFrame_0 ─────────────
                log("Stap 17: File Attachment popup zoeken (ptModFrame_0)...")
                upload_modal = next((f for f in page.frames if f.name == "ptModFrame_0"), None)
                if not upload_modal:
                    log("❌ Upload modal iframe niet gevonden")
                    await browser.close()
                    resultaat["fout"] = "Upload modal niet gevonden"
                    return resultaat
                log("Stap 17: ✓ Upload modal gevonden")

                # Zoek de file input in de popup (de "Bestand kiezen" knop)
                await asyncio.sleep(1)
                file_input = await upload_modal.query_selector("input[type='file']")
                if not file_input:
                    log("❌ File input niet gevonden in upload modal")
                    await browser.close()
                    resultaat["fout"] = "File input niet gevonden"
                    return resultaat

                log(f"Stap 17: Bestand instellen: {Path(pdf_pad).name}")
                await file_input.set_input_files(pdf_pad)
                await asyncio.sleep(1)
                log("Stap 17: ✓ Bestand geselecteerd")

                # ── 18. Upload knop klikken in popup ──────────────────────
                log("Stap 18: Wachten tot Upload-knop enabled is...")
                try:
                    await upload_modal.wait_for_selector(
                        "#Upload:not([disabled])", state="visible", timeout=10_000
                    )
                    await upload_modal.click("#Upload")
                    await asyncio.sleep(3)
                    log("Stap 18: ✓ Upload geklikt")
                except Exception as e:
                    log(f"Stap 18: ⚠ Upload knop niet gevonden of niet enabled: {e}")

                # ── 19. Wacht tot bestandsnaam zichtbaar in hoofdpagina ───
                log("Stap 19: Wachten tot bestandsnaam zichtbaar is na upload...")
                bestandsnaam_ok = False
                for _ in range(30):
                    await asyncio.sleep(1)
                    try:
                        el = await tc.query_selector("#UZ_ATTACHMENT_ATTACHUSERFILE")
                        if el:
                            tekst = (await el.inner_text()).strip()
                            if tekst:
                                log(f"Stap 19: ✓ Bestandsnaam zichtbaar: '{tekst}'")
                                bestandsnaam_ok = True
                                break
                    except Exception:
                        pass
                if not bestandsnaam_ok:
                    log("Stap 19: ⚠ Bestandsnaam niet verschenen na 30s — doorgaan")

                # ── 20. Beschrijving invullen = "Service rapport" ─────────
                log("Stap 20: Beschrijving 'Service rapport' invullen...")
                try:
                    await tc.wait_for_selector("#UZ_ATTACHMENT_DESCR50_MIXED", state="visible", timeout=10_000)
                    await tc.fill("#UZ_ATTACHMENT_DESCR50_MIXED", "Service rapport")
                    log("Stap 20: ✓ Beschrijving ingevuld")
                except Exception as e:
                    log(f"Stap 20: ⚠ Beschrijving invullen mislukt: {e}")

                # ── 21. OK klikken ─────────────────────────────────────────
                log("Stap 21: OK klikken...")
                try:
                    await tc.click('[id="#ICSave"]')
                    await asyncio.sleep(3)
                    log("Stap 21: ✓ OK geklikt")
                except Exception as e:
                    log(f"Stap 21: ⚠ OK klikken mislukt: {e}")

                # ── 22. Wacht tot bijlage zichtbaar in grid ────────────────
                log("Stap 22: Wachten tot bijlage in grid staat...")
                try:
                    await tc.wait_for_selector(
                        "#UZ_ATTACHMENTVW_ATTACHUSERFILE\\$0", state="visible", timeout=15_000
                    )
                    bijlage_tekst = await tc.locator("#UZ_ATTACHMENTVW_ATTACHUSERFILE\\$0").inner_text()
                    log(f"Stap 22: ✓ Bijlage in grid: '{bijlage_tekst.strip()}'")
                except Exception as e:
                    log(f"Stap 22: ⚠ Bijlage grid niet gevonden: {e}")

                # ── 23. Type instellen op SR ───────────────────────────────
                log("Stap 23: Bijlagetype 'SR' selecteren...")
                try:
                    await tc.wait_for_selector("#UZ_ATTACH_TYPE\\$0", state="visible", timeout=10_000)
                    await tc.select_option("#UZ_ATTACH_TYPE\\$0", value="SR")
                    await asyncio.sleep(2)
                    log("Stap 23: ✓ Type 'SR' geselecteerd")
                except Exception as e:
                    log(f"Stap 23: ⚠ Type selecteren mislukt: {e}")

                # ── 24. Opslaan na bijlage ─────────────────────────────────
                log("Stap 24: Opslaan na bijlage...")
                await page.click('[id="#ICSave"]')
                await asyncio.sleep(3)
                log("Stap 24: ✓ Opgeslagen")

                pdf_upload_succesvol = True  # PDF upload was successful
            else:
                log("Stap 14: Geen PDF-pad beschikbaar — bijlage overgeslagen")

            # ── 25. Rapportage-tab openen en status op UITGEV ──────────────
            if zet_uitgev:
                log("Stap 25: Rapportage-tab openen...")
                tc = await zoek_win1_frame(page, log=log)
                await tc.click("#ICTAB_2")
                await asyncio.sleep(2)
                log("Stap 25: ✓ Rapportage-tab geladen")

                # ── 26. Status op UITGEV instellen ────────────────────────────
                log("Stap 26: Status 'UITGEV' instellen...")
                try:
                    await tc.wait_for_selector('[id="UZFM_WO_WRK_UZ_WO_STAT_WZ_TECH$70$"]',
                                               state="visible", timeout=10_000)
                    await tc.select_option('[id="UZFM_WO_WRK_UZ_WO_STAT_WZ_TECH$70$"]', value="UITGEV")
                    await asyncio.sleep(1)
                    log("Stap 26: ✓ Status 'UITGEV' ingesteld")
                except Exception as e:
                    log(f"Stap 26: ⚠ Status instellen mislukt: {e}")

                # ── 27. Opslaan en status verifiëren ──────────────────────────
                log("Stap 27: Opslaan en status verifiëren...")
                await page.click('[id="#ICSave"]')
                eindstatus = ""
                for poging in range(15):
                    await asyncio.sleep(2)
                    try:
                        eindstatus = (await tc.locator("#UZFM_WO_UZ_WO_STATUS")
                                      .inner_text(timeout=3_000)).strip()
                        log(f"Stap 27: Status = '{eindstatus}' (poging {poging+1})")
                        if eindstatus == "UITGEV":
                            log("Stap 27: ✓ Status bevestigd als 'UITGEV'")
                            break
                    except Exception as e:
                        log(f"Stap 27: Status niet leesbaar: {e}")
                if eindstatus != "UITGEV":
                    log("Stap 27: ⚠ Status 'UITGEV' niet bevestigd na opslaan")
                else:
                    await browser.close()
            else:
                log("Stap 25-27: Overgeslagen — WO blijft op status 'INUITV' staan")
                await browser.close()

            # ── Resultaat opslaan ──────────────────────────────────────────
            sr_update(rapport_id,
                      wo_id=wo_id or "",
                      status="wo_aangemaakt" if wo_id else "fout",
                      log=log_lijnen)

            resultaat["ok"] = bool(wo_id)
            resultaat["wo_id"] = wo_id
            log(f"{'✓' if wo_id else '❌'} WO aanmaken {'geslaagd' if wo_id else 'mislukt'}"
                f" — WO-ID: {wo_id}")

    except Exception as e:
        log(f"❌ Onverwachte fout: {e}")
        resultaat["fout"] = str(e)
        sr_update(rapport_id, status="fout", log=log_lijnen)

    return resultaat


# ── Synchrone wrappers (voor gebruik vanuit Flask) ────────────────────────────

def zoek_tnummer_op_sn(serienummer: str, stap_log=None) -> dict:
    """Synchrone wrapper voor zoek_tnummer_via_ps."""
    return asyncio.run(zoek_tnummer_via_ps(serienummer, stap_log))


def maak_wo_voor_service_rapport(rapport_id: str, stap_log=None, zet_uitgev: bool = True) -> dict:
    """Synchrone wrapper voor maak_wo_service_rapport."""
    return asyncio.run(maak_wo_service_rapport(rapport_id, stap_log, zet_uitgev=zet_uitgev))