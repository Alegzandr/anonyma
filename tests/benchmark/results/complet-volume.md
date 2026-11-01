# Résultats du banc d'essai — complet-volume

## Profil complet

- Durée du lot : 442.0 s pour 25 documents
- Rappel identifiants directs : **100.0 %** (324/324)
- Rappel autres types : 96.7 % (58/60)
- Précision (segments remplacés correspondant à une valeur de référence) : 95.5 % (322/337) ; hors dates non liées à une naissance : 96.1 %
- Phrases témoins intactes : 82.2 % (37/45)

| Type | Rappel | Valeurs |
|---|---|---|
| PERSONNE | 100.0 % | 72 |
| EMAIL | 100.0 % | 49 |
| TELEPHONE | 100.0 % | 51 |
| IBAN | 100.0 % | 32 |
| CARTE | 100.0 % | 5 |
| NIR | 100.0 % | 7 |
| ADRESSE | 100.0 % | 30 |
| PLAQUE | 100.0 % | 19 |
| IP | 100.0 % | 8 |
| DATE_NAISSANCE | 100.0 % | 34 |
| IDENTIFIANT | 100.0 % | 17 |
| ORGANISATION | 100.0 % | 8 |
| LIEU | 95.8 % | 24 |
| FONCTION | 100.0 % | 11 |
| SIREN | 100.0 % | 2 |
| SIRET | 100.0 % | 2 |
| TVA | 100.0 % | 2 |
| URL | 100.0 % | 4 |
| NUMERO_DOSSIER | 66.7 % | 3 |
| GPS | 100.0 % | 1 |

| Format | Rappel direct | Précision | Statuts |
|---|---|---|---|
| 7z | 100.0 % | 100.0 % | a_revoir ×1 |
| csv | 100.0 % | 100.0 % | anonymise ×1 |
| docx | 100.0 % | 93.3 % | a_revoir ×1 |
| eml | 100.0 % | 97.4 % | a_revoir ×1, anonymise ×1 |
| html | 100.0 % | 100.0 % | a_revoir ×1 |
| jpg | 100.0 % | — | a_revoir ×1 |
| json | 100.0 % | 95.5 % | anonymise ×1 |
| mbox | 100.0 % | 96.7 % | anonymise ×1 |
| msg | 100.0 % | 100.0 % | anonymise ×1 |
| ods | 100.0 % | 100.0 % | anonymise ×1 |
| odt | 100.0 % | 76.9 % | anonymise ×1 |
| pdf | 100.0 % | 100.0 % | a_revoir ×1, anonymise ×2 |
| png | 100.0 % | — | anonymise ×1 |
| pptx | 100.0 % | 90.9 % | a_revoir ×1 |
| rtf | 100.0 % | 100.0 % | anonymise ×1 |
| txt | 100.0 % | 91.2 % | a_revoir ×2, anonymise ×3 |
| xlsx | 100.0 % | 100.0 % | anonymise ×1 |
| xml | 100.0 % | 66.7 % | a_revoir ×1 |
| zip | 100.0 % | 100.0 % | anonymise ×1 |

### Fuites

- `23_txt_temoins` LIEU (autre) « Isère » — 23_txt_temoins/notes_internes.txt [octets utf-8] « Isère »; 23_txt_temoins/notes_internes.txt « Isère »
- `24_txt_anglais` NUMERO_DOSSIER (autre) « INC-2026-0315 » — 24_txt_anglais/incident_report.txt [octets latin-1] « inc20260315 »; 24_txt_anglais/incident_report.txt [octets utf-8] « inc20260315 »; 24_txt_anglais/incident_report.txt « inc20260315 »

### Faux positifs (exemples)

- `01_txt_lettre` (1) : 4 mars 2026
- `03_json_crm` (1) : 2026 02 01
- `04_xml_sinistre` (1) : le boucher du marche couvert
- `06_eml_fil` (2) : batiment b | marchand de journaux
- `08_mbox_boite` (1) : 12 mars 2026 a 14 h 30
- `10_docx_audit` (1) : le boucher du marche couvert
- `12_pptx_projet` (1) : projet hermine
- `13_odt_compte_rendu` (3) : 12 mars 2026 a 14 h 30 | salle bleue | 16 h
- `23_txt_temoins` (4) : le boucher du marche couvert | 12 mars 2026 a 14 h 30 | salle bleue | fontaine
