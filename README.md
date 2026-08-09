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

### 1. Bestanden ophalen
Clone deze repo (aanbevolen — updates ophalen wordt dan gewoon `git pull`):
```
git clone <repo-url> C:\Werkorder-Dashboard
```
Of zet alle bestanden handmatig in één map, bijv. `C:\Werkorder-Dashboard\`.

### 2. `installeer.ps1` uitvoeren
Rechtsklik op `installeer.ps1` → **"Uitvoeren met PowerShell"** (of `.\installeer.ps1`
in een PowerShell-venster in die map). Dit doet **alles** in één keer, geen losse
stappen meer nodig:

1. **Python controleren/installeren** — is Python al aanwezig, dan gaat het script
   gewoon verder; ontbreekt het, dan wordt Python automatisch geïnstalleerd via
   `winget` (geen handmatige download/installatie meer nodig).
2. pip bijwerken
3. Python-pakketten installeren (`flask`, `playwright`, `openpyxl`, `watchdog`,
   `cryptography`, ...)
4. Playwright-browsers installeren (Microsoft Edge + Chromium — dit hoeft maar
   **één keer**, nadien nooit meer driver-updates)
5. **Configuratie instellen** — maakt `config.json` aan vanuit `config.example.json`
   (als het nog niet bestaat) en vraagt interactief welk pakket je wil installeren
   (Volledig of Beperkt — zie "Beperkt pakket" verderop)
6. Snelkoppeling aanmaken op het bureaublad

### 3. Inloggegevens instellen
Start de app (zie "Starten" hieronder) en klik op de aanmeld-badge rechtsboven in
het dashboard. Vul je PeopleSoft-gebruikersnaam en -wachtwoord in — dat gebeurt
via de app zelf, **niet** door `config.json` handmatig te bewerken. Het wachtwoord
wordt daarbij versleuteld opgeslagen (`crypto_utils.py`, veld `password_enc` in
`config.json`).

`config.json` en het bijhorende `secret.key` bevatten/ontsluiten gevoelige gegevens
en horen niet gedeeld of gecommit te worden (staan al in `.gitignore`).

---

## Starten

Na de installatie: dubbelklik de snelkoppeling **"Werkorder Dashboard"** op je
bureaublad.

Handmatig kan ook, vanuit de map:
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

## Logging
Het persistente logbestand (`logs/app.log`) bevat standaard enkel **WARNING en
ERROR** — niet elke routinematige INFO-regel, om te vermijden dat het bestand
onnodig aangroeit. Instelbaar in `config.json`:
```json
"log_niveau": "WARNING"
```
Andere waarden: `"DEBUG"`, `"INFO"`, `"ERROR"`. Dit heeft geen invloed op de
live voortgangsweergave tijdens een WO-actie (bv. de stap-voor-stap-log bij
Service Rapporten/Snelle Werkorder) — dat loopt via een apart mechanisme.

`debug_modus` (standaard `false`) schakelt Flask's interactieve debugger uit.
Enkel aanzetten voor lokaal ontwikkelen/foutopsporing, nooit permanent, want
de debugger laat via de browser willekeurige Python-code uitvoeren bij een
onverwerkte fout:
```json
"debug_modus": false
```

## Actieve uren
De scraper haalt enkel werkorders op tussen 06:30 en 17:00 (instelbaar in `config.json`).
Buiten die uren slaapt de scraper maar blijft het dashboard bereikbaar.

## Manuele refresh
In het dashboard staat een "↻ VERNIEUWEN"-knop om op elk moment handmatig een nieuwe
scrape te starten.

## Bijwerken vanuit de app (geen `start.py` nodig)
Elke pagina controleert op de achtergrond (elke 15 minuten) of er een nieuwe versie
op GitHub staat. Is dat zo, dan verschijnt rechtsboven een badge **"🔄 Update
beschikbaar"**.

Klik erop → een popup vraagt bevestiging → pas dan gebeurt er iets: `git pull` +
de app herstart zichzelf (`os.execv`, dezelfde manier van opstarten als daarvoor
blijft behouden — `start.py` blijft `start.py`, een snelkoppeling naar `app.py`
blijft `app.py`). De pagina herlaadt zelf zodra de app terug online is.

Dit gebeurt **nooit automatisch** — enkel na een expliciete klik + bevestiging,
net om te vermijden dat een update ongevraagd tussenkomt terwijl iemand midden in
een actie zit (het probleem met `start.py`'s bestandswatcher-herstart).

Draaien meerdere pc's tegelijk vanaf dezelfde gedeelde `Z:`-map? Een korte
(30 seconden) lock voorkomt dat twee pc's tegelijk `git pull` doen — klikt iemand
anders net tegelijk, dan krijg je een duidelijke melding om het even opnieuw te
proberen.

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

## Beperkt pakket (standalone Service Rapporten + Snelle Werkorder)
Een collega die enkel service-rapporten wil verwerken en snel een toestel-WO
wil aanmaken, heeft de rest van de app (dashboard, thuisdialyse, RO-staalname,
staalresultaten, logs) niet nodig — en dus ook geen draaiende
PeopleSoft-scraper of OneDrive-sync.

**Bij installatie**: `installeer.ps1` vraagt in stap 5/6 welk pakket je wil
("1) Volledig" of "2) Beperkt") en maakt `config.json` aan (vanuit
`config.example.json`, als dat bestand nog niet bestaat) met het juiste
`modus`-veld erin.

Achteraf omschakelen kan ook, gewoon in `config.json` van die installatie:
```json
"modus": "beperkt"
```
(standaardwaarde is `"volledig"` — ontbreekt het veld, dan verandert er niets)

Gevolgen van `"beperkt"`:
- Bij het opstarten wordt er **geen** achtergrond-scraper, lock-ping of
  OneDrive-sync gestart — enkel de Flask-server zelf.
- De browser opent meteen op **Service Rapporten** in plaats van het
  dashboard.
- De menubalk toont enkel "🔧 Service Rapporten" en "⚡ Snelle Werkorder".
- Elke andere pagina/route stuurt door naar `/service-rapporten` (of geeft
  een nette 403 op API-aanroepen).
- Service Rapporten en Snelle Werkorder blijven **volledig functioneel**: die
  loggen elk zelf per actie in bij PeopleSoft via Playwright, onafhankelijk
  van de achtergrond-scraper.

Zo'n beperkte installatie is een gewone, aparte kopie van deze repo (eigen
`config.json`/`secret.key`, niet gedeeld met een volledige installatie) —
niet iets wat je binnen één en dezelfde draaiende app aan/uit zet.
