# Changelog

Alle noemenswaardige wijzigingen aan deze applicatie worden hier bijgehouden.
Dit bestand is gestart op 17/09/2026 — wijzigingen van vóór die datum staan
niet met terugwerkende kracht vermeld (zie `git log` voor de volledige
historiek).

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

