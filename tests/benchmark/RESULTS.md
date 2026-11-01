# Banc d'essai d'anonymisation — résultats

Évaluation indépendante d'Anonyma (phase 3) : à quel point les données personnelles
disparaissent-elles réellement des fichiers produits ? Mesure de référence (avant toute
modification), corrections, mesure finale.

**En bref** (corpus synthétique de 24 documents, 19 formats, 379 valeurs sensibles dont
322 identifiants directs) :

| Profil | Rappel identifiants directs | Précision | Phrases témoins intactes |
|---|---|---|---|
| `leger` — référence | 91,0 % (293/322) | 94,1 % | 93,0 % |
| `leger` — final | **99,7 %** (321/322) | 93,5 % | 93,0 % |
| `standard` — référence | 94,4 % (304/322) | 87,0 % | 44,2 % |
| `standard` — final | **100,0 %** (322/322) | 92,4 % | 88,4 % |

Depuis, le **correctif global n°1** (incident du PDF signé, voir la section dédiée) a
ajouté un 25e document ; en `leger` : 99,7 % (323/324) de rappel direct, 94,2 % de
précision, 93,3 % de phrases témoins intactes (`standard` non remesuré, modèles absents).

Les dates d'envoi et de transport des courriels (en-têtes `Date` et `Received`,
séparateurs mbox, en-tête cité dans un corps) figurent depuis dans la vérité terrain sous
le type `DATE_ENVOI`, pour la précision seulement (hors taux de rappel) : ce sont des
métadonnées personnelles que la politique des dates remplace, pas des faux positifs. En
`leger` : 100,0 % de rappel direct, 65,0 % sur les autres types, précision 97,6 %
(90,8 % avec l'ancienne vérité terrain), 93,3 % de phrases témoins intactes.

La cible (≥ 99 % de rappel sur les identifiants directs en profil `standard`) est
atteinte sur ce corpus ; voir les limites plus bas : 322 valeurs ne permettent pas de
garantir 99 % sur des documents réels.

## Méthode

### Boîte noire

Anonyma est utilisé **uniquement par sa ligne de commande**, sur tout le corpus en un
seul lot (comme un utilisateur le ferait, la propagation se fait donc à l'échelle du
lot) :

```
anonyma run <24 dossiers> -o <sortie> --profil leger|standard --rapport rapport.json -q
```

La mesure de référence a été faite et enregistrée (`results/baseline.*`) avant toute
lecture du code de détection.

### Relecture indépendante des sorties

`measure.py` relit chaque fichier produit **sans les extracteurs d'Anonyma** :

* OOXML / ODF / zip : `zipfile` + texte XML (paragraphes recollés pour retrouver les
  noms coupés par la mise en forme, et version balises → espaces) + attributs bruts ;
* courriels : `email` (bibliothèque standard), en-têtes décodés RFC 2047, parties
  décodées (quoted-printable, base64), HTML, pièces jointes et messages joints
  récursivement ; mbox découpé sur les lignes `From ` ;
* msg : `olefile` (flux UTF-16, RTF compressé décompressé par `compressed_rtf`, pièces
  jointes relues récursivement) ; 7z : `py7zr` ;
* PDF : PyMuPDF `get_text`, métadonnées, XMP, signets, annotations, formulaires, liens,
  pièces jointes, objets et flux décompressés ;
* RTF : décodage maison des `\'xx` et `\uN` ; JSON : toutes les clés et valeurs ;
  images : EXIF (dont GPS), blocs texte PNG compressés ;
* pour **tout** fichier : recherche dans les octets bruts décodés en UTF-8, Latin-1,
  UTF-16LE/BE, dans les blocs base64 décodés, et dans **tous les noms de fichiers,
  de dossiers et de membres d'archives**.

### Fuite

Chaque valeur de la vérité terrain porte des *rendus* (`common.py`) : une valeur fuit si
l'un d'eux subsiste n'importe où dans l'arborescence produite pour son document, y
compris en quarantaine. Comparaison insensible à la casse et aux accents, séparateurs
normalisés :

* personne : nom complet dans les deux ordres, nom de famille seul, chaque élément d'un
  nom composé (`Delacroix`, `Vasseur`) ; pour les noms qui sont aussi des mots courants
  (`Petit`, `Boucher`, `Martin`…), seules les formes capitalisées ou en majuscules
  comptent. Le **prénom seul** est compté comme fuite : dans un fil de courriels, c'est
  la forme la plus fréquente d'une personne (« Merci Maëlle ») ;
* e-mail : adresse complète et partie locale suivie de `@` ;
* téléphone, IBAN, carte, NIR, SIREN/SIRET, TVA, plaque, n° de dossier : chiffres et
  lettres sans séparateurs (téléphone : numéro national, préfixe pays toléré ; IBAN :
  IBAN complet ou BBAN ; NIR : avec ou sans clé) ;
* date de naissance : toutes les écritures usuelles (`14/02/1979`, `1979-02-14`,
  `14 février 1979`, `February 14, 1979`, format US…) ;
* adresse : « numéro + voie » et le nom de la voie seul ;
* IP : adresse exacte ; GPS : présence de coordonnées dans l'EXIF.

**Images et PDF numérisés** : le générateur connaît la position de chaque valeur
dessinée. La zone est jugée lisible si l'image produite y est encore corrélée à
l'originale (corrélation ≥ 0,35) ; elle est masquée si la zone est uniforme ou
décorrélée. Pour une personne, la zone contrôlée est celle du nom de famille.

### Précision

Pour chaque document, le texte décodé de l'original et celui de la sortie (même lecteur
indépendant, métadonnées et en-têtes techniques exclus) sont comparés mot à mot
(`difflib`). Chaque segment *remplacé* qui ne recouvre aucune valeur de la vérité
terrain (valeurs, formes écrites, prénoms) est un faux positif. Les suppressions pures
(métadonnées purgées, révisions supprimées acceptées) ne comptent pas. Les dates non
liées à une naissance (date d'un courriel, d'une réunion) sont masquées par choix de
conception d'Anonyma (`detect_dates`) : la précision est donnée avec et sans elles.

En complément, 43 **phrases témoins** ordinaires (« Le boucher du marché couvert
propose désormais des produits bio. ») doivent ressortir intactes.

### Statuts, durées

Statuts lus dans le rapport JSON d'Anonyma ; durée = temps total de la commande pour le
lot (Windows 11, processeur, ONNX Runtime DirectML disponible).

## Corpus

`generate.py` (déterministe, graine fixe) écrit `corpus/<document>/` et
`ground_truth/<document>.json` (type, valeur, emplacements, formes écrites, rendus,
zones d'image). **Aucune donnée réelle** : personnes inventées, domaines
`exemple.test` / `.example`, numéros fictifs ARCEP (06 39 98…, 01 99 00…), Ofcom
(+44 7700 900…, +44 20 7946 0…), plages IP de documentation (192.0.2.0/24,
198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32), IBAN, cartes (Luhn), NIR, SIREN/SIRET
et TVA générés avec des clés valides.

| Document | Format | Valeurs (directes) | Pièges principaux |
|---|---|---|---|
| 01_txt_lettre | txt | 26 (17) | adresse sur 3 lignes avec complément, IBAN au format RIB, NIR espacé, « M. Lefebvre » après première mention, initiale + nom, téléphone avec espaces insécables |
| 02_csv_clients | csv | 96 (84) | 12 lignes : colonnes nom/prénom séparées, NOMS en majuscules, prénoms sans accent, 8 écritures de téléphone, 6 d'IBAN, adresse multiligne, phrases témoins dans les cellules |
| 03_json_crm | json | 20 (18) | clé nominative « Kowalczyk Jean-Baptiste », téléphone en nombre JSON, `login`, échappements `\u`, IPv6 |
| 04_xml_sinistre | xml | 11 (8) | ISO-8859-1, attributs, commentaire, CDATA, instruction de traitement, adresse avec saut de ligne |
| 05_html_equipe | html | 12 (7) | nom dans l'URL et le nom de fichier d'image, `alt`/`title`, nom coupé par `<b>`, `mailto` encodé `%40`, JSON-LD, entités HTML, « J. Petit » |
| 06_eml_fil | eml | 24 (21) | fil de réponses `>`, `>>`, `>>>`, transfert, texte + HTML, image de signature en ligne avec texte, docx et pdf joints, message joint contenant un message joint, « J.-B. K. », IP et nom d'hôte dans `Received` |
| 07_eml_encode | eml | 8 (8) | HTML seul en base64, en-têtes RFC 2047 Q et B, « NOM Prénom », zip joint, pixel de suivi |
| 08_mbox_boite | mbox | 16 (15) | QP, base64, CSV joint, ligne échappée `>From` |
| 09_msg_outlook | msg | 9 (9) | propriétés MAPI, RTF compressé, destinataire, pièce jointe nommée |
| 10_docx_audit | docx | 21 (16) | prénom coupé gras/italique, lien sur deux runs, en-tête/pied, commentaire, révisions (insertion, suppression avec IBAN), note, métadonnées (auteur, dernier modificateur, société), image contenant du texte, personne au nom de ville (« Lorraine Vienne ») |
| 11_xlsx_registre | xlsx | 20 (19) | feuille au nom d'une personne, formule et nom défini qui la citent, commentaire de cellule, note VML, en-tête/pied, téléphone en nombre, IBAN à doubles espaces |
| 12_pptx_projet | pptx | 11 (9) | titre coupé en deux runs, notes de l'orateur, commentaires et auteurs, masque, disposition |
| 13_odt_compte_rendu | odt | 11 (9) | métadonnées, annotation, modification suivie, en-tête/pied, nom coupé par un span, adresse avec saut de ligne, « M. Durand » |
| 14_ods_planning | ods | 6 (6) | nom de feuille, annotation, formule |
| 15_rtf_attestation | rtf | 11 (8) | `\info`, en-tête/pied, `\'e9`/`\u`, gras coupant un prénom, NIR à points, initiales « F.D.V. » |
| 16_pdf_releve | pdf | 9 (9) | métadonnées, XMP, signets, annotation, nom sur deux polices, adresse renvoyée à la ligne dans un cadre, deux pages |
| 17_pdf_numerise | pdf | 7 (7) | page entièrement image (JPEG), aucune couche texte |
| 18_png_capture | png | 7 (7) | capture de formulaire, métadonnées PNG tEXt/iTXt |
| 19_jpg_badge | jpg | 4 (3) | texte dans l'image, EXIF Artist/XPAuthor/propriétaire, **GPS** |
| 20_zip_dossier | zip | 11 (11) | dossier et fichiers nommés d'après la personne, commentaire d'archive, **zip dans un courriel dans un zip** (docx et txt) |
| 21_7z_sauvegarde | 7z | 8 (8) | 7z → zip → courriel → CSV joint |
| 22_dossiers_nommes | txt, csv | 4 (4) | dossiers et fichiers du système de fichiers nommés d'après la personne, minuscules et majuscules sans accents |
| 23_txt_temoins | txt | 4 (0) | témoin négatif : homonymes courants (petit, boucher, martin-pêcheur, fontaine, chevalier, blanc, marchand), villes, dates de réunion |
| 24_txt_anglais | txt | 23 (19) | anglais : +44, +353, +49, IBAN DE/BE, IPv6, date US, apostrophe (« O'Callaghan »), tréma en majuscules |
| 25_pdf_signe | pdf | 5 (2) | ajouté avec le correctif « PDF signé » : tampon de signature en image transparente (nom en casse mixte « zéphyrin QUINTAVALLE-BREZAC », horodatage **vertical**), référence hexadécimale de 40 caractères en image et en **QR code**, identifiant de transaction en **texte invisible**, **signature numérique** (certificat au nom du signataire), Info/XMP nominatifs (titre = nom du fichier), **nom de fichier nominatif** ; éditeur fictif « Paraphéo » (nom, URL, logo) déclaré en liste blanche, qui doit rester |

Types directs : personne, e-mail, téléphone (FR et international), IBAN, carte, NIR,
adresse, plaque, IP, date de naissance, identifiant. Autres : organisation, lieu,
fonction, SIREN, SIRET, TVA, URL, numéro de dossier, GPS.

**Hors périmètre ici** : LibreOffice n'est pas installé en mode source sur cette machine,
donc pas de `.doc` ni de `.ppt` (un `.xls` serait restitué en `.xlsx`) ; le corpus n'en
contient pas. Le profil `complet` (LLM) n'a pas été mesuré (lent sur processeur). Le
modèle OCR latin optionnel n'est pas installé : l'OCR est celui embarqué par
RapidOCR, qui colle souvent les mots d'une ligne.

## Résultats détaillés

Tableaux produits par `compare.py` ; liste complète des fuites et faux positifs de
chaque mesure dans `results/baseline.md` et `results/final.md` (valeurs synthétiques).

### Profil leger

| Mesure | Référence | Final |
|---|---|---|
| Rappel identifiants directs | 91.0 % (293/322) | **99.7 %** (321/322) |
| Rappel autres types | 38.6 % (22/57) | **63.2 %** (36/57) |
| Précision (segments remplacés) | 94.1 % (304/323) | **93.5 %** (290/310) |
| Précision hors dates non liées à une naissance | 99.7 % (304/305) | **99.3 %** (290/292) |
| Phrases témoins intactes | 93.0 % (40/43) | **93.0 %** (40/43) |
| Durée du lot (24 documents) | 17 s | 14 s |

Statuts des fichiers : référence Anonymisé ×16, Résidus ×2, À revoir ×7 ; final Anonymisé ×25

#### Rappel par type (leger)

| Type | Valeurs | Référence | Final |
|---|---|---|---|
| PERSONNE | 71 | 90.1 % | 100.0 % |
| EMAIL | 48 | 100.0 % | 100.0 % |
| TELEPHONE | 51 | 92.2 % | 100.0 % |
| IBAN | 32 | 68.8 % | 100.0 % |
| CARTE | 5 | 100.0 % | 100.0 % |
| NIR | 7 | 71.4 % | 100.0 % |
| ADRESSE | 30 | 90.0 % | 100.0 % |
| PLAQUE | 19 | 100.0 % | 100.0 % |
| IP | 8 | 100.0 % | 100.0 % |
| DATE_NAISSANCE | 34 | 97.1 % | 100.0 % |
| IDENTIFIANT | 17 | 88.2 % | 94.1 % |
| ORGANISATION * | 8 | 75.0 % | 75.0 % |
| LIEU * | 24 | 12.5 % | 66.7 % |
| FONCTION * | 11 | 0.0 % | 9.1 % |
| SIREN * | 2 | 100.0 % | 100.0 % |
| SIRET * | 2 | 100.0 % | 100.0 % |
| TVA * | 2 | 100.0 % | 100.0 % |
| URL * | 4 | 100.0 % | 100.0 % |
| NUMERO_DOSSIER * | 3 | 66.7 % | 66.7 % |
| GPS * | 1 | 100.0 % | 100.0 % |

\* type indirect (hors objectif de 99 %).

#### Par format (leger)

| Format | Rappel direct réf. | Rappel direct final | Précision réf. | Précision finale | Statuts finaux |
|---|---|---|---|---|---|
| 7z | 100.0 % | 100.0 % | 100.0 % | 100.0 % | Anonymisé ×1 |
| csv | 86.9 % | 100.0 % | 100.0 % | 100.0 % | Anonymisé ×1 |
| docx | 100.0 % | 100.0 % | 100.0 % | 100.0 % | Anonymisé ×1 |
| eml | 96.6 % | 100.0 % | 90.8 % | 89.6 % | Anonymisé ×2 |
| html | 100.0 % | 100.0 % | 100.0 % | 100.0 % | Anonymisé ×1 |
| jpg | 100.0 % | 100.0 % | — | — | Anonymisé ×1 |
| json | 83.3 % | 100.0 % | 94.7 % | 95.5 % | Anonymisé ×1 |
| mbox | 100.0 % | 100.0 % | 82.6 % | 78.9 % | Anonymisé ×1 |
| msg | 100.0 % | 100.0 % | 94.4 % | 94.4 % | Anonymisé ×1 |
| ods | 100.0 % | 100.0 % | 100.0 % | 100.0 % | Anonymisé ×1 |
| odt | 100.0 % | 100.0 % | 90.0 % | 90.0 % | Anonymisé ×1 |
| pdf | 81.2 % | 100.0 % | 100.0 % | 100.0 % | Anonymisé ×2 |
| png | 100.0 % | 100.0 % | — | — | Anonymisé ×1 |
| pptx | 100.0 % | 100.0 % | 100.0 % | 100.0 % | Anonymisé ×1 |
| rtf | 75.0 % | 100.0 % | 100.0 % | 100.0 % | Anonymisé ×1 |
| txt | 85.0 % | 97.5 % | 92.2 % | 92.6 % | Anonymisé ×5 |
| xlsx | 89.5 % | 100.0 % | 100.0 % | 100.0 % | Anonymisé ×1 |
| xml | 100.0 % | 100.0 % | 100.0 % | 100.0 % | Anonymisé ×1 |
| zip | 90.9 % | 100.0 % | 93.8 % | 94.4 % | Anonymisé ×1 |

#### Identifiants directs par type × format (leger, final)

Valeurs protégées / valeurs ; entre parenthèses, la référence quand elle diffère.

| Format | PERS | MAIL | TEL | IBAN | CB | NIR | ADR | PLAQ | IP | DDN | LOGIN |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 7z | 3/3 | 2/2 | 1/1 | 2/2 |  |  |  |  |  |  |  |
| csv | 12/12 (8) | 12/12 | 12/12 (11) | 12/12 (6) |  |  | 12/12 | 12/12 |  | 12/12 |  |
| docx | 6/6 | 2/2 | 3/3 | 2/2 |  | 1/1 |  | 1/1 |  | 1/1 |  |
| eml | 7/7 (6) | 6/6 | 5/5 | 1/1 |  | 1/1 | 3/3 | 2/2 | 1/1 | 1/1 | 2/2 |
| html | 2/2 | 1/1 | 1/1 |  |  |  | 1/1 |  |  | 1/1 | 1/1 |
| jpg | 1/1 |  |  |  |  |  |  |  |  | 1/1 | 1/1 |
| json | 2/2 (1) | 2/2 | 2/2 (1) | 1/1 | 1/1 | 1/1 | 2/2 | 1/1 | 2/2 | 2/2 | 2/2 (1) |
| mbox | 5/5 | 3/3 | 3/3 | 1/1 |  |  |  |  |  | 2/2 | 1/1 |
| msg | 2/2 | 2/2 | 1/1 | 1/1 | 1/1 |  |  |  | 1/1 | 1/1 |  |
| ods | 3/3 | 1/1 | 2/2 |  |  |  |  |  |  |  |  |
| odt | 2/2 | 2/2 | 2/2 | 1/1 |  |  | 1/1 |  |  | 1/1 |  |
| pdf | 3/3 | 2/2 | 2/2 | 2/2 (1) | 1/1 | 1/1 (0) | 2/2 (1) | 1/1 |  | 2/2 |  |
| png | 2/2 | 1/1 | 1/1 | 1/1 |  |  |  |  | 1/1 |  | 1/1 |
| pptx | 3/3 | 2/2 | 1/1 |  |  |  | 1/1 |  |  | 1/1 | 1/1 |
| rtf | 3/3 (2) | 1/1 | 1/1 |  |  | 1/1 (0) | 1/1 |  |  | 1/1 |  |
| txt | 7/7 | 5/5 | 7/7 (6) | 4/4 (3) | 2/2 | 1/1 | 4/4 (2) | 1/1 | 3/3 | 3/3 (2) | 2/3 |
| xlsx | 4/4 | 1/1 | 4/4 (3) | 3/3 (2) |  |  | 1/1 |  |  | 3/3 | 3/3 |
| xml | 2/2 | 1/1 | 1/1 |  |  |  | 1/1 | 1/1 |  | 1/1 | 1/1 |
| zip | 2/2 | 2/2 | 2/2 | 1/1 (0) |  | 1/1 | 1/1 |  |  | 1/1 | 1/1 |

### Profil standard

| Mesure | Référence | Final |
|---|---|---|
| Rappel identifiants directs | 94.4 % (304/322) | **100.0 %** (322/322) |
| Rappel autres types | 93.0 % (53/57) | **94.7 %** (54/57) |
| Précision (segments remplacés) | 87.0 % (289/332) | **92.4 %** (291/315) |
| Précision hors dates non liées à une naissance | 91.7 % (289/315) | **97.7 %** (291/298) |
| Phrases témoins intactes | 44.2 % (19/43) | **88.4 %** (38/43) |
| Durée du lot (24 documents) | 50 s | 35 s |

Statuts des fichiers : référence Anonymisé ×10, Résidus ×1, À revoir ×14 ; final Anonymisé ×20, À revoir ×5

#### Rappel par type (standard)

| Type | Valeurs | Référence | Final |
|---|---|---|---|
| PERSONNE | 71 | 95.8 % | 100.0 % |
| EMAIL | 48 | 100.0 % | 100.0 % |
| TELEPHONE | 51 | 92.2 % | 100.0 % |
| IBAN | 32 | 78.1 % | 100.0 % |
| CARTE | 5 | 100.0 % | 100.0 % |
| NIR | 7 | 71.4 % | 100.0 % |
| ADRESSE | 30 | 96.7 % | 100.0 % |
| PLAQUE | 19 | 100.0 % | 100.0 % |
| IP | 8 | 100.0 % | 100.0 % |
| DATE_NAISSANCE | 34 | 100.0 % | 100.0 % |
| IDENTIFIANT | 17 | 94.1 % | 100.0 % |
| ORGANISATION * | 8 | 100.0 % | 100.0 % |
| LIEU * | 24 | 91.7 % | 91.7 % |
| FONCTION * | 11 | 90.9 % | 100.0 % |
| SIREN * | 2 | 100.0 % | 100.0 % |
| SIRET * | 2 | 100.0 % | 100.0 % |
| TVA * | 2 | 100.0 % | 100.0 % |
| URL * | 4 | 100.0 % | 100.0 % |
| NUMERO_DOSSIER * | 3 | 66.7 % | 66.7 % |
| GPS * | 1 | 100.0 % | 100.0 % |

#### Par format (standard)

| Format | Rappel direct réf. | Rappel direct final | Précision réf. | Précision finale | Statuts finaux |
|---|---|---|---|---|---|
| 7z | 100.0 % | 100.0 % | 83.3 % | 100.0 % | Anonymisé ×1 |
| csv | 90.5 % | 100.0 % | 97.1 % | 100.0 % | Anonymisé ×1 |
| docx | 100.0 % | 100.0 % | 93.3 % | 100.0 % | Anonymisé ×1 |
| eml | 100.0 % | 100.0 % | 87.8 % | 87.8 % | Anonymisé ×1, À revoir ×1 |
| html | 100.0 % | 100.0 % | 90.9 % | 100.0 % | Anonymisé ×1 |
| jpg | 100.0 % | 100.0 % | — | — | À revoir ×1 |
| json | 88.9 % | 100.0 % | 95.0 % | 95.5 % | Anonymisé ×1 |
| mbox | 100.0 % | 100.0 % | 72.7 % | 80.0 % | Anonymisé ×1 |
| msg | 100.0 % | 100.0 % | 85.0 % | 94.4 % | Anonymisé ×1 |
| ods | 100.0 % | 100.0 % | 100.0 % | 100.0 % | Anonymisé ×1 |
| odt | 100.0 % | 100.0 % | 76.9 % | 83.3 % | À revoir ×1 |
| pdf | 87.5 % | 100.0 % | 100.0 % | 100.0 % | Anonymisé ×1, À revoir ×1 |
| png | 100.0 % | 100.0 % | — | — | Anonymisé ×1 |
| pptx | 100.0 % | 100.0 % | 90.9 % | 90.9 % | Anonymisé ×1 |
| rtf | 87.5 % | 100.0 % | 85.7 % | 100.0 % | Anonymisé ×1 |
| txt | 92.5 % | 100.0 % | 79.7 % | 92.9 % | Anonymisé ×4, À revoir ×1 |
| xlsx | 89.5 % | 100.0 % | 100.0 % | 100.0 % | Anonymisé ×1 |
| xml | 100.0 % | 100.0 % | 66.7 % | 66.7 % | Anonymisé ×1 |
| zip | 100.0 % | 100.0 % | 94.4 % | 94.4 % | Anonymisé ×1 |

#### Identifiants directs par type × format (standard, final)

Valeurs protégées / valeurs ; entre parenthèses, la référence quand elle diffère.

| Format | PERS | MAIL | TEL | IBAN | CB | NIR | ADR | PLAQ | IP | DDN | LOGIN |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 7z | 3/3 | 2/2 | 1/1 | 2/2 |  |  |  |  |  |  |  |
| csv | 12/12 (9) | 12/12 | 12/12 (11) | 12/12 (8) |  |  | 12/12 | 12/12 |  | 12/12 |  |
| docx | 6/6 | 2/2 | 3/3 | 2/2 |  | 1/1 |  | 1/1 |  | 1/1 |  |
| eml | 7/7 | 6/6 | 5/5 | 1/1 |  | 1/1 | 3/3 | 2/2 | 1/1 | 1/1 | 2/2 |
| html | 2/2 | 1/1 | 1/1 |  |  |  | 1/1 |  |  | 1/1 | 1/1 |
| jpg | 1/1 |  |  |  |  |  |  |  |  | 1/1 | 1/1 |
| json | 2/2 | 2/2 | 2/2 (1) | 1/1 | 1/1 | 1/1 | 2/2 | 1/1 | 2/2 | 2/2 | 2/2 (1) |
| mbox | 5/5 | 3/3 | 3/3 | 1/1 |  |  |  |  |  | 2/2 | 1/1 |
| msg | 2/2 | 2/2 | 1/1 | 1/1 | 1/1 |  |  |  | 1/1 | 1/1 |  |
| ods | 3/3 | 1/1 | 2/2 |  |  |  |  |  |  |  |  |
| odt | 2/2 | 2/2 | 2/2 | 1/1 |  |  | 1/1 |  |  | 1/1 |  |
| pdf | 3/3 | 2/2 | 2/2 | 2/2 (1) | 1/1 | 1/1 (0) | 2/2 | 1/1 |  | 2/2 |  |
| png | 2/2 | 1/1 | 1/1 | 1/1 |  |  |  |  | 1/1 |  | 1/1 |
| pptx | 3/3 | 2/2 | 1/1 |  |  |  | 1/1 |  |  | 1/1 | 1/1 |
| rtf | 3/3 | 1/1 | 1/1 |  |  | 1/1 (0) | 1/1 |  |  | 1/1 |  |
| txt | 7/7 | 5/5 | 7/7 (6) | 4/4 (3) | 2/2 | 1/1 | 4/4 (3) | 1/1 | 3/3 | 3/3 | 3/3 |
| xlsx | 4/4 | 1/1 | 4/4 (3) | 3/3 (2) |  |  | 1/1 |  |  | 3/3 | 3/3 |
| xml | 2/2 | 1/1 | 1/1 |  |  |  | 1/1 | 1/1 |  | 1/1 | 1/1 |
| zip | 2/2 | 2/2 | 2/2 | 1/1 |  | 1/1 | 1/1 |  |  | 1/1 | 1/1 |

## Classes de fuites trouvées et corrections

Corrigées à la racine (détecteur, propagation, extraction), sans cas particulier pour le
corpus ; chaque classe a son test de non-régression.

| Classe de fuite (référence) | Cause | Correction |
|---|---|---|
| IBAN au format RIB (4/5/5/11/2), avec tirets, points ou doubles espaces (10 IBAN en `standard`) | motif limité aux groupes de 4 séparés par un espace | motif tous groupements et séparateurs ; la clé décide, la correspondance est réduite au plus long préfixe valide ; un candidat invalide reste borné |
| NIR à points ; NIR collé à un mot par l'OCR | séparateurs limités aux espaces, frontière gauche stricte | séparateurs points/tirets ; la clé suffit à sécuriser une frontière gauche alphabétique |
| Téléphones `06/39/98/55/08`, `+44 7700 900456` (plage réservée), `33639984697` stocké en nombre | séparateur `/` absent ; numéros « possibles » non attribués rejetés ; indicatif sans `+` | règle dédiée aux barres obliques ; règle « possible » (score de revue) ; nombre à 11 chiffres 336/337 et contexte « téléphone » |
| Noms en colonnes séparées d'un CSV (« LEBRUN » / « Margaux ») ; `login` en JSON | le contexte de l'en-tête de colonne ou de la clé était perdu | **indices de champ** (`anonyma/fieldhints.py`) : un en-tête (csv, tsv, xlsx, ods) ou une clé JSON reconnu type ses valeurs ; le libellé ne porte qu'un mot du vocabulaire, jamais l'en-tête ni la clé ; score sous le seuil de certitude du vérificateur |
| Ligne entière absente de l'OCR (IBAN d'un courrier numérisé) | la passe « boîtes de mots » de RapidOCR renvoie un texte vide sur une ligne longue | relecture seule de la boîte en échec (reconnaissance sur le recadrage) |
| Complément d'adresse « Bât. C, appartement 34 » | non reconnu | règle « compléments » (bâtiment, appartement, escalier, étage) |
| Clé JSON « Kowalczyk Jean-Baptiste » (en `leger`, mise en quarantaine) | la propagation ignorait une forme qui chevauche une mention plus courte (« Jean-Baptiste ») | une forme plus longue est retenue, le remplacement porte sur l'union |
| Initiales « J.-B. K. », « F.D.V. », « J.-B. Kowalczyk » (en `leger`) | formes non propagées | formes d'initiales pointées propagées |
| Organisation lue « TransportsValmontSAS » par l'OCR (quarantaine en `leger`) | mots collés par l'OCR | noms et adresses de plusieurs mots aussi cherchés sans espaces |
| Adresse renvoyée à la ligne dans un cadre PDF ; adresse anglaise « 221 Larkspur Crescent » ; « 37000Tours » ; date US « 03/14/1985 » (en `leger`) | sauts de ligne non admis ; ordre anglais ; OCR collé ; ordre mois/jour | un saut de ligne admis entre les mots d'une adresse ; ordre anglais ; code postal collé à une ville capitalisée ; règle de date US non ambiguë |
| Nom de fichier « CV [PERSONNE_049]txt.txt » (substitut incohérent, extension abîmée) | le point de l'extension pris dans le nom | point final exclu de la mention |

Faux positifs corrigés (profil `standard`), qui se propageaient à tout le lot :

* « Mme », « Madame, Monsieur », « Un martin-pêcheur » pris pour des personnes,
  « comité de direction », « salle du rez-de-chaussée » pour des organisations ou des
  lieux : écartés quel que soit le score (civilités seules, mots courants en minuscules) ;
* « Bonjour Rémi », « Rappeler M. Joubert » : salutation ou verbe retirés de la mention ;
* « marchand de journaux », « comédien amateur » pris pour des fonctions : une fonction
  n'est retenue qu'à proximité d'une personne (même paragraphe) ou si elle occupe la
  cellule ; et une fonction n'est **propagée et vérifiée que dans son propre fichier**
  (« présidente » à côté de Mme Vidal n'est pas masquée dans tous les autres documents,
  et sa présence ailleurs n'envoie plus en quarantaine).

## Correctif global n°1 (incident du PDF signé)

### L'incident

Un vrai PDF signé électroniquement (une page de formulaire, une page de certificat de
signature) était sorti marqué « Anonymisé » alors qu'il contenait encore : le nom du
signataire dans une **image de tampon** posée sur une page qui avait pourtant une
couche texte ; des **horodatages verticaux**, une **référence hexadécimale de
40 caractères** et un **QR code** dans des images ; un identifiant de transaction en
**texte invisible** ; un **nom de fichier** identique à l'original et contenant le nom
de la personne. À l'inverse, l'URL publique et le logo d'un éditeur en liste blanche
avaient été masqués. Le vérificateur n'avait rien signalé.

### Ce qui a changé

* **OCR de toute image et de toute page**, dans tous les formats : chaque image
  intégrée est lue (y compris sur les pages qui ont une couche texte), chaque page PDF
  est aussi rendue et lue entière (tampons, texte vectorisé, champs aplatis), avec
  déduplication par rapport à la couche texte ; passe verticale pour le texte tourné.
  Un OCR désactivé, indisponible ou en échec n'est plus silencieux : il produit un
  problème `ocr…`.
* **Codes** : QR codes, Data Matrix et codes-barres décodés ; le code est toujours
  masqué en entier et son contenu passe par la détection.
* **Texte invisible** (mode de rendu 3, blanc, hors page, taille nulle) : lu,
  anonymisé et supprimé par défaut.
* **Signatures numériques** : leur texte (signataire, certificat, dates) est détecté,
  puis la signature est supprimée ; le PDF est entièrement réécrit (plus d'`/ID`
  d'origine, plus de mises à jour incrémentales).
* **Identifiants techniques** (empreintes hexadécimales, base64, UUID, identifiants de
  transaction) et **horodatages** masqués.
* **Politique des organisations** : « toutes », « toutes sauf la liste blanche »
  (défaut, liste fournie d'éditeurs, prestataires de signature et domaines publics)
  ou « liste noire » ; un éditeur conservé garde son nom, son URL générique et son
  logo.
* **Noms de fichiers** : les détecteurs lisent les noms avec `_`, `+` et le point de
  l'extension comme des espaces ; les valeurs du lot sont recherchées dans les noms,
  métadonnées, attributs et en-têtes quels que soient les séparateurs
  (`Contrat_jean_DUPONT`, `DUPONT-Jean`) ; option de noms neutres (`document_0001`,
  `piece_jointe_0001`, correspondance chiffrée en pseudonymisation).
* **Vérificateur** : il relit la sortie avec les mêmes extracteurs (donc OCR, codes,
  texte invisible, signatures, métadonnées et noms) et compare avec toutes les valeurs
  du lot, y compris leurs formes en casse mixte, sans tiret, collées et leurs formes
  « OCR » (confusions `0`/`O`, `1`/`l`/`I`…). Il cherche aussi le nom d'origine du
  fichier, sa racine et ses mots distinctifs. Une image ou une page de la sortie qui
  n'a pas pu être relue rend la vérification **incomplète** : le fichier n'est jamais
  « Anonymisé », il est « À revoir » et placé en quarantaine. Une image non lue à
  l'analyse donne aussi « À revoir ».

### Nouveau cas du corpus : `25_pdf_signe`

Reproduction synthétique de l'incident (générateur partagé avec le test de
non-régression `tests/test_regression_signed_pdf.py`) : signataire fictif
« Zéphyrin Quintavalle-Brezac » dans le texte, un champ de formulaire, un tampon en
image transparente écrit « zéphyrin QUINTAVALLE-BREZAC » avec un horodatage vertical,
la signature numérique (certificat à son nom), les métadonnées Info/XMP (titre = nom
du fichier) et le nom du fichier ; référence hexadécimale en image et en QR code ;
identifiant de transaction en texte invisible ; éditeur fictif « Paraphéo » (nom, URL,
logo). Pour la mesure, l'éditeur est ajouté à la liste blanche fournie
(`--organisations-blanches`), comme un utilisateur déclarerait son prestataire.

Résultat (`leger`) : **5 valeurs sur 5 protégées** (personne, e-mail, horodatage,
référence, identifiant de transaction), y compris dans les images (zones du tampon,
de l'horodatage vertical, de la référence et du QR code masquées), aucun prénom
résiduel, signature et `/ID` d'origine absents, nom produit
`Contrat_[PERSONNE_065]_signe.pdf` ; le nom et l'URL de l'éditeur sont conservés
(2/2 phrases témoins), statut « Anonymisé ».

### Mesure

Profil `leger` seulement : le dossier `models/` est vide sur cette machine, le profil
`standard` (GLiNER) n'a pas pu être remesuré. Détail dans `results/correctif1.{json,md}`.

| Mesure (`leger`) | Final précédent (24 documents) | Correctif n°1 (25 documents) |
|---|---|---|
| Rappel identifiants directs | 99,7 % (321/322) | **99,7 %** (323/324) |
| Rappel autres types | 63,2 % (36/57) | **65,0 %** (39/60) |
| Précision (segments remplacés) | 93,5 % (290/310) | **94,2 %** (291/309) |
| Précision hors dates non liées à une naissance | 99,3 % (290/292) | 95,1 % (291/306) |
| Phrases témoins intactes | 93,0 % (40/43) | **93,3 %** (42/45) |
| Statuts | Anonymisé ×25 | Anonymisé ×26 |
| Durée du lot | 14 s | 35 s |

Lecture :

* sur les 24 documents d'origine, rien ne change pour le rappel : la seule fuite
  directe reste `socallaghan` (voir plus bas) ; les 2 identifiants directs et les
  3 autres valeurs ajoutés par `25_pdf_signe` sont tous protégés ;
* la baisse de la précision « hors dates » est un effet de mesure : les horodatages
  désormais masqués (`04 mar 2026 09:00:00 +0100` dans les en-têtes `Date`, « 12 mars
  2026 à 14 h 30 », « 16 h ») ne sont pas reconnus comme des dates par le filtre de
  `measure.py` et comptent donc comme faux positifs ; ce sont les mêmes segments
  qu'avant, élargis à l'heure, plus « 16 h ». La précision globale progresse ;
* la durée augmente (OCR de chaque page et de chaque image, réécriture complète des
  PDF) : 35 s au lieu de 14 s pour le lot.

### Limites connues de ce correctif

* les images `data:` intégrées dans du CSS ne sont pas extraites ;
* les images EMF/WMF/SVG sont remplacées par un contenu vide, pas rastérisées ni lues ;
* le texte rogné (hors de la zone de découpe) est traité comme visible ;
* un QR code ou un code-barres qui ne peut pas être décodé est ignoré (il n'est
  masqué que si l'OCR ou la détection le couvrent) ;
* la passe OCR verticale perd parfois des espaces entre les mots ;
* un prénom rare écrit en casse mixte, sans aucun indice (civilité, libellé,
  autre mention), n'est pas détecté.

## Profil complet (modèle de langage local)

Mesuré avec Qwen3 4B (Q4_K_M, processeur seul, `--accueil` contenant aussi GLiNER),
avant et après la reprise du harnais du modèle de langage (`results/complet.*`) :

| | Rappel direct | Autres types | Précision | Phrases témoins | À revoir |
|---|---|---|---|---|---|
| `complet` — avant | 100,0 % (324/324) | 96,7 % | 92,7 % | 82,2 % | 6 |
| `complet` — après | 100,0 % (324/324) | 96,7 % | 92,7 % | 82,2 % | 9 |
| `complet` — gros lots (`results/complet-volume.*`) | 100,0 % (324/324) | 96,7 % | 95,5 % | 82,2 % | 10 |

Ligne « gros lots » : le modèle ne lit plus que les paragraphes rédigés où il peut
ajouter quelque chose, une fois chacun par lot, après les autres détecteurs (lancés en
parallèle) ; lot traité en 442 s au lieu de 937 à 1 459 s. Une première mesure avait
perdu « Berlin » (24-018, phrase anglaise pleine de numéros, pas reconnue comme
rédigée) : les chiffres comptent désormais comme du texte rédigé.

Sur ce corpus, les expressions régulières et GLiNER trouvent déjà tout ce que le modèle
de langage trouve : les chiffres ne bougent pas. Les phrases témoins perdues par rapport
à `standard` (88,4 %) sont « Le boucher du marché couvert… », que le modèle décrit comme
identifiant indirect : elles restent masquées, mais vont désormais en revue (d'où les
trois fichiers « À revoir » de plus). Les gains se voient sur le corpus dédié au modèle
(`tests/test_llm.py`, mesures dans l'historique Git de `anonyma/llm.py`) : texte injecté
14/14 entités trouvées (8/14 avant), segment long 97,8 % de rappel (64,4 % avant),
aucune détection sur 90 segments sans contenu. Durées non comparables (machine partagée
pendant les mesures).

## Limites et fuites connues restantes

* **`leger`** : 1 fuite, l'identifiant `socallaghan` cité sans contexte (« for the account
  socallaghan ») : sans modèle, la personne « Siobhan O'Callaghan » n'est pas reconnue
  (prénom absent de la liste), donc ses formes d'identifiant ne sont pas dérivées.
  Le profil `leger` ne détecte pas les fonctions (9 % de rappel) et peu de lieux hors
  adresses et colonnes typées.
* **Prénoms seuls** : comptés comme fuites depuis la 0.4.0. Avant la propagation des
  prénoms à tout le lot, ils faisaient tomber le rappel direct à 98,8 % en `leger` et
  99,7 % en `standard` ; il est revenu à 100 % dans les deux profils, sans perte de
  précision (`leger` 97,6 %, `standard` 96,1 %, l'écart venant des dates sans année
  désormais masquées).
* **Précision** : le masquage des dates non liées à une naissance (choix d'Anonyma,
  `detect_dates`) est la première cause de « faux positifs » (17 sur 24 en
  `standard`) ; il se désactive par la configuration. Restent notamment
  « bâtiment B » (règle de compléments d'adresse), « salle Bleue », « Projet Hermine »,
  « boucher du marché couvert » cité à côté d'une personne, et la « salle Fontaine »
  masquée parce que Fontaine est le nom d'une personne du lot (homonymie assumée).
* **OCR** : sans le modèle latin, les mots d'une ligne sont souvent collés ; les lignes
  relues seules reçoivent des boîtes de caractères proportionnelles, donc une marge
  d'un caractère au bord d'une valeur masquée est possible (vu sur le « F » d'un IBAN
  numérisé, sans fuite d'identifiant). Une ligne que même la relecture ne lit pas reste
  invisible pour la détection.
* **Taille** : 322 identifiants directs ; une seule fuite vaut 0,3 point. Le rappel
  de 100 % mesure ce corpus et ses pièges, pas une garantie sur des documents réels :
  relire les fichiers « À revoir » reste nécessaire.
* **Mesure** : la précision n'est calculée que sur le texte (pas sur les images) ; les
  valeurs absentes de la vérité terrain (par exemple une ville seule non listée)
  n'entrent pas dans le rappel ; la zone contrôlée d'une personne dans une image est
  celle du nom de famille. Les archives 7z embarquent l'heure de génération (seul
  fichier non reproductible à l'octet près).
* **Environnement** : Windows 11, sources, sans LibreOffice (pas de `.doc`/`.ppt`, un
  `.xls` deviendrait `.xlsx`) ; profil `complet` mesuré une fois (section dédiée). Le test
  `tests/gui/test_models_page.py::test_hardware_and_recommendation` interrompt
  Python (« Fatal Python error: Aborted ») quand toute la suite tourne dans ce bac à
  sable, y compris sur le code d'origine ; il passe seul.

## Relancer

```sh
uv run python -m tests.benchmark.generate             # régénère corpus/ et ground_truth/
uv run python -m tests.benchmark.measure --nom essai  # leger + standard -> results/essai.{json,md}
uv run python -m tests.benchmark.measure --profil standard --travail /tmp/bench --reutiliser
uv run python -m tests.benchmark.compare baseline essai   # tableaux comparatifs
uv run pytest tests/test_benchmark.py                 # seuils (marqueurs slow, models)
```

`measure.py` lance la ligne de commande avec l'interpréteur courant ; le profil
`standard` demande le modèle GLiNER dans `models/` (ou `--accueil <dossier>` pour un
`ANONYMA_HOME` contenant `models/`). Les seuils vérifiés par `tests/test_benchmark.py` :
`standard` ≥ 99 % de rappel direct, `leger` ≥ 98 %, précision ≥ 85 %, phrases témoins
≥ 80 % (`standard`) et 85 % (`leger`).
