"""French regex recognizers (LIGHT profile): positives, negatives, checksums."""

from __future__ import annotations

import pytest

from anonyma import checksums
from anonyma.config import Config
from anonyma.detectors.regex_fr import RegexDetector
from anonyma.models import Entity, Segment, SegmentContext, SegmentKind


@pytest.fixture(scope="module")
def detector() -> RegexDetector:
    return RegexDetector(Config(case_number_patterns=[r"\bAFF-\d{4}-\d{3}\b"]))


def detect(
    detector: RegexDetector,
    text: str,
    *,
    kind: SegmentKind = SegmentKind.BODY,
    label: str = "",
) -> list[Entity]:
    return detector.detect([Segment("s", text, (), SegmentContext(kind, label))], "d")


def found(detector: RegexDetector, text: str, entity_type: str, **kw: object) -> list[str]:
    return [e.value for e in detect(detector, text, **kw) if e.entity_type == entity_type]  # type: ignore[arg-type]


# -- generated identifiers with valid check digits (never real ones) -------------


def make_iban_fr(account: str = "00012345678") -> str:
    bank, branch = "30001", "00794"
    bban = bank + branch + account + checksums.rib_key(bank, branch, account)
    return "FR" + checksums.iban_check_digits("FR", bban) + bban


def make_luhn(prefix: str, length: int) -> str:
    body = prefix + "7" * (length - len(prefix) - 1)
    return body + checksums.luhn_check_digit(body)


def make_nir(body13: str) -> str:
    key = checksums.nir_key(body13)
    assert key is not None
    return body13 + key


def spaced(value: str, groups: tuple[int, ...]) -> str:
    out, pos = [], 0
    for g in groups:
        out.append(value[pos : pos + g])
        pos += g
    return " ".join(out)


# -- checksums -------------------------------------------------------------------


def test_checksum_helpers() -> None:
    iban = make_iban_fr()
    assert checksums.iban_valid(iban)
    assert not checksums.iban_valid(iban[:-1] + ("0" if iban[-1] != "0" else "1"))
    assert checksums.iban_valid("DE89 3704 0044 0532 0130 00")  # ISO 13616 sample
    assert checksums.luhn_valid(make_luhn("4970", 16))
    assert not checksums.luhn_valid("4970101234567890")
    siren = make_luhn("73282", 9)
    assert checksums.siren_valid(siren)
    assert checksums.siren_valid(checksums.LA_POSTE_SIREN)
    assert checksums.siret_valid("35600000000047")  # La Poste: digit sum multiple of 5
    assert not checksums.siret_valid("35600000000048")
    assert checksums.vat_fr_valid("FR" + checksums.vat_fr_key(siren) + siren)
    nir = make_nir("1850478006084")
    assert checksums.nir_valid(nir)
    assert not checksums.nir_valid(nir[:-2] + "00")
    corsica_a, corsica_b = make_nir("185042A006084"), make_nir("185042B006084")
    assert checksums.nir_valid(corsica_a) and checksums.nir_valid(corsica_b)
    assert corsica_a[-2:] != corsica_b[-2:]


# -- contact data ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "écrire à jean.dupont@exemple.test demain",
        "contact: J.Dupont+rh@sous.exemple.test.",
        "jean.dupont (at) exemple (dot) test",
        "jean.dupont[at]exemple.test",
    ],
)
def test_email(detector: RegexDetector, text: str) -> None:
    assert found(detector, text, "EMAIL")


@pytest.mark.parametrize(
    "phone",
    [
        "06 12 34 56 78",
        "06.12.34.56.78",
        "06-12-34-56-78",
        "0612345678",
        "+33 6 12 34 56 78",
        "+33 (0)6 12 34 56 78",
        "+33612345678",
        "0033 6 12 34 56 78",
        "01 23 45 67 89",
        "+33 1 23 45 67 89",
        "+262 692 12 34 56",
    ],
)
def test_phone_fr(detector: RegexDetector, phone: str) -> None:
    assert found(detector, f"Tél. : {phone}.", "TELEPHONE") == [phone]


@pytest.mark.parametrize("phone", ["+49 30 12345678", "+1 415 555 2671", "+44 20 7946 0958"])
def test_phone_international(detector: RegexDetector, phone: str) -> None:
    assert found(detector, f"Appeler le {phone} demain", "TELEPHONE") == [phone]


@pytest.mark.parametrize("text", ["00 12 34 56 78", "réf 1234567890123", "n° 0612345"])
def test_phone_negatives(detector: RegexDetector, text: str) -> None:
    assert not found(detector, text, "TELEPHONE")


# -- banking -------------------------------------------------------------------------


def test_iban(detector: RegexDetector) -> None:
    iban = make_iban_fr()
    grouped = " ".join(iban[i : i + 4] for i in range(0, len(iban), 4))
    assert found(detector, f"IBAN : {grouped}", "IBAN") == [grouped]
    assert found(detector, f"virement sur {iban}.", "IBAN") == [iban]
    # Trailing words are not swallowed.
    assert found(detector, f"{grouped} BIC AGRIFRPP", "IBAN") == [grouped]


def test_iban_bad_checksum_kept_only_with_context(detector: RegexDetector) -> None:
    iban = make_iban_fr()
    bad = iban[:4] + ("1" if iban[4] != "1" else "2") + iban[5:]
    assert not found(detector, f"code {bad} produit", "IBAN")
    hits = [e for e in detect(detector, f"IBAN : {bad}") if e.entity_type == "IBAN"]
    assert hits and hits[0].score < 0.6  # sent to review


def test_bic_needs_context(detector: RegexDetector) -> None:
    assert found(detector, "BIC : AGRIFRPP882", "BIC") == ["AGRIFRPP882"]
    assert not found(detector, "Le mot ABCDEFGH seul", "BIC")


def test_credit_card(detector: RegexDetector) -> None:
    card = make_luhn("4970", 16)
    assert found(detector, f"carte {spaced(card, (4, 4, 4, 4))} exp", "CARTE_BANCAIRE")
    assert found(detector, f"n° {card}", "CARTE_BANCAIRE") == [card]
    assert not found(detector, "commande 4970101234567890", "CARTE_BANCAIRE")


# -- French identifiers ----------------------------------------------------------------


def test_nir(detector: RegexDetector) -> None:
    nir = make_nir("2850478006084")
    text = f"N° de sécurité sociale : {spaced(nir, (1, 2, 2, 2, 3, 3, 2))}"
    hits = [e for e in detect(detector, text) if e.entity_type == "NIR"]
    assert hits and hits[0].score == 1.0
    corsica = make_nir("185042A006084")
    assert found(detector, f"NIR {corsica}", "NIR") == [corsica]
    temporary = make_nir("7850478006084")
    assert found(detector, f"numéro {temporary}", "NIR") == [temporary]


def test_nir_bad_key(detector: RegexDetector) -> None:
    nir = make_nir("1850478006084")
    bad = nir[:-2] + f"{(int(nir[-2:]) + 1) % 97:02d}"
    assert not found(detector, f"commande {bad}", "NIR")
    assert found(detector, f"sécurité sociale {bad}", "NIR")


def test_siren_siret_vat(detector: RegexDetector) -> None:
    siren = make_luhn("73282", 9)
    nic_body = siren + "0007"
    siret = nic_body + checksums.luhn_check_digit(nic_body)
    vat = "FR" + checksums.vat_fr_key(siren) + siren
    text = f"SIREN {spaced(siren, (3, 3, 3))}, SIRET {siret}, TVA {vat}"
    assert found(detector, text, "SIREN") == [spaced(siren, (3, 3, 3))]
    assert found(detector, text, "SIRET") == [siret]
    assert found(detector, text, "TVA") == [vat]
    assert found(detector, "La Poste SIRET 35600000000047", "SIRET") == ["35600000000047"]
    assert found(detector, "TVA DE123456789", "TVA") == ["DE123456789"]


def test_siren_luhn_failure_dropped_without_context(detector: RegexDetector) -> None:
    siren = make_luhn("73282", 9)
    bad = siren[:-1] + str((int(siren[-1]) + 1) % 10)
    assert not found(detector, f"lot {bad}", "SIREN")


def test_plates(detector: RegexDetector) -> None:
    assert found(detector, "véhicule AB-123-CD garé", "PLAQUE") == ["AB-123-CD"]
    assert found(detector, "immatriculation 1234 AB 75", "PLAQUE") == ["1234 AB 75"]
    (odd,) = [e for e in detect(detector, "code AI-123-OU") if e.entity_type == "PLAQUE"]
    assert odd.score < 0.85  # letters SIV never uses: kept, but less confident
    assert not found(detector, "voir 1234 AB 75", "PLAQUE")  # FNI needs context


# -- places ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Il habite 12 rue des Lilas, 75011 Paris.", "12 rue des Lilas, 75011 Paris"),
        ("au 3 bis avenue Victor Hugo depuis", "3 bis avenue Victor Hugo"),
        ("8, bd de la République\n69002 Lyon", "8, bd de la République\n69002 Lyon"),
        (
            "Résidence : 45 chemin du Moulin 44000 NANTES CEDEX",
            "45 chemin du Moulin 44000 NANTES CEDEX",
        ),
    ],
)
def test_address(detector: RegexDetector, text: str, expected: str) -> None:
    assert expected in found(detector, text, "ADRESSE")


def test_postcode_and_city(detector: RegexDetector) -> None:
    assert found(detector, "à 44000 Nantes", "ADRESSE") == ["44000 Nantes"]
    assert not found(detector, "un budget de 15000 euros", "ADRESSE")
    assert found(detector, "code postal : 44000", "CODE_POSTAL") == ["44000"]
    assert not found(detector, "il reste 44000 unités", "CODE_POSTAL")


def test_birth_place(detector: RegexDetector) -> None:
    assert found(detector, "né à Saint-Malo en 1980", "LIEU") == ["Saint-Malo"]
    assert found(detector, "domiciliée à Nantes", "LIEU") == ["Nantes"]


# -- network -------------------------------------------------------------------------


def test_ip_addresses(detector: RegexDetector) -> None:
    assert found(detector, "serveur 192.168.10.20.", "IP") == ["192.168.10.20"]
    assert found(detector, "hôte 2001:db8:85a3::8a2e:370:7334 ok", "IP") == [
        "2001:db8:85a3::8a2e:370:7334"
    ]
    assert not found(detector, "version 999.1.1.1 et 127.0.0.1", "IP")
    assert not found(detector, "à 12:30:45", "IP")
    # Software version written by LibreOffice in its "Generator" metadata.
    assert not found(detector, "LibreOffice/24.2.7.2$Linux_X86_64 LibreOffice_project", "IP")
    assert found(detector, "voir http://10.20.30.40/admin", "IP") == ["10.20.30.40"]
    assert found(detector, "IP : 10.20.30.40/24", "IP") == ["10.20.30.40/24"]


def test_url_and_domains(detector: RegexDetector) -> None:
    assert found(detector, "voir (https://exemple.test/u/jean-dupont).", "URL") == [
        "https://exemple.test/u/jean-dupont"
    ]
    assert found(detector, "site www.dupont-avocats.fr, merci", "URL") == ["www.dupont-avocats.fr"]
    assert found(detector, "sur dupont-avocats.fr", "URL") == ["dupont-avocats.fr"]
    assert not found(detector, "rapport.pdf", "URL", kind=SegmentKind.FILENAME)
    assert not found(detector, "jean@dupont-avocats.fr", "URL")


def test_usernames(detector: RegexDetector) -> None:
    assert found(detector, "login : jdupont42", "IDENTIFIANT") == ["jdupont42"]
    assert found(detector, r"C:\Users\jdupont\Documents", "IDENTIFIANT") == ["jdupont"]
    assert found(detector, "/home/jdupont/notes", "IDENTIFIANT") == ["jdupont"]
    assert found(detector, "suivez @jean_dupont sur le réseau", "IDENTIFIANT") == ["@jean_dupont"]


def test_message_id_by_label_and_body(detector: RegexDetector) -> None:
    mid = "<CAF1234abcd.5678@mail.exemple.test>"
    assert found(detector, mid, "ID_MESSAGE", kind=SegmentKind.HEADER, label="Message-ID") == [mid]
    assert found(detector, f"voir {mid}", "ID_MESSAGE") == [mid]


def test_angle_address_is_not_a_message_id(detector: RegexDetector) -> None:
    """A digit in the domain once made ``<address>`` a message id in the From header:
    the eml then got another substitute than the JSON and CSV of the same e-mail."""
    text = "Jessica Morin <jessica.morin@leads-b2b.exemple.test>"
    for kind, label in ((SegmentKind.HEADER, "From"), (SegmentKind.BODY, "")):
        assert found(detector, text, "ID_MESSAGE", kind=kind, label=label) == []
        assert found(detector, text, "EMAIL", kind=kind, label=label) == [
            "jessica.morin@leads-b2b.exemple.test"
        ]
    numbered = "<jean.dupont75@exemple2.test>"
    assert found(detector, numbered, "ID_MESSAGE", kind=SegmentKind.HEADER, label="To") == []


# -- dates -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "né le 03/04/1985 à Lyon",
        "née le 1er mars 1985",
        "Date de naissance : 1985-04-03",
        "DDN 03.04.85",
        "born March 3, 1985",
    ],
)
def test_date_of_birth(detector: RegexDetector, text: str) -> None:
    assert found(detector, text, "DATE_NAISSANCE")


def test_other_dates_and_sentence_boundary(detector: RegexDetector) -> None:
    text = "Elle est née le 03/04/1985. Réunion le 12 mars 2021."
    assert found(detector, text, "DATE_NAISSANCE") == ["03/04/1985"]
    assert found(detector, text, "DATE") == ["12 mars 2021"]
    quiet = RegexDetector(Config(detect_dates=False))
    assert not found(quiet, text, "DATE")
    assert found(quiet, text, "DATE_NAISSANCE") == ["03/04/1985"]


def test_case_numbers(detector: RegexDetector) -> None:
    assert found(detector, "dossier AFF-2023-042 clos", "NUMERO_DOSSIER") == ["AFF-2023-042"]
    assert found(detector, "Dossier n° 2023/AB-1234.", "NUMERO_DOSSIER") == ["2023/AB-1234"]
    assert not found(detector, "dossier : en cours", "NUMERO_DOSSIER")
    assert not found(detector, "Clients/CV.pdf", "NUMERO_DOSSIER", kind=SegmentKind.FILENAME)


def test_invalid_case_pattern_is_ignored() -> None:
    RegexDetector(Config(case_number_patterns=["(unclosed"]))


# -- persons and organisations ------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Rendez-vous avec M. Dupont demain", "Dupont"),
        ("Mme Claire FONTAINE a signé", "Claire FONTAINE"),
        ("Maître Benoît de La Fontaine plaide", "Benoît de La Fontaine"),
        ("Dr J. Martin reçoit", "J. Martin"),
        ("signé Jean-Pierre Lemaire, gérant", "Jean-Pierre Lemaire"),
        ("le dossier de DUPONT Jean est clos", "DUPONT Jean"),
        ("JEAN DUPONT", "JEAN DUPONT"),
    ],
)
def test_person(detector: RegexDetector, text: str, expected: str) -> None:
    assert expected in found(detector, text, "PERSONNE")


def test_person_civility_left_outside_with_gender(detector: RegexDetector) -> None:
    (hit,) = [e for e in detect(detector, "Mme Lefèvre") if e.entity_type == "PERSONNE"]
    assert hit.value == "Lefèvre" and hit.attributes["gender"] == "f"


@pytest.mark.parametrize(
    "text",
    ["Madame la Présidente ouvre la séance", "Bonjour Madame,", "Le Directeur Général"],
)
def test_person_negatives(detector: RegexDetector, text: str) -> None:
    assert not found(detector, text, "PERSONNE")


def test_person_in_file_name(detector: RegexDetector) -> None:
    hits = found(detector, "Clients/CV_Jean_Dupont_2023.pdf", "PERSONNE", kind=SegmentKind.FILENAME)
    assert hits == ["Jean_Dupont"]


def test_mail_header_and_metadata_labels(detector: RegexDetector) -> None:
    header = '"Dupont, Jean" <jean.dupont@exemple.test>, Paul Martin <pm@exemple.test>'
    names = found(detector, header, "PERSONNE", kind=SegmentKind.HEADER, label="To")
    assert "Dupont, Jean" in names and "Paul Martin" in names
    meta = found(
        detector, "Bérénice Quintal", "PERSONNE", kind=SegmentKind.METADATA, label="dc:creator"
    )
    assert meta == ["Bérénice Quintal"]
    for software in ("pdfmake", "wkhtmltopdf", "iText"):
        assert not found(detector, software, "PERSONNE", kind=SegmentKind.METADATA, label="creator")
    org = found(
        detector, "Exemple Industries", "ORGANISATION", kind=SegmentKind.METADATA, label="Company"
    )
    assert org == ["Exemple Industries"]
    # "nom de fichier" must not be read as the French "De:" header.
    assert not detect(detector, "journal.log", kind=SegmentKind.FILENAME, label="nom de fichier")


def test_organisations(detector: RegexDetector) -> None:
    assert found(detector, "la société Lemaire Conseil SARL facture", "ORGANISATION")
    assert found(detector, "le Cabinet Fontaine. Le lendemain", "ORGANISATION") == ["Fontaine"]


def test_scores_within_bounds(detector: RegexDetector) -> None:
    text = "M. Jean Dupont, 06 12 34 56 78, jean.dupont@exemple.test, 12 rue des Lilas"
    for e in detect(detector, text):
        assert 0.0 < e.score <= 1.0
        assert e.value == text[e.start : e.end]


def test_detection_never_uses_the_network() -> None:
    import socket

    from anonyma import netguard

    netguard.install()
    detector = RegexDetector(Config())
    text = "Voir https://www.exemple.fr/a et écrire à jean.dupont@exemple.fr ou dupont-avocats.fr"
    assert {e.entity_type for e in detect(detector, text)} >= {"URL", "EMAIL"}
    with pytest.raises(netguard.NetworkBlockedError):
        socket.create_connection(("192.0.2.1", 80), timeout=0.1)


# -- leak classes found by the benchmark (tests/benchmark) ------------------------


IBAN_LAYOUTS = {
    "rib": lambda i: f"{i[:4]} {i[4:9]} {i[9:14]} {i[14:25]} {i[25:]}",
    "tirets": lambda i: "-".join(i[k : k + 4] for k in range(0, len(i), 4)),
    "doubles espaces": lambda i: "  ".join(i[k : k + 4] for k in range(0, len(i), 4)),
    "points": lambda i: ".".join(i[k : k + 4] for k in range(0, len(i), 4)),
}


@pytest.mark.parametrize("layout", list(IBAN_LAYOUTS))
def test_iban_any_grouping(detector: RegexDetector, layout: str) -> None:
    written = IBAN_LAYOUTS[layout](make_iban_fr("12345678901"))
    assert found(detector, f"Compte {written} et sa date", "IBAN") == [written]


def test_iban_invalid_candidate_stays_bounded(detector: RegexDetector) -> None:
    iban = make_iban_fr("12345678901")
    broken = iban[:-1] + ("0" if iban[-1] != "0" else "1")
    grouped = " ".join(broken[k : k + 4] for k in range(0, len(broken), 4))
    values = found(detector, f"virement sur le compte {grouped} et sa date", "IBAN")
    assert values == [grouped]


def test_nir_with_dots_and_glued_word(detector: RegexDetector) -> None:
    nir = make_nir("2811013742367")
    dotted = ".".join([nir[0], nir[1:3], nir[3:5], nir[5:7], nir[7:10], nir[10:13], nir[13:]])
    assert found(detector, f"NIR {dotted}", "NIR") == [dotted]
    # OCR glues words: the key makes the match safe even after letters.
    glued = f"numérodesécuritésociale{nir[:10]} {nir[10:]},"
    assert found(detector, glued, "NIR") == [f"{nir[:10]} {nir[10:]}"]


def test_phone_variants(detector: RegexDetector) -> None:
    assert found(detector, "tél 06/39/98/55/08", "TELEPHONE") == ["06/39/98/55/08"]
    # Ofcom fiction range: a possible number, not an allocated one.
    assert found(detector, "mobile +44 7700 900456", "TELEPHONE") == ["+44 7700 900456"]
    assert found(detector, "33639984697", "TELEPHONE", kind=SegmentKind.CELL) == ["33639984697"]
    assert found(detector, "telephone 33639984697", "TELEPHONE") == ["33639984697"]
    assert found(detector, "le lot 33639984697 du dossier", "TELEPHONE") == []


def test_us_date_of_birth(detector: RegexDetector) -> None:
    assert found(detector, "date of birth 03/14/1985", "DATE_NAISSANCE") == ["03/14/1985"]


def test_address_complement_and_english_order(detector: RegexDetector) -> None:
    assert found(detector, "Bât. C, appartement 34", "ADRESSE") == ["Bât. C, appartement 34"]
    assert found(detector, "esc. 2, 3e étage", "ADRESSE") == ["esc. 2, 3e étage"]
    assert found(detector, "Le bâtiment est neuf.", "ADRESSE") == []
    text = "Home address: 221 Larkspur Crescent, BS1 4XE Bristol"
    assert found(detector, text, "ADRESSE") == ["221 Larkspur Crescent"]


def test_address_wrapped_in_a_pdf_block(detector: RegexDetector) -> None:
    text = "Adresse : 8\nquai des\nMariniers-d'Antan, 33000\nBordeaux"
    assert found(detector, text, "ADRESSE") == ["8\nquai des\nMariniers-d'Antan, 33000\nBordeaux"]
    # OCR glues the postcode and the city.
    assert found(detector, "37000Tours", "ADRESSE", kind=SegmentKind.OCR) == ["37000Tours"]
    assert found(detector, "Total 25000Euros", "ADRESSE") == []


def test_person_in_file_name_leaves_extension_dot(detector: RegexDetector) -> None:
    values = found(detector, "CV Ophélie Gauthier.txt", "PERSONNE", kind=SegmentKind.FILENAME)
    assert values == ["Ophélie Gauthier"]


def test_column_and_key_hints(detector: RegexDetector) -> None:
    cell = {"kind": SegmentKind.CELL, "label": "C4 (colonne nom)"}
    assert found(detector, "LEBRUN", "PERSONNE", **cell) == ["LEBRUN"]
    login = found(detector, "jbkowal88", "IDENTIFIANT", label="$.*[0].* (champ identifiant)")
    assert login == ["jbkowal88"]
    assert found(detector, "Metz", "LIEU", label="$.*.* (champ ville)") == ["Metz"]
    # The hint word elsewhere in a label means nothing.
    assert not found(detector, "Synthèse", "PERSONNE", label="nom de feuille")
    # Below the verifier's certainty: a misleading header sends to review, not quarantine.
    assert all(e.score < 0.8 for e in detect(detector, "LEBRUN", **cell))  # type: ignore[arg-type]


def test_person_span_stops_at_form_labels(detector: RegexDetector) -> None:
    """« NOM : X   Prénom : Y »: the label is never swallowed, each value is found and
    tagged with the name part its label gives (the resolver pairs them)."""
    hits = [
        e
        for e in detect(detector, "NOM : TOURNESOL   Prénom : Ysaline")
        if e.entity_type == "PERSONNE"
    ]
    assert sorted(e.value for e in hits) == ["TOURNESOL", "Ysaline"]
    parts = {e.value: e.attributes.get("field") for e in hits}
    assert parts == {"TOURNESOL": "nom", "Ysaline": "prenom"}
    values = found(detector, "Nom de famille : Quintavalle  Né le 03/04/1985", "PERSONNE")
    assert values == ["Quintavalle"]


def test_person_span_stops_at_sentence_end_and_next_civility(detector: RegexDetector) -> None:
    text = "Entretien avec M. Braquemart. Mme Vasseur-Delmas absente. Vu M. Durand Mme Petit."
    values = found(detector, text, "PERSONNE")
    assert "Braquemart" in values and "Vasseur-Delmas" in values
    assert not any("." in v or "Mme" in v for v in values), values
    # An initial keeps its period and stays in the name.
    assert "J. Dupont" in found(detector, "Signé par J. Dupont.", "PERSONNE")


def test_hyphenated_handle_is_one_identifier(detector: RegexDetector) -> None:
    assert found(detector, "suivez @jean-baptiste sur le réseau", "IDENTIFIANT") == [
        "@jean-baptiste"
    ]


def test_windows_profile_folder_with_spaces(detector: RegexDetector) -> None:
    path = r"C:\Users\Zéphyrin Quintavalle\Documents\cv.txt"
    assert found(detector, path, "PERSONNE") == ["Zéphyrin Quintavalle"]


def test_value_next_to_a_field_name_cell(detector: RegexDetector) -> None:
    """Vertical layout « Nom | Zéphyrin Quintavalle »: the label cell types its neighbour."""

    def cell(ref: str, text: str) -> Segment:
        return Segment(ref, text, (), SegmentContext(SegmentKind.CELL, f"feuille 1!{ref}"))

    segments = [
        cell("A1", "Nom"),
        cell("B1", "Zéphyrin Quintavalle"),
        cell("A2", "Service"),
        cell("B2", "Comptabilité générale"),
        cell("C1", "Zéphyrin Quintavalle"),  # not next to the label
    ]
    persons = [
        (e.segment_id, e.value)
        for e in detector.detect(segments, "d")
        if e.entity_type == "PERSONNE"
    ]
    assert ("B1", "Zéphyrin Quintavalle") in persons
    assert all(seg != "B2" for seg, _ in persons)


def test_header_row_cells_are_not_values(detector: RegexDetector) -> None:
    def cell(ref: str, text: str) -> Segment:
        return Segment(ref, text, (), SegmentContext(SegmentKind.CELL, ref))

    header = [cell("A1", "nom"), cell("B1", "prenom"), cell("C1", "note"), cell("D1", "cp")]
    assert not detector.detect(header, "d")


def test_label_after_other_header_cells_types_nothing(detector: RegexDetector) -> None:
    def cell(ref: str, text: str) -> Segment:
        return Segment(ref, text, (), SegmentContext(SegmentKind.CELL, ref))

    assert not detector.detect([cell("A1", "date"), cell("B1", "auteur"), cell("C1", "note")], "d")
