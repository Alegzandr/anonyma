# Résultats du banc d'essai — final

## Profil leger

- Durée du lot : 14.1 s pour 24 documents
- Rappel identifiants directs : **99.7 %** (321/322)
- Rappel autres types : 63.2 % (36/57)
- Précision (segments remplacés correspondant à une valeur de référence) : 93.5 % (290/310) ; hors dates non liées à une naissance : 99.3 %
- Phrases témoins intactes : 93.0 % (40/43)

| Type | Rappel | Valeurs |
|---|---|---|
| PERSONNE | 100.0 % | 71 |
| EMAIL | 100.0 % | 48 |
| TELEPHONE | 100.0 % | 51 |
| IBAN | 100.0 % | 32 |
| CARTE | 100.0 % | 5 |
| NIR | 100.0 % | 7 |
| ADRESSE | 100.0 % | 30 |
| PLAQUE | 100.0 % | 19 |
| IP | 100.0 % | 8 |
| DATE_NAISSANCE | 100.0 % | 34 |
| IDENTIFIANT | 94.1 % | 17 |
| ORGANISATION | 75.0 % | 8 |
| LIEU | 66.7 % | 24 |
| FONCTION | 9.1 % | 11 |
| SIREN | 100.0 % | 2 |
| SIRET | 100.0 % | 2 |
| TVA | 100.0 % | 2 |
| URL | 100.0 % | 4 |
| NUMERO_DOSSIER | 66.7 % | 3 |
| GPS | 100.0 % | 1 |

| Format | Rappel direct | Précision | Statuts |
|---|---|---|---|
| 7z | 100.0 % | 100.0 % | Anonymisé ×1 |
| csv | 100.0 % | 100.0 % | Anonymisé ×1 |
| docx | 100.0 % | 100.0 % | Anonymisé ×1 |
| eml | 100.0 % | 89.6 % | Anonymisé ×2 |
| html | 100.0 % | 100.0 % | Anonymisé ×1 |
| jpg | 100.0 % | — | Anonymisé ×1 |
| json | 100.0 % | 95.5 % | Anonymisé ×1 |
| mbox | 100.0 % | 78.9 % | Anonymisé ×1 |
| msg | 100.0 % | 94.4 % | Anonymisé ×1 |
| ods | 100.0 % | 100.0 % | Anonymisé ×1 |
| odt | 100.0 % | 90.0 % | Anonymisé ×1 |
| pdf | 100.0 % | 100.0 % | Anonymisé ×2 |
| png | 100.0 % | — | Anonymisé ×1 |
| pptx | 100.0 % | 100.0 % | Anonymisé ×1 |
| rtf | 100.0 % | 100.0 % | Anonymisé ×1 |
| txt | 97.5 % | 92.6 % | Anonymisé ×5 |
| xlsx | 100.0 % | 100.0 % | Anonymisé ×1 |
| xml | 100.0 % | 100.0 % | Anonymisé ×1 |
| zip | 100.0 % | 94.4 % | Anonymisé ×1 |

### Fuites

- `01_txt_lettre` LIEU (autre) « Nantes » — 01_txt_lettre/lettre_reclamation.txt [octets latin-1] « Nantes »; 01_txt_lettre/lettre_reclamation.txt [octets utf-8] « Nantes »; 01_txt_lettre/lettre_reclamation.txt « Nantes »
- `01_txt_lettre` FONCTION (autre) « directrice administrative et financière » — 01_txt_lettre/lettre_reclamation.txt [octets utf-8] « directrice administrative et financière »; 01_txt_lettre/lettre_reclamation.txt « directrice administrative et financière »
- `04_xml_sinistre` ORGANISATION (autre) « Assurances Bellerive » — 04_xml_sinistre/declaration_sinistre.xml (décodé) « Assurances Bellerive »; 04_xml_sinistre/declaration_sinistre.xml [octets latin-1] « Assurances Bellerive »; 04_xml_sinistre/declaration_sinistre.xml [octets utf-8] « Assurances Bellerive »
- `04_xml_sinistre` LIEU (autre) « Brest » — 04_xml_sinistre/declaration_sinistre.xml (balises) « Brest »; 04_xml_sinistre/declaration_sinistre.xml (décodé) « Brest »; 04_xml_sinistre/declaration_sinistre.xml [octets latin-1] « Brest »; 04_xml_sinistre/declaration_sinistre.xml [octets utf-8] « Brest »
- `05_html_equipe` FONCTION (autre) « responsable communication » — 05_html_equipe/equipe.html (balises) « responsable communication »; 05_html_equipe/equipe.html (décodé) « responsable communication »; 05_html_equipe/equipe.html [octets latin-1] « responsable communication »; 05_html_equipe/equipe.html [octets utf-8] « responsable communication »
- `05_html_equipe` FONCTION (autre) « chef de projet » — 05_html_equipe/equipe.html (balises) « chef de projet »; 05_html_equipe/equipe.html (décodé) « chef de projet »; 05_html_equipe/equipe.html [octets latin-1] « chef de projet »; 05_html_equipe/equipe.html [octets utf-8] « chef de projet »
- `06_eml_fil` FONCTION (autre) « responsable RH » — 06_eml_fil/fil_discussion.eml (base64) « responsable RH »; 06_eml_fil/fil_discussion.eml (base64)!word/document.xml (balises) « responsable RH »; 06_eml_fil/fil_discussion.eml (base64)!word/document.xml (décodé) « responsable RH »; 06_eml_fil/fil_discussion.eml (base64)!word/document.xml [octets latin-1] « responsable RH »
- `08_mbox_boite` FONCTION (autre) « avocate » — 08_mbox_boite/boite.mbox [octets latin-1] « avocate »; 08_mbox_boite/boite.mbox [octets utf-8] « avocate »; 08_mbox_boite/boite.mbox#message1#partie0 « avocate »
- `10_docx_audit` FONCTION (autre) « auditeur » — 10_docx_audit/rapport_audit.docx!word/document.xml (balises) « auditeur »; 10_docx_audit/rapport_audit.docx!word/document.xml (décodé) « auditeur »; 10_docx_audit/rapport_audit.docx!word/document.xml [octets latin-1] « auditeur »; 10_docx_audit/rapport_audit.docx!word/document.xml [octets utf-8] « auditeur »
- `12_pptx_projet` FONCTION (autre) « présidente » — 12_pptx_projet/projet_hermine.pptx!ppt/slides/slide1.xml (balises) « présidente »; 12_pptx_projet/projet_hermine.pptx!ppt/slides/slide1.xml (décodé) « présidente »; 12_pptx_projet/projet_hermine.pptx!ppt/slides/slide1.xml [octets utf-8] « présidente »; 12_pptx_projet/projet_hermine.pptx!ppt/slides/slide1.xml « présidente »
- `13_odt_compte_rendu` ORGANISATION (autre) « Association des Riverains du Val-Fleuri » — 13_odt_compte_rendu/compte_rendu_ag.odt!content.xml (balises) « Association des Riverains du Val-Fleuri »; 13_odt_compte_rendu/compte_rendu_ag.odt!content.xml (décodé) « Association des Riverains du Val-Fleuri »; 13_odt_compte_rendu/compte_rendu_ag.odt!content.xml [octets latin-1] « Association des Riverains du Val-Fleuri »; 13_odt_compte_rendu/compte_rendu_ag.odt!content.xml [octets utf-8] « Association des Riverains du Val-Fleuri »
- `13_odt_compte_rendu` FONCTION (autre) « présidente » — 13_odt_compte_rendu/compte_rendu_ag.odt!content.xml (balises) « présidente »; 13_odt_compte_rendu/compte_rendu_ag.odt!content.xml (décodé) « présidente »; 13_odt_compte_rendu/compte_rendu_ag.odt!content.xml [octets utf-8] « présidente »; 13_odt_compte_rendu/compte_rendu_ag.odt!content.xml « présidente »
- `15_rtf_attestation` FONCTION (autre) « ingénieur méthodes » — 15_rtf_attestation/attestation.rtf « ingénieur méthodes »
- `15_rtf_attestation` LIEU (autre) « Rouen » — 15_rtf_attestation/attestation.rtf [octets latin-1] « Rouen »; 15_rtf_attestation/attestation.rtf [octets utf-8] « Rouen »; 15_rtf_attestation/attestation.rtf « Rouen »
- `23_txt_temoins` LIEU (autre) « Lyon » — 23_txt_temoins/notes_internes.txt [octets latin-1] « Lyon »; 23_txt_temoins/notes_internes.txt [octets utf-8] « Lyon »; 23_txt_temoins/notes_internes.txt « Lyon »
- `23_txt_temoins` LIEU (autre) « Paris » — 23_txt_temoins/notes_internes.txt [octets latin-1] « Paris »; 23_txt_temoins/notes_internes.txt [octets utf-8] « Paris »; 23_txt_temoins/notes_internes.txt « Paris »
- `23_txt_temoins` LIEU (autre) « Isère » — 23_txt_temoins/notes_internes.txt [octets utf-8] « Isère »; 23_txt_temoins/notes_internes.txt « Isère »
- `24_txt_anglais` NUMERO_DOSSIER (autre) « INC-2026-0315 » — 24_txt_anglais/incident_report.txt [octets latin-1] « inc20260315 »; 24_txt_anglais/incident_report.txt [octets utf-8] « inc20260315 »; 24_txt_anglais/incident_report.txt « inc20260315 »
- `24_txt_anglais` FONCTION (autre) « site manager » — 24_txt_anglais/incident_report.txt [octets latin-1] « site manager »; 24_txt_anglais/incident_report.txt [octets utf-8] « site manager »; 24_txt_anglais/incident_report.txt « site manager »
- `24_txt_anglais` LIEU (autre) « Bristol » — 24_txt_anglais/incident_report.txt [octets latin-1] « Bristol »; 24_txt_anglais/incident_report.txt [octets utf-8] « Bristol »; 24_txt_anglais/incident_report.txt « Bristol »
- `24_txt_anglais` IDENTIFIANT (direct) « socallaghan » — 24_txt_anglais/incident_report.txt [octets latin-1] « socallaghan »; 24_txt_anglais/incident_report.txt [octets utf-8] « socallaghan »; 24_txt_anglais/incident_report.txt « socallaghan »
- `24_txt_anglais` LIEU (autre) « Berlin » — 24_txt_anglais/incident_report.txt [octets latin-1] « Berlin »; 24_txt_anglais/incident_report.txt [octets utf-8] « Berlin »; 24_txt_anglais/incident_report.txt « Berlin »

### Faux positifs (exemples)

- `01_txt_lettre` (1) : 4 mars 2026
- `03_json_crm` (1) : 2026 02 01
- `06_eml_fil` (7) : 4 mar 2026 | 04 mar 2026 | 3 mars 2026 | 1 mars 2026 | batiment b | 3 mars 2026 | 01 mar 2026
- `07_eml_encode` (1) : 05 mar 2026
- `08_mbox_boite` (4) : 09 mar 2026 | 10 mar 2026 | 12 mars 2026 | 10 mar 2026
- `09_msg_outlook` (1) : 9 mar 2026
- `13_odt_compte_rendu` (1) : 12 mars 2026
- `20_zip_dossier` (1) : 06 mar 2026
- `23_txt_temoins` (2) : 12 mars 2026 | fontaine
- `24_txt_anglais` (1) : 12 march 2026

## Profil standard

- Durée du lot : 35.3 s pour 24 documents
- Rappel identifiants directs : **100.0 %** (322/322)
- Rappel autres types : 94.7 % (54/57)
- Précision (segments remplacés correspondant à une valeur de référence) : 92.4 % (291/315) ; hors dates non liées à une naissance : 97.7 %
- Phrases témoins intactes : 88.4 % (38/43)

| Type | Rappel | Valeurs |
|---|---|---|
| PERSONNE | 100.0 % | 71 |
| EMAIL | 100.0 % | 48 |
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
| LIEU | 91.7 % | 24 |
| FONCTION | 100.0 % | 11 |
| SIREN | 100.0 % | 2 |
| SIRET | 100.0 % | 2 |
| TVA | 100.0 % | 2 |
| URL | 100.0 % | 4 |
| NUMERO_DOSSIER | 66.7 % | 3 |
| GPS | 100.0 % | 1 |

| Format | Rappel direct | Précision | Statuts |
|---|---|---|---|
| 7z | 100.0 % | 100.0 % | Anonymisé ×1 |
| csv | 100.0 % | 100.0 % | Anonymisé ×1 |
| docx | 100.0 % | 100.0 % | Anonymisé ×1 |
| eml | 100.0 % | 87.8 % | Anonymisé ×1, À revoir ×1 |
| html | 100.0 % | 100.0 % | Anonymisé ×1 |
| jpg | 100.0 % | — | À revoir ×1 |
| json | 100.0 % | 95.5 % | Anonymisé ×1 |
| mbox | 100.0 % | 80.0 % | Anonymisé ×1 |
| msg | 100.0 % | 94.4 % | Anonymisé ×1 |
| ods | 100.0 % | 100.0 % | Anonymisé ×1 |
| odt | 100.0 % | 83.3 % | À revoir ×1 |
| pdf | 100.0 % | 100.0 % | Anonymisé ×1, À revoir ×1 |
| png | 100.0 % | — | Anonymisé ×1 |
| pptx | 100.0 % | 90.9 % | Anonymisé ×1 |
| rtf | 100.0 % | 100.0 % | Anonymisé ×1 |
| txt | 100.0 % | 92.9 % | Anonymisé ×4, À revoir ×1 |
| xlsx | 100.0 % | 100.0 % | Anonymisé ×1 |
| xml | 100.0 % | 66.7 % | Anonymisé ×1 |
| zip | 100.0 % | 94.4 % | Anonymisé ×1 |

### Fuites

- `23_txt_temoins` LIEU (autre) « Isère » — 23_txt_temoins/notes_internes.txt [octets utf-8] « Isère »; 23_txt_temoins/notes_internes.txt « Isère »
- `24_txt_anglais` NUMERO_DOSSIER (autre) « INC-2026-0315 » — 24_txt_anglais/incident_report.txt [octets latin-1] « inc20260315 »; 24_txt_anglais/incident_report.txt [octets utf-8] « inc20260315 »; 24_txt_anglais/incident_report.txt « inc20260315 »
- `24_txt_anglais` LIEU (autre) « Berlin » — 24_txt_anglais/incident_report.txt [octets latin-1] « Berlin »; 24_txt_anglais/incident_report.txt [octets utf-8] « Berlin »; 24_txt_anglais/incident_report.txt « Berlin »

### Faux positifs (exemples)

- `01_txt_lettre` (1) : 4 mars 2026
- `03_json_crm` (1) : 2026 02 01
- `04_xml_sinistre` (1) : le boucher du marche couvert
- `06_eml_fil` (8) : 4 mar 2026 | 04 mar 2026 | 3 mars 2026 | 1 mars 2026 | batiment b | 3 mars 2026 | 01 mar 2026 | marchand de journaux
- `07_eml_encode` (1) : 05 mar 2026
- `08_mbox_boite` (4) : 09 mar 2026 | 10 mar 2026 | 12 mars 2026 | 10 mar 2026
- `09_msg_outlook` (1) : 9 mar 2026
- `12_pptx_projet` (1) : projet hermine
- `13_odt_compte_rendu` (2) : 12 mars 2026 | salle bleue
- `20_zip_dossier` (1) : 06 mar 2026
- `23_txt_temoins` (3) : 12 mars 2026 | salle bleue | fontaine
