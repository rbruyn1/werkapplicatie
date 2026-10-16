# Changelog

Alle noemenswaardige wijzigingen aan deze applicatie worden hier bijgehouden.
Dit bestand is gestart op 17/09/2026 — wijzigingen van vóór die datum staan
niet met terugwerkende kracht vermeld (zie `git log` voor de volledige
historiek).

## 2026-10-16

### RO-formulieren: taken op het werkorder
- Het automatisch aangemaakte WO krijgt nu taaklijnen met de tekstwaarden uit het
  formulier: 0000001962 aantal zakken zout, 0000001963 ingangsdruk, 0000001964
  stand waterteller, 0000001965 Soft Control, 0000001966 geleidbaarheid,
  0000001967 controle hittedesinfectie, 0000001968 opmerkingen (in die volgorde).
- Een leeg veld of "nvt" (ook "NVT", "n vt", "n.v.t.") geeft géén taaklijn. Andere
  waarden, ook "0", wel. Regeleinden in opmerkingen worden een spatie en de tekst
  wordt begrensd op 250 tekens (wordt gelogd).
- De kolom wordt in de sheet gezocht op een deel van de header, hoofdletter- en
  spatie-ongevoelig (zie `TAKEN` in `wo_ro_sheets.py`; "Controle SoftControl" wordt
  gevonden met "softcontrol"). Per taak overschrijfbaar via `ro_sheets.taak_kolommen`
  (`{"0000001963": "zoekwoord"}`). Wordt een kolom niet gevonden, dan staat dat met
  de headers in `logs/ro_sheets_wo.log` en wordt die taak overgeslagen.
- De taken worden ingevuld vóór het definitief opslaan (UITGEV). Daarna worden de
  taak-ID's teruggelezen en vergeleken. Lukt het invullen niet (taak niet herkend,
  taaklijn 0 al ingevuld) of klopt de controle niet, dan volgt de popup-alert zoals
  bij andere fouten en moet het WO manueel nagekeken worden: het staat dan mogelijk
  nog op INUITV, zonder werkuren.
- Ingaven die vóór deze wijziging al verwerkt waren (`ro_sheets_staat.json`) krijgen
  niet achteraf taken.
- `ro_sheets.taken_toevoegen: false` schakelt dit uit. Dry-run logt de taken die
  aangemaakt zouden worden.
- Nieuwe gedeelde helpers in `wo_aanmaken.py`: `vul_taaklijnen_tekst` en
  `lees_taak_ids`. `dialyse_resultaten.py` is ongewijzigd.

### RO-formulieren → automatisch werkorder (eerste versie)
- Nieuwe module `wo_ro_sheets.py`: pollt elke 5 minuten de Google Sheets achter
  de RO-formulieren (RO1 465, RO2 465, RO 304) en maakt voor elke nieuwe ingave
  een WO "Aanvullen zout + algemene controle" aan met 1 werkuur voor `rbruyn1`
  (status UITGEV, zelfde aanpak als de CCS-container-WO).
- Draait enkel op de scraper-master, in een eigen achtergrondthread.
- Eerste keer = baseline: bestaande rijen worden niet verwerkt. Elke rij wordt
  maar één keer verwerkt (`ro_sheets_staat.json`). Bij een fout geen blinde
  herpoging, wel een popup-alert om manueel aan te maken.
- Staat standaard UIT en in `dry_run`: zet in `config.json` onder `ro_sheets`
  `enabled: true`, vul de T-nummers in en zet pas daarna `dry_run` op `false`.
  Zie `config.example.json`. Log: `logs/ro_sheets_wo.log`. Dat log is in de
  Logs-pagina enkel zichtbaar als de PC-gebruiker `rbruyn1` is (ook `sa_water`
  op de master-pc); voor anderen ontbreekt het in de lijst en geeft het 404.
- `vul_werkuren_lijn` controleert het urenveld nu tegen de meegegeven uren i.p.v.
  hard tegen 0.5.

## 2026-10-14

### Service rapporten — OCR-terugval (Tesseract) opstarten
- Tesseract wordt nu ook gevonden als het niet op PATH staat (standaardmap
  `C:\Program Files\Tesseract-OCR`). Winget draait enkel nog bij een echt
  ontbrekende installatie, i.p.v. bij elke start.
- Het Nederlandse taalpakket komt in een eigen map `tessdata\` naast `app.py`
  (eng + nld) en `TESSDATA_PREFIX` wijst daarheen. Dat werkt zonder
  adminrechten; voorheen faalde de download met "Permission denied" op
  `C:\Program Files`. De map staat in `.gitignore`.
- `fitz` vervangen door `pymupdf` (de oude import gaf bij elke start een
  waarschuwing). Vereist PyMuPDF 1.24.3 of nieuwer; een oudere versie wordt bij
  het opstarten automatisch bijgewerkt. De OCR-code valt voor de zekerheid nog
  terug op `fitz`.

### Herstart na "update nu"
- Op Windows geeft `os.execv` de PowerShell-prompt meteen terug terwijl de app
  op de achtergrond doorloopt. Vermoedelijk bleef daardoor een verweesde
  instantie poort 5000 bezet houden (Werkzeug laat op Windows dubbele binding
  toe), zodat de herstarte app geen verzoeken kreeg en pas na meerdere
  handmatige herstarts werkte.
- `python app.py` start nu eerst een kleine supervisor die de echte app als
  kindproces draait en in de voorgrond van de console blijft. Na een update
  sluit de app af met exitcode 75 en start de supervisor meteen een verse
  instantie (nieuwe code, zelfde console, poort vrij). Ctrl+C stopt alles.
- `start.py` zet zelf `WO_SUPERVISED`, zodat er niet dubbel gesupervised wordt.
  Zonder supervisor (andere opstartwijze) blijft de oude `os.execv`-herstart
  gelden.
- De eerste herstart naar deze versie verliep nog via de oude code; de nieuwe
  herstart wordt pas bij de update daarna gebruikt.

## 2026-10-12

### RO-resultaten — keuze bij meerdere gevonden werkorders
- Bij "WO opzoeken" op de RO-resultaten werd altijd stilzwijgend de eerste
  gevonden WO genomen. Worden er meerdere WO's gevonden, dan verschijnt nu een
  popup met alle opties (WO-nummer, status, omschrijving) om de juiste te
  kiezen. INUITV-WO's staan in het groen.
- "Later kiezen" slaat het staal over; via de knop **⚠ Kies WO (n)** in de
  WO-kolom kan alsnog gekozen worden. Zolang de keuze openstaat wordt het staal
  bij verzenden overgeslagen met de melding "WO-keuze nog te maken".
- Nieuwe route `POST /api/ro-resultaten/wo-keuze`; de gekozen WO moet bij de
  opgeslagen opties van dat staal horen.
- Zelfde keuze nu ook voor de TD-stalen (zie hieronder).

### TD-resultaten — keuze bij meerdere gevonden werkorders
- Ook bij de TD-stalen (zowel "WO opzoeken" als het verwerken van PDF-stalen)
  wordt niet langer stilzwijgend de eerste WO genomen: bij meerdere WO's komt
  dezelfde popup als bij RO, met de knop **⚠ Kies WO (n)** in de WO-kolom.
- De popup toont enkel stalen uit de lopende zoekjob, zodat een eerder
  overgeslagen keuze niet bij elke nieuwe zoekopdracht terugkomt.
- Nieuwe route `POST /api/dialyse-resultaten/wo-keuze`; RO en TD delen de
  validatie (gekozen WO moet bij de opgeslagen opties horen).
- Zonder gemaakte keuze wordt het staal bij verzenden overgeslagen met de
  melding "WO-keuze nog te maken".

### TD-resultaten — "WO opzoeken selectie" zocht alle stalen op
- De route negeerde de selectie en zocht steeds alle stalen op. Nu worden enkel
  de geselecteerde stalen opgezocht; de overige stalen blijven ongewijzigd
  bewaard (voorheen zou de lijst vervangen worden door enkel de gezochte).
- De knop doet niets meer als er niets geselecteerd is, i.p.v. stilzwijgend alles
  op te zoeken. De knop "WO opzoeken" (alles) blijft ongewijzigd.

## 2026-09-17

### Service rapporten — bestaande PO-werkorder herkennen
- Bij een service rapport van het type "Preventief onderhoud" wordt nu eerst
  gezocht naar een reeds bestaande werkorder voor het T-nummer, vóór er een
  nieuwe wordt aangemaakt:
  - Bestaande WO in status **GOED** → het rapport wordt hieraan gekoppeld
    (geen dubbele WO meer).
  - Bestaande WO in status **INUITV** → er wordt niets aangemaakt; het
    rapport krijgt status "fout" met melding om de WO manueel te
    controleren in PeopleSoft.
  - Geen bestaande WO gevonden → een nieuwe WO wordt aangemaakt met type
    **PO** (voorheen altijd hardcoded op "AO").
- Nieuw bewerkbaar type-veld (Herstelling / Preventief onderhoud) per
  rapport in het overzicht van Service Rapporten — **standaard op
  "Herstelling"**; de gebruiker kiest zelf bewust voor "Preventief
  onderhoud" wanneer van toepassing.
- Fix: werktype-selectie "PO" in het zoekscherm vereist slechts 1x een druk
  op de toets "p" (was voorheen 2x, wat een verkeerde selectie kon geven).

### Service rapporten — meerdere PDF's per rapport/WO
- Nieuwe knop "📎+" per rapport om een extra PDF-bijlage te koppelen (bv.
  het onderhoudsprotocol naast het service rapport bij een PO). Extra
  bijlagen worden getoond als klein chipje met bestandsnaam en een ✕ om
  ze terug te verwijderen.
- Bij het aanmaken/koppelen van de WO worden het hoofd-service-rapport én
  alle gekoppelde extra bijlagen na elkaar naar dezelfde WO geüpload
  (beschrijving = bestandsnaam zonder extensie voor de extra bijlagen).
- Een extra bijlage kan nu ook rechtstreeks op de rij van het rapport
  gesleept worden (bv. een PDF-bijlage rechtstreeks vanuit Outlook, zonder
  ze eerst lokaal te moeten opslaan) — als alternatief op de "📎+"-knop.
- Fix: upload van een 2e (of volgende) bijlage mislukte met "Upload modal
  iframe niet gevonden", omdat de popup-iframenaam hardcoded op
  `ptModFrame_0` stond. PeopleSoft geeft elke volgende popup een oplopende
  naam (`ptModFrame_1`, `ptModFrame_2`, ...) — dit wordt nu dynamisch
  opgezocht.

### Service rapporten — automatische inschatting Herstelling/PO
- Nieuwe `_classificeer_type_verzoek()` in `wo_service_rapport.py`: scant de
  geëxtraheerde rapporttekst op sleutelwoorden ("Preventief onderhoud",
  "Preventive Maintenance", "Planned Maintenance", "PO volgens protocol",
  losstaand "PO"/"PM") om in te schatten of een rapport een PO betreft.
- Bij twijfel (geen enkele indicator gevonden) blijft de standaardwaarde
  altijd **"Herstelling"** — de gebruiker kan dit nog altijd manueel
  aanpassen via de dropdown vóór de WO aangemaakt wordt.
- Deze inschatting geldt voor alle rapportformaten (Fresenius, B.Braun,
  Nipro, ...), niet enkel de firma's waarvoor voorheen een vast type
  gehardcode stond.

### Genius-staalnameresultaten — PDF-bijlage upload gefixt
- Fix: dezelfde `ptModFrame_0`-hardcoding als bij de service rapporten zat
  ook in `dialyse_resultaten.py` (`afwerk_wo_voor_staal`), waardoor het
  analyseverslag (PDF) niet werd opgeladen bij de WO en de daaropvolgende
  stap ("commentaarveld") in timeout liep doordat de niet-gesloten popup de
  pagina blokkeerde. De popup-iframe wordt nu net als bij de service
  rapporten dynamisch opgezocht (`ptModFrame_N`).

