# Résultats du banc d'essai — baseline

## Profil leger

- Durée du lot : 16.8 s pour 24 documents
- Rappel identifiants directs : **91.0 %** (293/322)
- Rappel autres types : 38.6 % (22/57)
- Précision (segments remplacés correspondant à une valeur de référence) : 94.1 % (304/323) ; hors dates non liées à une naissance : 99.7 %
- Phrases témoins intactes : 93.0 % (40/43)

| Type | Rappel | Valeurs |
|---|---|---|
| PERSONNE | 90.1 % | 71 |
| EMAIL | 100.0 % | 48 |
| TELEPHONE | 92.2 % | 51 |
| IBAN | 68.8 % | 32 |
| CARTE | 100.0 % | 5 |
| NIR | 71.4 % | 7 |
| ADRESSE | 90.0 % | 30 |
| PLAQUE | 100.0 % | 19 |
| IP | 100.0 % | 8 |
| DATE_NAISSANCE | 97.1 % | 34 |
| IDENTIFIANT | 88.2 % | 17 |
| ORGANISATION | 75.0 % | 8 |
| LIEU | 12.5 % | 24 |
| FONCTION | 0.0 % | 11 |
| SIREN | 100.0 % | 2 |
| SIRET | 100.0 % | 2 |
| TVA | 100.0 % | 2 |
| URL | 100.0 % | 4 |
| NUMERO_DOSSIER | 66.7 % | 3 |
| GPS | 100.0 % | 1 |

| Format | Rappel direct | Précision | Statuts |
|---|---|---|---|
| 7z | 100.0 % | 100.0 % | À revoir ×1 |
| csv | 86.9 % | 100.0 % | À revoir ×1 |
| docx | 100.0 % | 100.0 % | À revoir ×1 |
| eml | 96.6 % | 90.8 % | Anonymisé ×1, Résidus ×1 |
| html | 100.0 % | 100.0 % | Anonymisé ×1 |
| jpg | 100.0 % | — | Anonymisé ×1 |
| json | 83.3 % | 94.7 % | Résidus ×1 |
| mbox | 100.0 % | 82.6 % | À revoir ×1 |
| msg | 100.0 % | 94.4 % | Anonymisé ×1 |
| ods | 100.0 % | 100.0 % | À revoir ×1 |
| odt | 100.0 % | 90.0 % | Anonymisé ×1 |
| pdf | 81.2 % | 100.0 % | Anonymisé ×2 |
| png | 100.0 % | — | Anonymisé ×1 |
| pptx | 100.0 % | 100.0 % | Anonymisé ×1 |
| rtf | 75.0 % | 100.0 % | Anonymisé ×1 |
| txt | 85.0 % | 92.2 % | Anonymisé ×4, À revoir ×1 |
| xlsx | 89.5 % | 100.0 % | À revoir ×1 |
| xml | 100.0 % | 100.0 % | Anonymisé ×1 |
| zip | 90.9 % | 93.8 % | Anonymisé ×1 |

### Fuites

- `01_txt_lettre` ADRESSE (direct) « Bât. C, appartement 34 » — 01_txt_lettre/lettre_reclamation.txt [octets utf-8] « Bât. C, appartement 34 »; 01_txt_lettre/lettre_reclamation.txt « Bât. C, appartement 34 »
- `01_txt_lettre` LIEU (autre) « Nantes » — 01_txt_lettre/lettre_reclamation.txt [octets latin-1] « Nantes »; 01_txt_lettre/lettre_reclamation.txt [octets utf-8] « Nantes »; 01_txt_lettre/lettre_reclamation.txt « Nantes »
- `01_txt_lettre` FONCTION (autre) « directrice administrative et financière » — 01_txt_lettre/lettre_reclamation.txt [octets utf-8] « directrice administrative et financière »; 01_txt_lettre/lettre_reclamation.txt « directrice administrative et financière »
- `01_txt_lettre` IBAN (direct) « FR7685952778000487894129335 » — 01_txt_lettre/lettre_reclamation.txt [octets latin-1] « 85952778000487894129335 »; 01_txt_lettre/lettre_reclamation.txt [octets latin-1] « fr7685952778000487894129335 »; 01_txt_lettre/lettre_reclamation.txt [octets utf-8] « 85952778000487894129335 »; 01_txt_lettre/lettre_reclamation.txt [octets utf-8] « fr7685952778000487894129335 »
- `02_csv_clients` LIEU (autre) « Le Mans » — 02_csv_clients/clients.csv [octets latin-1] « Le Mans »; 02_csv_clients/clients.csv [octets utf-8] « Le Mans »; 02_csv_clients/clients.csv « Le Mans »
- `02_csv_clients` LIEU (autre) « La Rochelle » — 02_csv_clients/clients.csv [octets latin-1] « La Rochelle »; 02_csv_clients/clients.csv [octets utf-8] « La Rochelle »; 02_csv_clients/clients.csv « La Rochelle »
- `02_csv_clients` PERSONNE (direct) « Margaux Lebrun » — 02_csv_clients/clients.csv [octets latin-1] « Lebrun Margaux »; 02_csv_clients/clients.csv [octets latin-1] « Lebrun »; 02_csv_clients/clients.csv [octets utf-8] « Lebrun Margaux »; 02_csv_clients/clients.csv [octets utf-8] « Lebrun »
- `02_csv_clients` IBAN (direct) « FR7646582336560570485616579 » — 02_csv_clients/clients.csv [octets latin-1] « 46582336560570485616579 »; 02_csv_clients/clients.csv [octets latin-1] « fr7646582336560570485616579 »; 02_csv_clients/clients.csv [octets utf-8] « 46582336560570485616579 »; 02_csv_clients/clients.csv [octets utf-8] « fr7646582336560570485616579 »
- `02_csv_clients` LIEU (autre) « Lorient » — 02_csv_clients/clients.csv [octets latin-1] « Lorient »; 02_csv_clients/clients.csv [octets utf-8] « Lorient »; 02_csv_clients/clients.csv « Lorient »
- `02_csv_clients` LIEU (autre) « Pau » — 02_csv_clients/clients.csv [octets latin-1] « Pau »; 02_csv_clients/clients.csv [octets utf-8] « Pau »; 02_csv_clients/clients.csv « Pau »
- `02_csv_clients` IBAN (direct) « FR7619631901405427723107028 » — 02_csv_clients/clients.csv [octets latin-1] « 19631901405427723107028 »; 02_csv_clients/clients.csv [octets latin-1] « fr7619631901405427723107028 »; 02_csv_clients/clients.csv [octets utf-8] « 19631901405427723107028 »; 02_csv_clients/clients.csv [octets utf-8] « fr7619631901405427723107028 »
- `02_csv_clients` LIEU (autre) « Perpignan » — 02_csv_clients/clients.csv [octets latin-1] « Perpignan »; 02_csv_clients/clients.csv [octets utf-8] « Perpignan »; 02_csv_clients/clients.csv « Perpignan »
- `02_csv_clients` PERSONNE (direct) « Corentin Poirier » — 02_csv_clients/clients.csv [octets latin-1] « Poirier Corentin »; 02_csv_clients/clients.csv [octets latin-1] « Poirier »; 02_csv_clients/clients.csv [octets utf-8] « Poirier Corentin »; 02_csv_clients/clients.csv [octets utf-8] « Poirier »
- `02_csv_clients` IBAN (direct) « FR7601661181926675888847967 » — 02_csv_clients/clients.csv [octets latin-1] « 01661181926675888847967 »; 02_csv_clients/clients.csv [octets latin-1] « fr7601661181926675888847967 »; 02_csv_clients/clients.csv [octets utf-8] « 01661181926675888847967 »; 02_csv_clients/clients.csv [octets utf-8] « fr7601661181926675888847967 »
- `02_csv_clients` LIEU (autre) « Mulhouse » — 02_csv_clients/clients.csv [octets latin-1] « Mulhouse »; 02_csv_clients/clients.csv [octets utf-8] « Mulhouse »; 02_csv_clients/clients.csv « Mulhouse »
- `02_csv_clients` LIEU (autre) « Montluçon » — 02_csv_clients/clients.csv [octets utf-8] « Montluçon »; 02_csv_clients/clients.csv « Montluçon »
- `02_csv_clients` TELEPHONE (direct) « +33639985508 » — 02_csv_clients/clients.csv [octets latin-1] « 639985508 »; 02_csv_clients/clients.csv [octets utf-8] « 639985508 »; 02_csv_clients/clients.csv « 639985508 »
- `02_csv_clients` LIEU (autre) « Blois » — 02_csv_clients/clients.csv [octets latin-1] « Blois »; 02_csv_clients/clients.csv [octets utf-8] « Blois »; 02_csv_clients/clients.csv « Blois »
- `02_csv_clients` PERSONNE (direct) « Clémence Hamelin » — 02_csv_clients/clients.csv [octets latin-1] « Hamelin »; 02_csv_clients/clients.csv [octets utf-8] « Hamelin Clémence »; 02_csv_clients/clients.csv [octets utf-8] « Hamelin »; 02_csv_clients/clients.csv « Hamelin Clémence »
- `02_csv_clients` IBAN (direct) « FR7637831748521431842211370 » — 02_csv_clients/clients.csv [octets latin-1] « 37831748521431842211370 »; 02_csv_clients/clients.csv [octets latin-1] « fr7637831748521431842211370 »; 02_csv_clients/clients.csv [octets utf-8] « 37831748521431842211370 »; 02_csv_clients/clients.csv [octets utf-8] « fr7637831748521431842211370 »
- `02_csv_clients` LIEU (autre) « Arras » — 02_csv_clients/clients.csv [octets latin-1] « Arras »; 02_csv_clients/clients.csv [octets utf-8] « Arras »; 02_csv_clients/clients.csv « Arras »
- `02_csv_clients` LIEU (autre) « Angoulême » — 02_csv_clients/clients.csv [octets utf-8] « Angoulême »; 02_csv_clients/clients.csv « Angoulême »
- `02_csv_clients` IBAN (direct) « FR7697836437037575871881551 » — 02_csv_clients/clients.csv [octets latin-1] « 97836437037575871881551 »; 02_csv_clients/clients.csv [octets latin-1] « fr7697836437037575871881551 »; 02_csv_clients/clients.csv [octets utf-8] « 97836437037575871881551 »; 02_csv_clients/clients.csv [octets utf-8] « fr7697836437037575871881551 »
- `02_csv_clients` LIEU (autre) « Chalon-sur-Saône » — 02_csv_clients/clients.csv [octets utf-8] « Chalon-sur-Saône »; 02_csv_clients/clients.csv « Chalon-sur-Saône »
- `02_csv_clients` PERSONNE (direct) « Valentin Lemaire » — 02_csv_clients/clients.csv [octets latin-1] « Lemaire Valentin »; 02_csv_clients/clients.csv [octets latin-1] « Lemaire »; 02_csv_clients/clients.csv [octets utf-8] « Lemaire Valentin »; 02_csv_clients/clients.csv [octets utf-8] « Lemaire »
- `02_csv_clients` IBAN (direct) « FR7671641130216729155719955 » — 02_csv_clients/clients.csv [octets latin-1] « 71641130216729155719955 »; 02_csv_clients/clients.csv [octets latin-1] « fr7671641130216729155719955 »; 02_csv_clients/clients.csv [octets utf-8] « 71641130216729155719955 »; 02_csv_clients/clients.csv [octets utf-8] « fr7671641130216729155719955 »
- `02_csv_clients` LIEU (autre) « Bourges » — 02_csv_clients/clients.csv [octets latin-1] « Bourges »; 02_csv_clients/clients.csv [octets utf-8] « Bourges »; 02_csv_clients/clients.csv « Bourges »
- `03_json_crm` PERSONNE (direct) « Jean-Baptiste Kowalczyk » — 03_json_crm/crm_export.json (quarantaine) (brut) « Kowalczyk »; 03_json_crm/crm_export.json (quarantaine) [octets latin-1] « Kowalczyk »; 03_json_crm/crm_export.json (quarantaine) [octets utf-8] « Kowalczyk »; 03_json_crm/crm_export.json (quarantaine) « Kowalczyk »
- `03_json_crm` IDENTIFIANT (direct) « jbkowal88 » — 03_json_crm/crm_export.json (quarantaine) (brut) « jbkowal88 »; 03_json_crm/crm_export.json (quarantaine) [octets latin-1] « jbkowal88 »; 03_json_crm/crm_export.json (quarantaine) [octets utf-8] « jbkowal88 »; 03_json_crm/crm_export.json (quarantaine) « jbkowal88 »
- `03_json_crm` LIEU (autre) « Metz » — 03_json_crm/crm_export.json (quarantaine) (brut) « Metz »; 03_json_crm/crm_export.json (quarantaine) [octets latin-1] « Metz »; 03_json_crm/crm_export.json (quarantaine) [octets utf-8] « Metz »; 03_json_crm/crm_export.json (quarantaine) « Metz »
- `03_json_crm` TELEPHONE (direct) « +33639984697 » — 03_json_crm/crm_export.json (quarantaine) (brut) « 639984697 »; 03_json_crm/crm_export.json (quarantaine) [octets latin-1] « 639984697 »; 03_json_crm/crm_export.json (quarantaine) [octets utf-8] « 639984697 »; 03_json_crm/crm_export.json (quarantaine) « 639984697 »
- `03_json_crm` FONCTION (autre) « pharmacienne » — 03_json_crm/crm_export.json (quarantaine) (brut) « pharmacienne »; 03_json_crm/crm_export.json (quarantaine) [octets latin-1] « pharmacienne »; 03_json_crm/crm_export.json (quarantaine) [octets utf-8] « pharmacienne »; 03_json_crm/crm_export.json (quarantaine) « pharmacienne »
- `04_xml_sinistre` ORGANISATION (autre) « Assurances Bellerive » — 04_xml_sinistre/declaration_sinistre.xml (décodé) « Assurances Bellerive »; 04_xml_sinistre/declaration_sinistre.xml [octets latin-1] « Assurances Bellerive »; 04_xml_sinistre/declaration_sinistre.xml [octets utf-8] « Assurances Bellerive »
- `04_xml_sinistre` LIEU (autre) « Brest » — 04_xml_sinistre/declaration_sinistre.xml (balises) « Brest »; 04_xml_sinistre/declaration_sinistre.xml (décodé) « Brest »; 04_xml_sinistre/declaration_sinistre.xml [octets latin-1] « Brest »; 04_xml_sinistre/declaration_sinistre.xml [octets utf-8] « Brest »
- `05_html_equipe` FONCTION (autre) « responsable communication » — 05_html_equipe/equipe.html (balises) « responsable communication »; 05_html_equipe/equipe.html (décodé) « responsable communication »; 05_html_equipe/equipe.html [octets latin-1] « responsable communication »; 05_html_equipe/equipe.html [octets utf-8] « responsable communication »
- `05_html_equipe` FONCTION (autre) « chef de projet » — 05_html_equipe/equipe.html (balises) « chef de projet »; 05_html_equipe/equipe.html (décodé) « chef de projet »; 05_html_equipe/equipe.html [octets latin-1] « chef de projet »; 05_html_equipe/equipe.html [octets utf-8] « chef de projet »
- `06_eml_fil` FONCTION (autre) « responsable RH » — 06_eml_fil/fil_discussion.eml (quarantaine) (base64) « responsable RH »; 06_eml_fil/fil_discussion.eml (quarantaine) (base64)!word/document.xml (balises) « responsable RH »; 06_eml_fil/fil_discussion.eml (quarantaine) (base64)!word/document.xml (décodé) « responsable RH »; 06_eml_fil/fil_discussion.eml (quarantaine) (base64)!word/document.xml [octets latin-1] « responsable RH »
- `06_eml_fil` PERSONNE (direct) « J.-B. K. » — 06_eml_fil/fil_discussion.eml (quarantaine) [octets latin-1] « J.-B. K. »; 06_eml_fil/fil_discussion.eml (quarantaine) [octets utf-8] « J.-B. K. »; 06_eml_fil/fil_discussion.eml (quarantaine)#partie2 « J.-B. K. »
- `08_mbox_boite` FONCTION (autre) « avocate » — 08_mbox_boite/boite.mbox [octets latin-1] « avocate »; 08_mbox_boite/boite.mbox [octets utf-8] « avocate »; 08_mbox_boite/boite.mbox#message1#partie0 « avocate »
- `10_docx_audit` FONCTION (autre) « auditeur » — 10_docx_audit/rapport_audit.docx!word/document.xml (balises) « auditeur »; 10_docx_audit/rapport_audit.docx!word/document.xml (décodé) « auditeur »; 10_docx_audit/rapport_audit.docx!word/document.xml [octets latin-1] « auditeur »; 10_docx_audit/rapport_audit.docx!word/document.xml [octets utf-8] « auditeur »
- `11_xlsx_registre` IBAN (direct) « FR7633794677577299956109875 » — 11_xlsx_registre/registre_personnel.xlsx!xl/sharedStrings.xml (balises) « 33794677577299956109875 »; 11_xlsx_registre/registre_personnel.xlsx!xl/sharedStrings.xml (balises) « fr7633794677577299956109875 »; 11_xlsx_registre/registre_personnel.xlsx!xl/sharedStrings.xml (décodé) « 33794677577299956109875 »; 11_xlsx_registre/registre_personnel.xlsx!xl/sharedStrings.xml (décodé) « fr7633794677577299956109875 »
- `11_xlsx_registre` TELEPHONE (direct) « +33639982412 » — 11_xlsx_registre/registre_personnel.xlsx!xl/worksheets/sheet1.xml (balises) « 639982412 »; 11_xlsx_registre/registre_personnel.xlsx!xl/worksheets/sheet1.xml (décodé) « 639982412 »; 11_xlsx_registre/registre_personnel.xlsx!xl/worksheets/sheet1.xml [octets latin-1] « 639982412 »; 11_xlsx_registre/registre_personnel.xlsx!xl/worksheets/sheet1.xml [octets utf-8] « 639982412 »
- `12_pptx_projet` FONCTION (autre) « présidente » — 12_pptx_projet/projet_hermine.pptx!ppt/slides/slide1.xml (balises) « présidente »; 12_pptx_projet/projet_hermine.pptx!ppt/slides/slide1.xml (décodé) « présidente »; 12_pptx_projet/projet_hermine.pptx!ppt/slides/slide1.xml [octets utf-8] « présidente »; 12_pptx_projet/projet_hermine.pptx!ppt/slides/slide1.xml « présidente »
- `13_odt_compte_rendu` ORGANISATION (autre) « Association des Riverains du Val-Fleuri » — 13_odt_compte_rendu/compte_rendu_ag.odt!content.xml (balises) « Association des Riverains du Val-Fleuri »; 13_odt_compte_rendu/compte_rendu_ag.odt!content.xml (décodé) « Association des Riverains du Val-Fleuri »; 13_odt_compte_rendu/compte_rendu_ag.odt!content.xml [octets latin-1] « Association des Riverains du Val-Fleuri »; 13_odt_compte_rendu/compte_rendu_ag.odt!content.xml [octets utf-8] « Association des Riverains du Val-Fleuri »
- `13_odt_compte_rendu` FONCTION (autre) « présidente » — 13_odt_compte_rendu/compte_rendu_ag.odt!content.xml (balises) « présidente »; 13_odt_compte_rendu/compte_rendu_ag.odt!content.xml (décodé) « présidente »; 13_odt_compte_rendu/compte_rendu_ag.odt!content.xml [octets utf-8] « présidente »; 13_odt_compte_rendu/compte_rendu_ag.odt!content.xml « présidente »
- `15_rtf_attestation` NIR (direct) « 281101374236779 » — 15_rtf_attestation/attestation.rtf [octets latin-1] « 2811013742367 »; 15_rtf_attestation/attestation.rtf [octets latin-1] « 281101374236779 »; 15_rtf_attestation/attestation.rtf [octets utf-8] « 2811013742367 »; 15_rtf_attestation/attestation.rtf [octets utf-8] « 281101374236779 »
- `15_rtf_attestation` PERSONNE (direct) « F.D.V. » — 15_rtf_attestation/attestation.rtf [octets latin-1] « F.D.V. »; 15_rtf_attestation/attestation.rtf [octets utf-8] « F.D.V. »; 15_rtf_attestation/attestation.rtf « F.D.V. »
- `15_rtf_attestation` FONCTION (autre) « ingénieur méthodes » — 15_rtf_attestation/attestation.rtf « ingénieur méthodes »
- `15_rtf_attestation` LIEU (autre) « Rouen » — 15_rtf_attestation/attestation.rtf [octets latin-1] « Rouen »; 15_rtf_attestation/attestation.rtf [octets utf-8] « Rouen »; 15_rtf_attestation/attestation.rtf « Rouen »
- `16_pdf_releve` ADRESSE (direct) « 8 quai des Mariniers-d'Antan, 33000 Bordeaux » — 16_pdf_releve/releve_identite.pdf (page 1) « 8 quai des Mariniers-d'Antan »; 16_pdf_releve/releve_identite.pdf (page 1) « quai des Mariniers-d'Antan »
- `17_pdf_numerise` NIR (direct) « 159013717447383 » — image page1 (corrélation 1.00)
- `17_pdf_numerise` IBAN (direct) « FR7653832419785025088366803 » — image page1 (corrélation 1.00)
- `20_zip_dossier` IBAN (direct) « FR7687573018181263165969491 » — 20_zip_dossier/dossier_complet.zip!Dossier [PERSONNE_014]/courriels/echange_[IDENTIFIANT_002].eml (base64)!RIB_[PERSONNE_014].txt [octets latin-1] « 87573018181263165969491 »; 20_zip_dossier/dossier_complet.zip!Dossier [PERSONNE_014]/courriels/echange_[IDENTIFIANT_002].eml (base64)!RIB_[PERSONNE_014].txt [octets latin-1] « fr7687573018181263165969491 »; 20_zip_dossier/dossier_complet.zip!Dossier [PERSONNE_014]/courriels/echange_[IDENTIFIANT_002].eml (base64)!RIB_[PERSONNE_014].txt [octets utf-8] « 87573018181263165969491 »; 20_zip_dossier/dossier_complet.zip!Dossier [PERSONNE_014]/courriels/echange_[IDENTIFIANT_002].eml (base64)!RIB_[PERSONNE_014].txt [octets utf-8] « fr7687573018181263165969491 »
- `23_txt_temoins` LIEU (autre) « Lyon » — 23_txt_temoins/notes_internes.txt [octets latin-1] « Lyon »; 23_txt_temoins/notes_internes.txt [octets utf-8] « Lyon »; 23_txt_temoins/notes_internes.txt « Lyon »
- `23_txt_temoins` LIEU (autre) « Paris » — 23_txt_temoins/notes_internes.txt [octets latin-1] « Paris »; 23_txt_temoins/notes_internes.txt [octets utf-8] « Paris »; 23_txt_temoins/notes_internes.txt « Paris »
- `23_txt_temoins` LIEU (autre) « Isère » — 23_txt_temoins/notes_internes.txt [octets utf-8] « Isère »; 23_txt_temoins/notes_internes.txt « Isère »
- `24_txt_anglais` NUMERO_DOSSIER (autre) « INC-2026-0315 » — 24_txt_anglais/incident_report.txt [octets latin-1] « inc20260315 »; 24_txt_anglais/incident_report.txt [octets utf-8] « inc20260315 »; 24_txt_anglais/incident_report.txt « inc20260315 »
- `24_txt_anglais` FONCTION (autre) « site manager » — 24_txt_anglais/incident_report.txt [octets latin-1] « site manager »; 24_txt_anglais/incident_report.txt [octets utf-8] « site manager »; 24_txt_anglais/incident_report.txt « site manager »
- `24_txt_anglais` TELEPHONE (direct) « +447700900456 » — 24_txt_anglais/incident_report.txt [octets latin-1] « 7700900456 »; 24_txt_anglais/incident_report.txt [octets utf-8] « 7700900456 »; 24_txt_anglais/incident_report.txt « 7700900456 »
- `24_txt_anglais` DATE_NAISSANCE (direct) « 1985-03-14 » — 24_txt_anglais/incident_report.txt [octets latin-1] « 03/14/1985 »; 24_txt_anglais/incident_report.txt [octets utf-8] « 03/14/1985 »; 24_txt_anglais/incident_report.txt « 03/14/1985 »
- `24_txt_anglais` ADRESSE (direct) « 221 Larkspur Crescent, BS1 4XE Bristol » — 24_txt_anglais/incident_report.txt [octets latin-1] « 221 Larkspur Crescent »; 24_txt_anglais/incident_report.txt [octets latin-1] « Larkspur Crescent »; 24_txt_anglais/incident_report.txt [octets utf-8] « 221 Larkspur Crescent »; 24_txt_anglais/incident_report.txt [octets utf-8] « Larkspur Crescent »
- `24_txt_anglais` LIEU (autre) « Bristol » — 24_txt_anglais/incident_report.txt [octets latin-1] « Bristol »; 24_txt_anglais/incident_report.txt [octets utf-8] « Bristol »; 24_txt_anglais/incident_report.txt « Bristol »
- `24_txt_anglais` IDENTIFIANT (direct) « socallaghan » — 24_txt_anglais/incident_report.txt [octets latin-1] « socallaghan »; 24_txt_anglais/incident_report.txt [octets utf-8] « socallaghan »; 24_txt_anglais/incident_report.txt « socallaghan »
- `24_txt_anglais` LIEU (autre) « Berlin » — 24_txt_anglais/incident_report.txt [octets latin-1] « Berlin »; 24_txt_anglais/incident_report.txt [octets utf-8] « Berlin »; 24_txt_anglais/incident_report.txt « Berlin »

### Faux positifs (exemples)

- `01_txt_lettre` (1) : 4 mars 2026
- `03_json_crm` (1) : 2026 02 01
- `06_eml_fil` (6) : 4 mar 2026 | 04 mar 2026 | 3 mars 2026 | 1 mars 2026 | 3 mars 2026 | 01 mar 2026
- `07_eml_encode` (1) : 05 mar 2026
- `08_mbox_boite` (4) : 09 mar 2026 | 10 mar 2026 | 12 mars 2026 | 10 mar 2026
- `09_msg_outlook` (1) : 9 mar 2026
- `13_odt_compte_rendu` (1) : 12 mars 2026
- `20_zip_dossier` (1) : 06 mar 2026
- `23_txt_temoins` (2) : 12 mars 2026 | fontaine
- `24_txt_anglais` (1) : 12 march 2026

## Profil standard

- Durée du lot : 49.9 s pour 24 documents
- Rappel identifiants directs : **94.4 %** (304/322)
- Rappel autres types : 93.0 % (53/57)
- Précision (segments remplacés correspondant à une valeur de référence) : 87.0 % (289/332) ; hors dates non liées à une naissance : 91.7 %
- Phrases témoins intactes : 44.2 % (19/43)

| Type | Rappel | Valeurs |
|---|---|---|
| PERSONNE | 95.8 % | 71 |
| EMAIL | 100.0 % | 48 |
| TELEPHONE | 92.2 % | 51 |
| IBAN | 78.1 % | 32 |
| CARTE | 100.0 % | 5 |
| NIR | 71.4 % | 7 |
| ADRESSE | 96.7 % | 30 |
| PLAQUE | 100.0 % | 19 |
| IP | 100.0 % | 8 |
| DATE_NAISSANCE | 100.0 % | 34 |
| IDENTIFIANT | 94.1 % | 17 |
| ORGANISATION | 100.0 % | 8 |
| LIEU | 91.7 % | 24 |
| FONCTION | 90.9 % | 11 |
| SIREN | 100.0 % | 2 |
| SIRET | 100.0 % | 2 |
| TVA | 100.0 % | 2 |
| URL | 100.0 % | 4 |
| NUMERO_DOSSIER | 66.7 % | 3 |
| GPS | 100.0 % | 1 |

| Format | Rappel direct | Précision | Statuts |
|---|---|---|---|
| 7z | 100.0 % | 83.3 % | À revoir ×1 |
| csv | 90.5 % | 97.1 % | À revoir ×1 |
| docx | 100.0 % | 93.3 % | Anonymisé ×1 |
| eml | 100.0 % | 87.8 % | Anonymisé ×1, À revoir ×1 |
| html | 100.0 % | 90.9 % | Anonymisé ×1 |
| jpg | 100.0 % | — | À revoir ×1 |
| json | 88.9 % | 95.0 % | Anonymisé ×1 |
| mbox | 100.0 % | 72.7 % | À revoir ×1 |
| msg | 100.0 % | 85.0 % | Résidus ×1 |
| ods | 100.0 % | 100.0 % | Anonymisé ×1 |
| odt | 100.0 % | 76.9 % | À revoir ×1 |
| pdf | 87.5 % | 100.0 % | Anonymisé ×1, À revoir ×1 |
| png | 100.0 % | — | Anonymisé ×1 |
| pptx | 100.0 % | 90.9 % | À revoir ×1 |
| rtf | 87.5 % | 85.7 % | À revoir ×1 |
| txt | 92.5 % | 79.7 % | Anonymisé ×1, À revoir ×4 |
| xlsx | 89.5 % | 100.0 % | À revoir ×1 |
| xml | 100.0 % | 66.7 % | Anonymisé ×1 |
| zip | 100.0 % | 94.4 % | Anonymisé ×1 |

### Fuites

- `01_txt_lettre` ADRESSE (direct) « Bât. C, appartement 34 » — 01_txt_lettre/lettre_reclamation.txt [octets utf-8] « Bât. C, appartement 34 »; 01_txt_lettre/lettre_reclamation.txt « Bât. C, appartement 34 »
- `01_txt_lettre` IBAN (direct) « FR7685952778000487894129335 » — 01_txt_lettre/lettre_reclamation.txt [octets latin-1] « 85952778000487894129335 »; 01_txt_lettre/lettre_reclamation.txt [octets latin-1] « fr7685952778000487894129335 »; 01_txt_lettre/lettre_reclamation.txt [octets utf-8] « 85952778000487894129335 »; 01_txt_lettre/lettre_reclamation.txt [octets utf-8] « fr7685952778000487894129335 »
- `02_csv_clients` PERSONNE (direct) « Margaux Lebrun » — 02_csv_clients/clients.csv [octets latin-1] « Lebrun »; 02_csv_clients/clients.csv [octets utf-8] « Lebrun »; 02_csv_clients/clients.csv « Lebrun »
- `02_csv_clients` IBAN (direct) « FR7619631901405427723107028 » — 02_csv_clients/clients.csv [octets latin-1] « 19631901405427723107028 »; 02_csv_clients/clients.csv [octets latin-1] « fr7619631901405427723107028 »; 02_csv_clients/clients.csv [octets utf-8] « 19631901405427723107028 »; 02_csv_clients/clients.csv [octets utf-8] « fr7619631901405427723107028 »
- `02_csv_clients` PERSONNE (direct) « Corentin Poirier » — 02_csv_clients/clients.csv [octets latin-1] « Poirier »; 02_csv_clients/clients.csv [octets utf-8] « Poirier »; 02_csv_clients/clients.csv « Poirier »
- `02_csv_clients` IBAN (direct) « FR7601661181926675888847967 » — 02_csv_clients/clients.csv [octets latin-1] « 01661181926675888847967 »; 02_csv_clients/clients.csv [octets latin-1] « fr7601661181926675888847967 »; 02_csv_clients/clients.csv [octets utf-8] « 01661181926675888847967 »; 02_csv_clients/clients.csv [octets utf-8] « fr7601661181926675888847967 »
- `02_csv_clients` TELEPHONE (direct) « +33639985508 » — 02_csv_clients/clients.csv [octets latin-1] « 639985508 »; 02_csv_clients/clients.csv [octets utf-8] « 639985508 »; 02_csv_clients/clients.csv « 639985508 »
- `02_csv_clients` IBAN (direct) « FR7697836437037575871881551 » — 02_csv_clients/clients.csv [octets latin-1] « 97836437037575871881551 »; 02_csv_clients/clients.csv [octets latin-1] « fr7697836437037575871881551 »; 02_csv_clients/clients.csv [octets utf-8] « 97836437037575871881551 »; 02_csv_clients/clients.csv [octets utf-8] « fr7697836437037575871881551 »
- `02_csv_clients` PERSONNE (direct) « Valentin Lemaire » — 02_csv_clients/clients.csv [octets latin-1] « Lemaire »; 02_csv_clients/clients.csv [octets utf-8] « Lemaire »; 02_csv_clients/clients.csv « Lemaire »
- `02_csv_clients` IBAN (direct) « FR7671641130216729155719955 » — 02_csv_clients/clients.csv [octets latin-1] « 71641130216729155719955 »; 02_csv_clients/clients.csv [octets latin-1] « fr7671641130216729155719955 »; 02_csv_clients/clients.csv [octets utf-8] « 71641130216729155719955 »; 02_csv_clients/clients.csv [octets utf-8] « fr7671641130216729155719955 »
- `03_json_crm` IDENTIFIANT (direct) « jbkowal88 » — 03_json_crm/crm_export.json (brut) « jbkowal88 »; 03_json_crm/crm_export.json [octets latin-1] « jbkowal88 »; 03_json_crm/crm_export.json [octets utf-8] « jbkowal88 »; 03_json_crm/crm_export.json « jbkowal88 »
- `03_json_crm` TELEPHONE (direct) « +33639984697 » — 03_json_crm/crm_export.json (brut) « 639984697 »; 03_json_crm/crm_export.json [octets latin-1] « 639984697 »; 03_json_crm/crm_export.json [octets utf-8] « 639984697 »; 03_json_crm/crm_export.json « 639984697 »
- `03_json_crm` FONCTION (autre) « pharmacienne » — 03_json_crm/crm_export.json (brut) « pharmacienne »; 03_json_crm/crm_export.json [octets latin-1] « pharmacienne »; 03_json_crm/crm_export.json [octets utf-8] « pharmacienne »; 03_json_crm/crm_export.json « pharmacienne »
- `11_xlsx_registre` IBAN (direct) « FR7633794677577299956109875 » — 11_xlsx_registre/registre_personnel.xlsx!xl/sharedStrings.xml (balises) « 33794677577299956109875 »; 11_xlsx_registre/registre_personnel.xlsx!xl/sharedStrings.xml (balises) « fr7633794677577299956109875 »; 11_xlsx_registre/registre_personnel.xlsx!xl/sharedStrings.xml (décodé) « 33794677577299956109875 »; 11_xlsx_registre/registre_personnel.xlsx!xl/sharedStrings.xml (décodé) « fr7633794677577299956109875 »
- `11_xlsx_registre` TELEPHONE (direct) « +33639982412 » — 11_xlsx_registre/registre_personnel.xlsx!xl/worksheets/sheet1.xml (balises) « 639982412 »; 11_xlsx_registre/registre_personnel.xlsx!xl/worksheets/sheet1.xml (décodé) « 639982412 »; 11_xlsx_registre/registre_personnel.xlsx!xl/worksheets/sheet1.xml [octets latin-1] « 639982412 »; 11_xlsx_registre/registre_personnel.xlsx!xl/worksheets/sheet1.xml [octets utf-8] « 639982412 »
- `15_rtf_attestation` NIR (direct) « 281101374236779 » — 15_rtf_attestation/attestation.rtf [octets latin-1] « 2811013742367 »; 15_rtf_attestation/attestation.rtf [octets latin-1] « 281101374236779 »; 15_rtf_attestation/attestation.rtf [octets utf-8] « 2811013742367 »; 15_rtf_attestation/attestation.rtf [octets utf-8] « 281101374236779 »
- `17_pdf_numerise` NIR (direct) « 159013717447383 » — image page1 (corrélation 1.00)
- `17_pdf_numerise` IBAN (direct) « FR7653832419785025088366803 » — image page1 (corrélation 1.00)
- `23_txt_temoins` LIEU (autre) « Isère » — 23_txt_temoins/notes_internes.txt [octets utf-8] « Isère »; 23_txt_temoins/notes_internes.txt « Isère »
- `24_txt_anglais` NUMERO_DOSSIER (autre) « INC-2026-0315 » — 24_txt_anglais/incident_report.txt [octets latin-1] « inc20260315 »; 24_txt_anglais/incident_report.txt [octets utf-8] « inc20260315 »; 24_txt_anglais/incident_report.txt « inc20260315 »
- `24_txt_anglais` TELEPHONE (direct) « +447700900456 » — 24_txt_anglais/incident_report.txt [octets latin-1] « 7700900456 »; 24_txt_anglais/incident_report.txt [octets utf-8] « 7700900456 »; 24_txt_anglais/incident_report.txt « 7700900456 »
- `24_txt_anglais` LIEU (autre) « Berlin » — 24_txt_anglais/incident_report.txt [octets latin-1] « Berlin »; 24_txt_anglais/incident_report.txt [octets utf-8] « Berlin »; 24_txt_anglais/incident_report.txt « Berlin »

### Faux positifs (exemples)

- `01_txt_lettre` (4) : 4 mars 2026 | monsieur | comite de direction | monsieur
- `02_csv_clients` (1) : salle du rez de chaussee
- `03_json_crm` (1) : 2026 02 01
- `04_xml_sinistre` (1) : le boucher du marche couvert
- `05_html_equipe` (1) : salle du rez de chaussee
- `06_eml_fil` (8) : 4 mar 2026 | 04 mar 2026 | 3 mars 2026 | bonjour | 1 mars 2026 | 3 mars 2026 | 01 mar 2026 | marchand de journaux
- `07_eml_encode` (1) : 05 mar 2026
- `08_mbox_boite` (6) : 09 mar 2026 | 10 mar 2026 | le comite | 12 mars 2026 | salle bleue | 10 mar 2026
- `09_msg_outlook` (3) : 9 mar 2026 | comite de direction | bonjour
- `10_docx_audit` (1) : le boucher du marche couvert
- `12_pptx_projet` (1) : projet hermine
- `13_odt_compte_rendu` (3) : le comite | 12 mars 2026 | salle bleue
- `15_rtf_attestation` (1) : salle du rez de chaussee
- `20_zip_dossier` (1) : 06 mar 2026
- `21_7z_sauvegarde` (1) : comite de direction
- `23_txt_temoins` (9) : comite de direction | salle du rez de chaussee un martin pecheur | boucher du marche couvert | marchand de journaux | le comite | 12 mars 2026 | salle bleue | fontaine
