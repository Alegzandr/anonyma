"""csv / tsv / json / xml: span edits, dialect and encoding kept, XXE safety."""

from __future__ import annotations

import codecs
import json
from pathlib import Path

from anonyma.config import Config
from anonyma.extractors._tabular import (
    quote_csv,
    sniff_delimiter,
    tokenize_csv,
    tokenize_json,
)
from anonyma.models import DocumentStatus
from anonyma.pipeline import extract_file
from tests.canary import CANARY_PERSON
from tests.office.conftest import run_pipeline

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "contract"


def test_csv_tokenizer_spans() -> None:
    text = 'a;"b;""c"""\r\n"multi\nligne";\n'
    fields = tokenize_csv(text, ";")
    assert [(f.row, f.col, f.value, f.quoted) for f in fields] == [
        (0, 0, "a", False),
        (0, 1, 'b;"c"', True),
        (1, 0, "multi\nligne", True),
        (1, 1, "", False),
    ]
    assert text[fields[1].start : fields[1].end] == '"b;""c"""'
    assert sniff_delimiter("nom;prenom\nx;y\n") == ";"
    assert quote_csv("a,b", ",", False) == '"a,b"'
    assert quote_csv('a"b', ";", False) == '"a""b"'
    assert quote_csv("ab", ";", True) == '"ab"'


def test_csv_dialect_and_bom_kept(tmp_path: Path, canary_config: Config) -> None:
    _, output = run_pipeline(FIXTURES / "contacts.csv", tmp_path, canary_config)
    data = output.read_bytes()
    assert data.startswith(codecs.BOM_UTF8)
    text = data[3:].decode("utf-8")
    assert text.startswith("Nom;Courriel;Note\r\n")
    assert '"[PERSONNE_001]";[EMAIL_001];"Rappeler ; [PERSONNE_001]"\r\n' in text


def test_tsv_encoding_kept(tmp_path: Path, canary_config: Config) -> None:
    _, output = run_pipeline(FIXTURES / "export.tsv", tmp_path, canary_config)
    text = output.read_bytes().decode("cp1252")
    assert text == "nom\tcourriel\n[PERSONNE_001]\t[EMAIL_001]\nM. [PERSONNE_001]\t\n"


def test_json_tokens_cover_keys_values_numbers() -> None:
    text = '{"a": "x", "Zé": [1, 612345678, "\\u00e9"], "n": null}\n{"b": 2}'
    tokens = tokenize_json(text)
    assert [(t.kind, t.value) for t in tokens] == [
        ("key", "a"),
        ("string", "x"),
        ("key", "Zé"),
        ("number", "1"),
        ("number", "612345678"),
        ("string", "é"),
        ("key", "n"),
        ("key", "b"),
        ("number", "2"),
    ]
    assert tokens[5].ascii_escaped
    assert all("Zé" not in t.path for t in tokens)


def test_json_output(tmp_path: Path, canary_config: Config) -> None:
    config = Config(
        profile=canary_config.profile,
        workers=1,
        always_mask=[*canary_config.always_mask, {"value": "612345678", "type": "TELEPHONE"}],
    )
    _, output = run_pipeline(FIXTURES / "dossier.json", tmp_path, config)
    text = output.read_text(encoding="utf-8")
    data = json.loads(text)
    dossier = data["dossier"]
    assert dossier["contact"]["telephone"] == "[TELEPHONE_001]"
    assert "[PERSONNE_001]" in dossier
    assert '"Cr\\u00e9\\u00e9 par [PERSONNE_001]"' in text  # escaping style kept
    assert text.startswith('{\n  "dossier": {\n')  # indentation kept


def test_invalid_json_fails(tmp_path: Path, canary_config: Config) -> None:
    source = tmp_path / "bad.json"
    source.write_text('{"a": ', encoding="utf-8")
    doc = extract_file(source, source.name, canary_config, tmp_path / "w")
    assert doc.status is DocumentStatus.FAILED


def test_xml_output_keeps_structure(tmp_path: Path, canary_config: Config) -> None:
    _, output = run_pipeline(FIXTURES / "dossier.xml", tmp_path, canary_config)
    data = output.read_bytes()
    assert data.startswith(b"<?xml version='1.0' encoding='ISO-8859-1'?>")
    text = data.decode("iso-8859-1")
    # Internal entities expanded, internal subset dropped.
    assert "<!DOCTYPE" not in text and "&org;" not in text
    assert "<x:organisation>[ORGANISATION_001]</x:organisation>" in text
    assert "<![CDATA[Vu avec <b>[PERSONNE_001]</b> & co]]>" in text
    assert 'auteur="[PERSONNE_001]"' in text
    assert '<?traitement responsable="[PERSONNE_001]"?>' in text
    assert "<!-- Export réalisé par [PERSONNE_001] -->" in text
    assert CANARY_PERSON not in text


def test_xml_external_entities_are_not_resolved(tmp_path: Path, canary_config: Config) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("CONTENU_SECRET", encoding="utf-8")
    source = tmp_path / "xxe.xml"
    source.write_text(
        f'<?xml version="1.0"?>\n<!DOCTYPE r [<!ENTITY xxe SYSTEM "{secret.as_uri()}">\n'
        '<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>\n<r><x>&xxe;</x><y>&c;</y></r>',
        encoding="utf-8",
    )
    doc = extract_file(source, source.name, canary_config, tmp_path / "w")
    assert doc.status is DocumentStatus.EXTRACTED
    joined = " ".join(s.text for s in doc.segments)
    assert "CONTENU_SECRET" not in joined
    assert "a" * 1000 in joined  # internal entities are expanded (and exposed)


def test_xml_entity_bomb_fails(tmp_path: Path, canary_config: Config) -> None:
    levels = "".join(f'<!ENTITY e{i} "{f"&e{i - 1};" * 10}">' for i in range(1, 8))
    source = tmp_path / "bombe.xml"
    source.write_text(
        f'<?xml version="1.0"?>\n<!DOCTYPE r [<!ENTITY e0 "aaaaaaaaaa">{levels}]>\n<r>&e7;</r>',
        encoding="utf-8",
    )
    doc = extract_file(source, source.name, canary_config, tmp_path / "w")
    assert doc.status is DocumentStatus.FAILED
