# Sjabloonformaat (prototype — nog niet gekoppeld aan de live app)

Elk `*.json`-bestand in deze map beschrijft één fabrikant/rapportformaat.
Zie `siemens.json` als volledig werkend voorbeeld, bewezen identiek aan de
handgeschreven `_extraheer_siemens()` in `wo_service_rapport.py`
(zie `template_engine.py`).

```json
{
  "naam": "korte_id",              // wordt het 'formaat'-veld in het resultaat
  "firma": "Weergavenaam",
  "detectie": {
    "bevat_alle": ["tekst 1", "tekst 2"]   // AND-logica, zoals vandaag
  },
  "velden": {
    "sn": { "patroon": "regex met (capture-groep)" },
    "product": { "patroon": ["eerste kandidaat", "terugval-kandidaat"] },
    "omschrijving_kort": {
      "patroon": "...",
      "verwerking": ["strip", "join_whitespace"]  // optioneel, in volgorde
    }
  },
  "bonus_velden": {
    "t_nummer": { "patroon": "..." }        // enkel toegevoegd als gevonden
  }
}
```

- `patroon` mag één regex-string zijn, of een lijst — dan wordt de eerste
  match in de lijst gebruikt (terugvalpatronen voor lay-outvarianten).
- `verwerking` is een lijst van naamregistratie-stappen uit
  `template_engine.POSTPROCESSORS` (vandaag: `strip`, `join_whitespace`,
  `hhmm_naar_decimaal`). Bewust géén vrije code/eval — nieuwe stappen
  vereisen wel een (kleine, generieke) Python-toevoeging, maar een nieuw
  *rapport* nooit.

Dit is een **voorstel**, nog niet ingeschakeld. Zie de openstaande vragen
die hierover gesteld zijn voor de beslissingen die nog genomen moeten
worden vóór dit de bestaande `_extraheer_<vendor>()`-functies vervangt.

## Bevindingen uit reële voorbeelden (Siemens, ProCare, Radiometer, Stryker, Terumo, B.Braun)

- **Siemens, Radiometer**: volledig 1-op-1 declaratief te vangen, bewezen
  identiek resultaat tussen sjabloon en Python-functie.
- **ProCare**: gedeeltelijk. `sn`/`product`/`werkorder_nr`/`datum`/
  `type_verzoek`/bonus-`t_nummer` werken declaratief. `omschrijving_kort`
  niet: door de kolomlay-out van de brontabel komt een label
  ("Samenvatting uitgevoerde werkzaamheden") *middenin* de waarde terecht,
  en moet er met een aparte regex-substitutie uit geknipt worden. Het
  huidige sjabloonformaat (`patroon` + orde van `verwerking`-stappen) kan
  dat nog niet uitdrukken - zou een nieuwe stap-soort vereisen zoals
  `{"verwijder": "regex"}`.
- **Stryker**: niet in JSON-sjabloon gezet. Deze rapporten lopen vaak tot
  50+ pagina's (bijgevoegde foto's/QC-datasheets), en een volledige
  tekst-extractie duurde in de praktijk **80+ seconden** - veel te traag
  voor een interactieve upload. De Python-functie leest daarom bewust
  zelf slechts de eerste 3 pagina's via een eigen `pdfplumber`-call, in
  plaats van de al ingelezen `tekst`-parameter te gebruiken zoals alle
  andere extractors. **Dit is de belangrijkste, nog onbeantwoorde vraag
  voor het sjabloonformaat**: een puur tekst-in/dict-uit sjabloon kan dit
  soort paginabeperking niet zelf uitdrukken - dat vereist een
  `"max_paginas": 3`-veld in het sjabloon dat de PDF-lezer (niet de
  matcher) moet respecteren.
- Als gevolg hiervan is de hoofddispatcher (`extraheer_sn_uit_pdf` in
  `wo_service_rapport.py`) aangepast: detectie gebeurt nu op maximaal de
  eerste **5** pagina's in plaats van het volledige document, met een
  volledige herlezing enkel als terugval bij een écht onbekend formaat.
