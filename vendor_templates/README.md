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
