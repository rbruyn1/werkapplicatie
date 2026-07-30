# Werkorder Dashboard — UZ Leuven

## Wat doet dit?
Interne webapp voor de technische dienst dialyse van UZ Leuven. Logt automatisch in op
PeopleSoft en bundelt in één dashboard:

- **Dashboard** (`/`) — openstaande werkorders + installatie-logs, met alarmen (RO/HR/CCS) die je moet bevestigen. Ververst zelf, met een handmatige "↻ VERNIEUWEN"-knop.
- **Thuisdialyse** (`/thuisdialyse`) — staalnames van thuisdialyse-patiënten opvolgen en WO's aanmaken.
- **Staalresultaten** (`/dialyse-resultaten`) — dialyse-staalname-resultaten opzoeken/afwerken in PeopleSoft, of manueel een staal toevoegen.
- **RO Staalnames** (`/ro-staalname`) — RO-waterstaalnames opvolgen.
- **Service Rapporten** (`/service-rapporten`) — externe service-PDF's (Vantive, Nipro, B.Braun, Fluke, Fresenius) uploaden; serienummer, T-nummer en WO worden automatisch opgezocht/aangemaakt.
- **Snelle Werkorder** (`/toestel-werkorder`) — snel een WO aanmaken voor een toestel, zonder wisselstukken.
- **Logs** (`/logs`) — rauwe logbestanden inkijken.

Alle pagina's delen dezelfde menubalk (`templates/_nav.html`).

---

## Eénmalige installatie

### 1. Python installeren
Download Python 3.11 of nieuwer via https://www.python.org/downloads/
Zorg dat je bij de installatie "Add Python to PATH" aanvinkt.

### 2. Bestanden neerzetten
Zet alle bestanden in één map, bijv. `C:\Werkorder-Dashboard\`

### 3. Pakketten installeren
Open een opdrachtprompt (cmd) in die map en voer uit:

```
pip install -r requirements.txt
playwright install chromium
```

> `playwright install chromium` downloadt een eigen browser (~150MB).
> Dit hoeft maar **één keer**. Daarna nooit meer driver-updates!

### 4. Inloggegevens instellen
Open `config.json` en vul in:
```json
{
  "username": "jouw_username",
  "password": "jouw_wachtwoord",
  ...
}
```
Het wachtwoord wordt versleuteld opgeslagen (`crypto_utils.py`, veld `password_enc`).
`config.json` en het bijhorende `secret.key` bevatten/ontsluiten gevoelige gegevens en
horen niet gedeeld of gecommit te worden.

---

## Starten

```
python start.py
```

`start.py` is de aanbevolen manier: het start `app.py` op en herstart het automatisch
zodra een `.py`- of `.html`-bestand wijzigt (bv. na een git pull of handmatige aanpassing).
Rechtstreeks `python app.py` kan ook, maar dan moet je zelf herstarten na wijzigingen.

Het dashboard is dan bereikbaar op:
- **Jijzelf:**    http://localhost:5000
- **Collega's:**  http://<jouw-ip-adres>:5000

Je IP-adres vind je via `ipconfig` in de opdrachtprompt (zoek naar "IPv4-adres").

---

## Automatisch opstarten met Windows

Wil je dat het dashboard automatisch start als je PC opstart?

1. Maak een bestand `start.bat` aan met:
   ```
   @echo off
   cd /d C:\Werkorder-Dashboard
   python start.py
   ```
2. Druk op `Win + R`, typ `shell:startup` en druk Enter.
3. Zet een snelkoppeling naar `start.bat` in die map.

---

## Structuur

```
werkorder-dashboard/
├── app.py                    Flask-server: alle routes/API-endpoints
├── start.py                  Auto-restart wrapper (herstart app.py bij .py/.html-wijzigingen)
├── scraper.py                Playwright-scraper die periodiek PeopleSoft-werkorders ophaalt
├── crypto_utils.py           Versleutelde opslag van het PeopleSoft-wachtwoord
├── uitvoerder_utils.py       Bepaalt welke OPRID als "Uitvoerder" ingevuld wordt per gebruiker
├── onedrive_sync.py          Spiegelt *.json/*.txt naar OneDrive voor een lees-only kopie buiten het netwerk
│
├── wo_aanmaken.py            WO aanmaken voor thuisdialyse-stalen (Playwright)
├── wo_ro_staalname.py        RO-staalname WO's opzoeken
├── wo_toestel_werkorder.py   Snelle-werkorder flow (toestel → T-nummer → WO)
├── wo_service_rapport.py     Service-rapport PDF's inlezen + SN/T-nummer/WO-flow
├── wo_lage_temperatuur.py    Automatische afhandeling "Lage Temperatuur"-WO's (di/do/za)
├── wo_melkkoppelingen.py     Automatische afhandeling "melkkoppelingen"-WO's
├── ess_bestelling.py         Automatische ESS-bestelling bij CCS-container-vervanging
├── dialyse_resultaten.py     Dialyse-staalname-resultaten opzoeken/verzenden
├── import_excel.py           Eenmalige/handmatige import vanuit het Thuisdialyse Excel-sheet
│
├── config.json                Jouw instellingen & (versleuteld) wachtwoord
├── data.json / *.json         Lokale data-opslag (werkorders, stalen, rapporten, logs, alerts, ...) — auto-aangemaakt
├── requirements.txt            Python-pakketten
└── templates/
    ├── _nav.html               Gedeelde menubalk, gebruikt door alle pagina's
    ├── dashboard.html
    ├── thuisdialyse.html
    ├── dialyse_resultaten.html
    ├── ro_staalname.html
    ├── service_rapporten.html
    ├── toestel_werkorder.html
    └── logs.html
```

---

## Actieve uren
De scraper haalt enkel werkorders op tussen 06:30 en 17:00 (instelbaar in `config.json`).
Buiten die uren slaapt de scraper maar blijft het dashboard bereikbaar.

## Manuele refresh
In het dashboard staat een "↻ VERNIEUWEN"-knop om op elk moment handmatig een nieuwe
scrape te starten.

## OneDrive-sync (draaien op een pc buiten het netwerk)
Als meerdere pc's op kantoor vanaf dezelfde gedeelde map draaien (bv.
`Z:\APPLICATIE WO\werkorder-dashboard`), is er telkens één **scraper-master**
(zie `scraper.lock`) die effectief bij PeopleSoft aanmeldt; de rest leest gewoon mee.

Wil je de app ook draaien op een pc **buiten** het UZ Leuven-netwerk (bv. thuis)? Zet
op die pc een volledige, aparte kopie van de applicatie (bv. via git) in een map die
via OneDrive synct. Die pc kan niet bij PeopleSoft, maar toont via `onedrive_sync.py`
wel de actuele data — automatisch in "Leesmodus" (zie hierboven).

Zet daarvoor in `config.json` van de **scraper-master**:
```json
"onedrive_sync": {
  "enabled": true,
  "pad": "C:\\Users\\jouw_naam\\OneDrive - UZ Leuven\\werkorder-dashboard",
  "sync_config": false
}
```
- Enkel de scraper-master kopieert; op de andere pc's gebeurt niets.
- Elke wijziging aan een `.json`- of `.txt`-bestand in de root wordt (met korte
  vertraging) automatisch gekopieerd naar `pad`.
- `config.json` en `secret.key` (je PeopleSoft-inloggegevens) worden **nooit**
  gesynct, tenzij je `sync_config` bewust op `true` zet.
- Submappen (`logs/`, `uploads/`, ...) en `.py`/`.html`-bestanden worden niet
  meegenomen — die code krijg je op de tweede pc via git.
