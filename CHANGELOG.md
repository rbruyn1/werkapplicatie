# Changelog

Alle noemenswaardige wijzigingen aan deze applicatie worden hier bijgehouden.
Dit bestand is gestart op 17/09/2026 — wijzigingen van vóór die datum staan
niet met terugwerkende kracht vermeld (zie `git log` voor de volledige
historiek).

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

