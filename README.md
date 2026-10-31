<img src="packaging/icons/anonyma.svg" width="72" height="72" alt="">

# Anonyma

Anonyma anonymise en masse, **entièrement en local**, des documents de tous formats :
il repère les données personnelles (noms, adresses, courriels, téléphones, IBAN, NIR…),
les remplace de façon cohérente dans tout le lot, purge les métadonnées, puis
**relit chaque fichier produit** pour vérifier qu'il ne reste rien.

> [!CAUTION]
> **Usage interne uniquement.** Anonyma embarque des composants sous licence AGPL-3.0
> (PyMuPDF) et GPL-3.0 (extract-msg) : il ne doit pas être distribué à des tiers.
> Voir [LICENSES.md](LICENSES.md).

## Installation

### Dossier portable (recommandé)

Téléchargez l'archive de votre système (`Anonyma-<version>-windows-x86_64.zip`,
`…-macos-arm64.zip`, `…-linux-x86_64.zip`), décompressez-la où vous voulez (disque
local, clé USB) et lancez :

| Système | Interface graphique | Ligne de commande |
|---|---|---|
| Windows | `Anonyma.exe` | `anonyma-cli.exe` |
| macOS | `Anonyma.app` | `./anonyma-cli` |
| Linux | `./Anonyma` | `./anonyma-cli` |

Rien n'est installé sur la machine : configuration, journaux, fichiers temporaires et
modèles restent dans le dossier (`data/` et `models/`). Pour désinstaller, supprimez le
dossier. Le dossier doit être accessible en écriture.

**macOS (Gatekeeper).** L'application n'est ni signée ni notarisée. Au premier
lancement, macOS refuse de l'ouvrir (« développeur non identifié »). Avant toute chose,
retirez l'attribut de quarantaine de tout le dossier décompressé :

```sh
xattr -dr com.apple.quarantine /chemin/vers/Anonyma
```

Sinon, macOS peut aussi exécuter l'application depuis un emplacement temporaire en
lecture seule (« App Translocation ») : Anonyma ne retrouverait alors ni ses modèles ni
sa configuration. Ne déplacez pas `Anonyma.app` hors de son dossier : `models/` et
`data/` doivent rester à côté.

### LibreOffice embarqué

Le dossier portable contient une copie de **LibreOffice 26.2.6** (MPL-2.0) dans
`libreoffice/` : les anciens formats Office (`.doc`, `.xls`, `.ppt`) sont ainsi
toujours restitués **dans leur format d'origine**, sans rien installer. Cette copie est
utilisée en priorité (avant un éventuel LibreOffice installé sur la machine), en mode
invisible, avec un profil jetable créé dans `data/tmp/` puis supprimé : aucune
préférence n'est écrite hors du dossier, les macros sont désactivées.

LibreOffice est livré dans l'archive : l'application ne télécharge jamais rien pour
lui. Tailles indicatives (Windows x86_64) :

| Archive | Dossier décompressé | Archive zip |
|---|---|---|
| complète (avec LibreOffice) | 1,1 Go | 484 Mo |
| légère (sans LibreOffice) | 470 Mo | 207 Mo |

Avec l'archive légère (sans LibreOffice), les `.doc` et `.ppt`
ne sont pas pris en charge et les `.xls` sont produits au format `.xlsx`. Un LibreOffice
installé ailleurs peut être désigné par
`anonyma-cli config definir libreoffice_path "C:\chemin\vers\LibreOffice"`.

Sous Linux, la copie embarquée utilise les bibliothèques graphiques usuelles du système
(X11, fontconfig, cairo, CUPS), présentes sur tout poste de bureau.

## Interface graphique

Lancez `Anonyma.exe` (Windows), `Anonyma.app` (macOS) ou `./Anonyma` (Linux). Tout se
passe en local : aucune donnée ne quitte le poste.

- **Traitement** : déposez des fichiers ou des dossiers, choisissez un dossier de sortie
  distinct des originaux, puis cliquez sur « Anonymiser ». Le réglage **Relecture** décide
  si ce bouton s'arrête pour une revue : *aucune* (écriture directe), *entités incertaines
  seulement* (par défaut) ou *systématique*. « Analyser », « Revoir » et « Appliquer »
  restent disponibles pour garder la main. En pseudonymisation, une phrase secrète protège
  la table de correspondance chiffrée.
- **Revue** (facultative) : une ligne par entité, par exemple « Bérénice Lagarde —
  PERSONNE — 14 occurrences dans 6 fichiers », les plus incertaines d'abord, avec le motif
  de détection. Une décision (valider, rejeter, changer le type) s'applique à toutes les
  occurrences du lot. Dépliez une ligne pour voir chaque occurrence dans son contexte et y
  accéder dans la vue côte à côte : texte (en-têtes de courriel regroupés), ou rendu
  visuel des PDF et des images avec les zones encadrées. Un compteur « 12 / 40 décidées »,
  le bouton « Suivante non décidée », la recherche et les actions groupées accélèrent la
  relecture. Sélectionnez du texte pour ajouter une entité oubliée. Anonyma propose
  ensuite d'ajouter un faux positif à la liste blanche, ou de toujours masquer un ajout
  (modification annulable). Raccourcis : N / P naviguer, Maj+N suivante non décidée,
  V valider, R rejeter, T type, A ajouter.
- **Rapport** : bilan du lot, exportable en JSON, Markdown ou HTML. Les fichiers en
  quarantaine (dossier `<sortie>_quarantaine`, à côté du dossier de sortie) sont expliqués et ne doivent pas être diffusés. Chaque
  résidu s'ouvre à son emplacement dans le fichier produit, et « Corriger et réappliquer »
  ramène à la revue puis remplace les fichiers produits précédemment.
- **Modèles** et **Réglages** : matériel détecté, modèles installés (le téléchargement est
  la seule action réseau, toujours à votre demande) et réglages enregistrés dans
  `data/config.json`.

Statuts d'un fichier : **Anonymisé** (vérifié, rien à signaler) ; **À revoir** (vérifié,
mais des entités incertaines ont été masquées par précaution ou un détecteur statistique
signale un résidu possible) ; **Résidus** (une valeur d'origine a été retrouvée : fichier
en quarantaine) ; **Échec** ou **Non pris en charge** (rien n'est produit).

## Utilisation en ligne de commande

Toutes les commandes acceptent `-h` / `--help`.

```sh
# Anonymiser un dossier (sous-dossiers compris) vers un dossier de sortie
anonyma-cli run "Dossiers clients" --sortie anonymises --rapport rapport.html

# Choisir le profil, le mode de remplacement, ajouter des listes
anonyma-cli run courrier.docx -o sortie --profil standard --mode realiste \
    --liste-metier clients.csv --liste-metier projets.csv --liste-blanche ville.txt

# Analyse à blanc : ce qui serait masqué, sans rien écrire
anonyma-cli analyser "Dossiers clients"
anonyma-cli analyser "Dossiers clients" --json

# Matériel, formats, modèles, configuration
anonyma-cli materiel
anonyma-cli formats
anonyma-cli modeles liste
anonyma-cli config afficher
anonyma-cli config definir mode realiste
anonyma-cli config definir limits.max_files 50000
```

Options principales de `run` :

| Option | Rôle |
|---|---|
| `--sortie`, `-o` | dossier de sortie (distinct des originaux, jamais modifiés) |
| `--profil`, `-p` | `auto` (selon le matériel), `leger`, `standard`, `complet` |
| `--mode`, `-m` | `balises` (`[PERSONNE_001]`), `realiste` (substituts vraisemblables), `masquage` |
| `--regime`, `-r` | `anonymisation` (irréversible) ou `pseudonymisation` (table chiffrée) |
| `--paranoiaque` | masque aussi tout nom propre et toute séquence suspecte |
| `--liste-blanche FICHIER` | valeurs à ne jamais masquer, une par ligne (`#` = commentaire) |
| `--liste-metier CSV` | valeurs à toujours masquer, colonnes `valeur[,type]` ; répétable |
| `--processus N`, `-j N` | nombre de processus (0 = automatique) |
| `--config FICHIER` | autre fichier de configuration JSON |
| `--rapport FICHIER` | rapport `.json`, `.md` ou `.html` (aucune donnée d'origine) |
| `--table FICHIER` | table de correspondance (pseudonymisation) |
| `--silencieux`, `-q` | sans barre de progression |

Codes de sortie : **0** tous les fichiers sont anonymisés, **1** au moins un fichier
présente des résidus, a échoué, n'est pas pris en charge ou le traitement a été
interrompu, **2** erreur d'utilisation (option invalide, phrase secrète incorrecte,
configuration invalide…). Les fichiers dont la vérification échoue sont placés dans
`<sortie>_quarantaine/`, **à côté** du dossier de sortie (jamais dedans : le dossier de
sortie ne contient que des fichiers vérifiés et peut être partagé tel quel) ; ils ne doivent
pas être diffusés.

## Formats

Principe : vous choisissez un fichier ou un dossier ; chaque fichier est lu, anonymisé
puis restitué **dans son format d'origine**, avec la même arborescence et la même mise en
forme. Les noms de fichiers et de dossiers sont anonymisés comme le contenu
(`CV Jean Dupont.pdf` devient `CV [PERSONNE_001].pdf`). Les originaux ne sont jamais
modifiés. `anonyma-cli formats` affiche la liste exacte disponible sur le poste.

| Famille | Formats | Ce qui est traité |
|---|---|---|
| Bureautique | docx, xlsx, pptx (et variantes à macros ou modèles), odt, ods, odp, rtf | Texte (y compris les noms coupés par la mise en forme), tableaux, en-têtes et pieds de page, notes, commentaires (conservés, anonymisés, auteur « Anonyme »), révisions (acceptées), zones de texte, graphiques, noms de feuilles et formules, liens, textes alternatifs, images incorporées (OCR). Métadonnées, historique, miniatures et macros purgés. |
| Anciens formats | doc, xls, ppt | Convertis par le LibreOffice embarqué, anonymisés, puis réécrits dans leur format d'origine. Sans LibreOffice (version allégée), les xls sont restitués en xlsx (valeurs seules) et les doc/ppt échouent explicitement. |
| PDF | pdf texte et numérisé | Suppression réelle du texte (redaction, pas un simple cache), OCR des pages numérisées, formulaires, pièces jointes, signets, liens, métadonnées et XMP purgés. |
| Courriels | eml, msg, mbox, pst | Tous les en-têtes (From, To, Cc, Bcc, Reply-To, Sender, Return-Path, Received, Message-ID, In-Reply-To, References…), noms d'affichage, corps texte et HTML (attributs `href`, `alt`, `title` compris), citations, signatures, images incorporées (OCR), pièces jointes et messages joints, récursivement. Les msg sont réécrits nativement en msg ; un pst est restitué sous forme de **dossier d'eml** reproduisant l'arborescence de la boîte. Signatures DKIM/ARC supprimées (elles deviendraient invalides). |
| Données et texte | txt, log, md, csv, tsv, json, xml, html | Contenu, clés et nombres JSON, attributs et commentaires XML/HTML ; encodage, délimiteurs, indentation et balisage conservés à l'identique hors des valeurs remplacées. |
| Images | png, jpg, tiff (multipage), bmp, heic, webp | OCR puis masquage opaque des zones, métadonnées EXIF/XMP/IPTC et GPS purgées. Sans moteur OCR, l'image est signalée en échec (son texte ne peut pas être vérifié). |
| Archives | zip, 7z | Contenu traité récursivement sans limite arbitraire de profondeur, avec protection contre les bombes d'archives (taille, taux de compression, nombre de fichiers). Les entrées chiffrées sont signalées et omises. |

Un élément qui ne peut pas être anonymisé (format inconnu, fichier chiffré, lecture
impossible) n'est **jamais recopié tel quel** : il est absent de la sortie et signalé dans
le rapport. Un fichier dans lequel la vérification retrouve une valeur d'origine est placé
dans `<sortie>_quarantaine/`, à côté du dossier de sortie.

## Profils et matériel

| Profil | Détection | Besoins indicatifs |
|---|---|---|
| `leger` | expressions régulières + Presidio + listes métier | toute machine |
| `standard` | + modèle GLiNER (entités nommées) | 6 Go de mémoire vive |
| `complet` | + modèle de langage local (Qwen3, GGUF Q4) | 16 Go et 8 processeurs logiques |

`anonyma-cli materiel` affiche la mémoire, les processeurs, la carte graphique, les
accélérations disponibles pour ONNX Runtime (DirectML sous Windows, Core ML sous macOS,
processeur sinon) et le profil recommandé. Le modèle de langage recommandé est le plus
grand du catalogue qui tient dans la mémoire vive (4B, 8B ou 14B). Pour imposer un
profil : `--profil` ponctuellement, ou `anonyma-cli config definir profile complet`
(`auto` pour revenir au choix automatique). Un détecteur dont le modèle n'est pas
installé est ignoré et signalé.

## Modèles

Les modèles d'IA ne sont pas livrés dans l'archive :

```sh
anonyma-cli modeles liste                    # catalogue et état (installé, vérifié…)
anonyma-cli modeles telecharger <identifiant>
anonyma-cli modeles supprimer <identifiant>
```

Le téléchargement est la **seule** opération réseau d'Anonyma, toujours déclenchée
explicitement. Avant de télécharger, Anonyma lit la licence publiée sur Hugging Face et
refuse le modèle si elle est non commerciale (`cc-by-nc…`, « recherche uniquement »…),
non identifiée (`other`, `unknown`) ou différente de celle du catalogue. Les fichiers
sont téléchargés à la révision figée par le catalogue, **vérifiés par empreinte
SHA-256**, puis installés d'un bloc dans `models/<identifiant>/`. Un téléchargement
interrompu (coupure, Ctrl+C) reprend là où il s'était arrêté.

**Machine sans accès à Internet.** Téléchargez les fichiers du dépôt Hugging Face sur
une autre machine (à la révision indiquée par `modeles liste`), copiez-les puis :

```sh
anonyma-cli modeles importer <identifiant> /media/cle-usb/modele/
```

L'import accepte un dossier ou une liste de fichiers ; chaque fichier est reconnu par
son nom ou son empreinte et vérifié avant installation.

## Pseudonymisation ou anonymisation

- **Anonymisation** (par défaut) : aucune correspondance n'est conservée ; l'opération
  est irréversible.
- **Pseudonymisation** (`--regime pseudonymisation`) : les correspondances
  « substitut → valeur d'origine » sont enregistrées dans une **table chiffrée**
  (`data/table-correspondance.anonyma` par défaut, ou `--table`). Une même personne
  garde le même substitut d'un lot à l'autre.

La table est protégée par une **phrase secrète**, demandée à l'ouverture (saisie
masquée, confirmée à la création) ou lue dans la variable d'environnement
`ANONYMA_PASSPHRASE` pour les traitements automatisés. Elle n'est enregistrée nulle
part : **une phrase secrète perdue rend la table définitivement illisible.** La clé est
dérivée par Argon2id ; valeurs d'origine et substituts sont chiffrés en AES-256-GCM et
recherchés par empreintes HMAC : rien n'apparaît en clair dans le fichier.

Ré-identification (pseudonymisation uniquement) :

```sh
anonyma-cli reidentifier --balise "[PERSONNE_001]"
anonyma-cli reidentifier sortie/ --sortie reidentifies/   # fichiers texte (.txt, .md, .csv, .json…)
```

## Sécurité

- **Aucun accès réseau** : toute connexion sortante est bloquée dans le processus, sauf
  pendant un téléchargement de modèle demandé par l'utilisateur. Aucune télémétrie.
- **Originaux intacts** : les fichiers sources ne sont jamais modifiés ; le dossier de
  sortie doit être distinct des entrées.
- **Fichiers temporaires** : uniquement dans `data/tmp/`, supprimés en fin de lot et au
  démarrage suivant (en cas d'arrêt brutal), sans jamais toucher au lot d'une autre
  instance d'Anonyma en cours.
- **Vérification systématique** : chaque fichier produit est relu ; s'il reste une valeur
  sensible ou si la relecture est impossible, il part en **quarantaine**
  (`<sortie>_quarantaine/`, hors du dossier de sortie) et n'est jamais compté comme anonymisé.
- **Contrôle des octets bruts** : en plus de cette relecture, les octets du fichier produit
  et de chacun de ses éléments (membres d'archive, parties Office, pièces jointes, flux
  PDF et Outlook) sont fouillés, sous leurs encodages courants (UTF-8, UTF-16, Windows-1252,
  entités HTML/XML, quoted-printable, base64), à la recherche des valeurs d'origine
  identifiantes du lot : la moindre trouvaille envoie le fichier en quarantaine.
- **Balisage caché** : en HTML et dans les courriels HTML, rien n'est recopié sans avoir été
  analysé (instructions de traitement, DOCTYPE, commentaires atypiques, noms d'attributs
  inconnus) ; une fausse balise comme `<prenom.nom@exemple.fr>` est traitée comme du texte.
  En XML, la DTD interne n'est jamais recopiée (ses entités sont remplacées par leur texte
  anonymisé) ; une partie Office ou OpenDocument qui en contient une est refusée.
- **Liens ignorés** : dans un dossier d'entrée, les liens symboliques et les jonctions ne
  sont jamais suivis ; ils sont signalés « non pris en charge ».
- **LibreOffice isolé** : profil jetable sans macros, contenu actif, contenu distant, mise
  à jour des liens ni recalcul à l'ouverture ; il ne reçoit que les variables système
  (aucun proxy, jamais la phrase secrète).
- **Processus de travail** : si l'un d'eux plante, les fichiers restants sont repris un par
  un dans un nouveau processus, jamais dans l'application ; un fichier qui le fait planter
  à nouveau est signalé en échec. Les images de plus de 100 millions de pixels sont refusées.
- **Table de correspondance** : créée lisible par son seul propriétaire (macOS, Linux) ; des
  paramètres de dérivation aberrants la font refuser comme endommagée.
- **Journaux et rapports** : `data/logs/` et les rapports ne contiennent ni valeur
  sensible ni nom de fichier d'origine (seulement des noms anonymisés, des nombres et des
  codes).
- **Effacement des temporaires** par suppression simple : sur SSD, un effacement
  « sécurisé » n'est pas garanti ; chiffrez le disque pour les données les plus sensibles.

## Limites connues

- La détection automatique n'est jamais parfaite : relisez les fichiers marqués
  « À revoir » et les résidus signalés. Le mode paranoïaque réduit les oublis au prix de
  davantage de faux positifs.
- La ré-identification de fichiers ne traite que les formats texte ; pour les autres
  formats, utilisez `--balise`.
- Seuls les dépôts Hugging Face publics sont pris en charge (aucun jeton n'est envoyé).
- Le contrôle des octets bruts ne cherche que les identifiants (personnes, contacts,
  numéros, termes métier) d'au moins 6 caractères : les organisations, lieux et dates
  figurent légitimement dans la structure des formats binaires. Le contenu des archives
  7z est contrôlé élément par élément après extraction.
- macOS : application non signée (voir Gatekeeper ci-dessus), prévue pour les Mac
  Apple Silicon uniquement.
- Windows : gardez le chemin du dossier portable sous une centaine de caractères
  (par exemple `C:\Outils\Anonyma`). Au-delà, LibreOffice ne peut plus créer son profil
  temporaire et les `.doc`, `.xls` et `.ppt` échouent (fichiers signalés en échec, jamais
  produits non anonymisés).
- macOS : la copie de LibreOffice embarquée est l'application officielle signée de The
  Document Foundation ; ne modifiez pas son contenu, sa signature serait invalidée.

## Comportements à connaître

- **Dans le doute, Anonyma masque** : une seule détection suffit ; les entités
  incertaines sont masquées *et* proposées en revue, jamais laissées visibles.
- **« À revoir » n'est pas « Résidus »** : une valeur d'origine retrouvée met le fichier
  en quarantaine ; un simple signalement des modèles d'IA (GLiNER, modèle de langage) le
  marque seulement « À revoir », car ils réagissent parfois aux balises elles-mêmes.
- **Liste blanche stricte** : rien n'est masqué à l'intérieur d'une valeur de la liste
  blanche (un courriel conservé reste intact même si son identifiant est masqué ailleurs).
- **Rien n'est recopié tel quel** : un élément non anonymisable est omis de la sortie et
  toujours signalé dans le rapport.
- **Commentaires bureautiques conservés et anonymisés** (auteur « Anonyme ») ; les
  révisions suivies sont acceptées (les suppressions disparaissent entièrement).
- **PST restitué en dossier d'eml** : aucun outil libre ne sait réécrire un PST de façon
  fiable.

## Licences

Voir [LICENSES.md](LICENSES.md) : licences de chaque dépendance et des modèles du
catalogue.

Pour travailler sur le code (sources, tests, construction) : voir
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).
