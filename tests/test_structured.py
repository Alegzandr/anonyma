"""Structured exports (Dataverse / OData JSON, CSV, XML): embedded files, structural
keys, atomic identifiers and residue locations. Synthetic data only."""

from __future__ import annotations

import base64
import io
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from anonyma.atomic import atomic_spans, trimmed
from anonyma.config import Config
from anonyma.extractors._embedded import (
    MARK,
    Base64Style,
    Hint,
    find_blobs,
    hint_from,
    parse_base64,
)
from anonyma.models import (
    Document,
    DocumentStatus,
    Entity,
    Segment,
    SegmentContext,
    SegmentKind,
)
from anonyma.pipeline import Pipeline, extract_file
from anonyma.rawscan import EMBEDDED_TYPE, RawScanner, raw_residues
from anonyma.replacer import Replacer, build_plan
from anonyma.resolver import resolve
from anonyma.structure import is_structural, is_structural_token, vocabulary
from anonyma.verifier import place
from tests.canary import CANARY_EMAIL, CANARY_PERSON
from tests.fixtures.generators.office import (
    DATAVERSE_ACTIVITY,
    DATAVERSE_INDEX,
    dataverse_page,
)

GUID = "34be8221-5b2a-4e8a-b93c-e8b15d765216"


@pytest.fixture
def config(canary_config: Config) -> Config:
    """The consultant's run: identifiers kept (``--conserver-identifiants``)."""
    return replace(canary_config, mask_opaque_ids=False)


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buffer, "PNG")
    return buffer.getvalue()


def _run(config: Config, source: Path, out: Path) -> tuple[Pipeline, object]:
    pipeline = Pipeline(config)
    analysis = pipeline.analyze([source])
    result = pipeline.apply(analysis, out)
    pipeline.close(analysis)
    return pipeline, result


def _keys(value: object) -> list[str]:
    if isinstance(value, dict):
        return [k for k, v in value.items() for k in (k, *_keys(v))]
    if isinstance(value, list):
        return [k for item in value for k in _keys(item)]
    return []


# ---------------------------------------------------------------------------
# Base64 values
# ---------------------------------------------------------------------------


def test_base64_layout_is_read_and_written_back() -> None:
    data = os.urandom(300)
    wrapped = base64.encodebytes(data).decode().replace("\n", "\r\n")
    parsed = parse_base64(wrapped)
    assert parsed is not None
    decoded, style = parsed
    assert decoded == data
    assert (style.line, style.newline, style.trailing_newline) == (76, "\r\n", True)
    assert style.encode(data) == wrapped
    unpadded = base64.urlsafe_b64encode(b"\xfb\xff\xfe-x").decode().rstrip("=")
    parsed = parse_base64(unpadded)
    assert parsed is not None and parsed[1].urlsafe and not parsed[1].padded
    assert parsed[1].encode(parsed[0]) == unpadded


def test_values_that_are_not_files() -> None:
    for value in (
        GUID,
        DATAVERSE_INDEX,
        "Bonjour",
        'W/"83736425"',
        "abcdefghijklmnopqrstuvwxyzabcdef",
        base64.b64encode(os.urandom(30)).decode(),  # a short opaque token
    ):
        assert find_blobs(value) == [], value
    for value in (GUID, DATAVERSE_INDEX, "Bonjour", "abcdefghijklmnopqrstuvwxyzabcdef"):
        assert find_blobs(value, Hint(strong=True)) == [], value


def test_files_in_values() -> None:
    png = _png()
    (blob,) = find_blobs(base64.b64encode(png).decode())
    assert blob.data == png and blob.suffix == ".png"
    text = base64.b64encode(f"Bonjour {CANARY_PERSON}".encode()).decode()
    assert find_blobs(text) == []  # short text without hint: an identifier
    (blob,) = find_blobs(text, hint_from("body", {"filename": "note.txt"}))
    assert blob.suffix == ".txt"
    html = f'<p>Logo</p><img src="data:image/png;base64,{base64.b64encode(png).decode()}">'
    (blob,) = find_blobs(html)
    assert html[blob.start : blob.end].startswith("data:image/png;base64,")
    assert blob.style.prefix == "data:image/png;base64,"
    assert hint_from("documentbody", {}).strong
    assert hint_from("x", {"mimetype": "application/pdf"}).mime == "application/pdf"


def test_dataverse_attachment_is_anonymized(config: Config, tmp_path: Path) -> None:
    note = f"Rédigé par {CANARY_PERSON} ({CANARY_EMAIL}).\r\n" * 3
    page = dataverse_page(note.encode(), "compte-rendu.txt", "text/plain")
    source = tmp_path / "in" / "emails_page_001.json"
    source.parent.mkdir()
    source.write_text(json.dumps(page, indent=2, ensure_ascii=False), encoding="utf-8")

    _, result = _run(config, source, tmp_path / "out")
    top = result.results[0]  # type: ignore[attr-defined]
    assert top.status in (DocumentStatus.ANONYMIZED, DocumentStatus.NEEDS_REVIEW)
    out = json.loads(top.output_path.read_text(encoding="utf-8"))
    record = out["value"][0]
    # Schema and identifiers untouched.
    assert _keys(out) == _keys(page)
    assert record["activityid"] == DATAVERSE_ACTIVITY
    assert record["conversationindex"] == DATAVERSE_INDEX
    assert record["_ownerid_value@Microsoft.Dynamics.CRM.lookuplogicalname"] == "systemuser"
    # The attachment was anonymized and re-encoded the same way (76 columns, CRLF).
    body = record["email_activity_mime_attachment"][0]["body"]
    original = page["value"][0]["email_activity_mime_attachment"][0]["body"]  # type: ignore[index]
    assert body != original and body.endswith("\r\n")
    assert all(len(line) <= 76 for line in body.split("\r\n"))
    text = base64.b64decode(body).decode()
    assert "Quintavalle" not in text and CANARY_EMAIL not in text
    assert "Rédigé par" in text
    # The size field follows the anonymized file.
    attachment = record["email_activity_mime_attachment"][0]
    assert attachment["filesize"] == len(base64.b64decode(body))


def test_html_values_are_read_as_html(config: Config, tmp_path: Path) -> None:
    """A Dataverse ``description`` is HTML: entities decoded for detection, markup
    kept, data: URIs anonymized as files."""
    png = base64.b64encode(_png()).decode()
    description = (
        '<html><head><style>p.MsoNormal {font-family:"Aptos";}</style></head><body>'
        '<p class="MsoNormal">Bonjour,</p><p>Z&eacute;phyrin Quintavalle<br>'
        f'<img src="data:image/png;base64,{png}" alt="logo"></p></body></html>'
    )
    source = tmp_path / "in" / "page.json"
    source.parent.mkdir()
    source.write_text(json.dumps({"value": [{"description": description}]}), encoding="utf-8")
    doc = extract_file(source, "page.json", config, tmp_path / "work")
    texts = [s.text for s in doc.segments]
    assert any(CANARY_PERSON in t for t in texts)
    assert [c.name for c in doc.children] == ["fichier-incorpore-1.png"]

    _, result = _run(config, source, tmp_path / "out")
    top = result.results[0]  # type: ignore[attr-defined]
    assert top.status in (DocumentStatus.ANONYMIZED, DocumentStatus.NEEDS_REVIEW)
    out = json.loads(top.output_path.read_text(encoding="utf-8"))["value"][0]["description"]
    assert "Quintavalle" not in out and "Z&eacute;phyrin" not in out
    assert out.startswith('<html><head><style>p.MsoNormal {font-family:"Aptos";}</style>')
    assert 'src="data:image/png;base64,' in out and 'alt="logo"' in out


def test_unreadable_attachment_is_omitted(config: Config, tmp_path: Path) -> None:
    """Fail closed: a file that cannot be anonymized leaves an empty value."""
    broken = _png()[:16] + os.urandom(2000)  # PNG signature, unreadable image
    page = dataverse_page(broken, "piece_identite.png", "image/png")
    source = tmp_path / "in" / "page.json"
    source.parent.mkdir()
    source.write_text(json.dumps(page, indent=2), encoding="utf-8")

    _, result = _run(config, source, tmp_path / "out")
    top = result.results[0]  # type: ignore[attr-defined]
    assert top.status is DocumentStatus.NEEDS_REVIEW
    assert any(i.code == "child.omitted" for i in top.issues)
    out = json.loads(top.output_path.read_text(encoding="utf-8"))
    assert out["value"][0]["email_activity_mime_attachment"][0]["body"] == ""


def test_csv_and_xml_embedded_files(config: Config, tmp_path: Path) -> None:
    note = base64.b64encode(f"Signé {CANARY_PERSON}".encode()).decode()
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "pieces.csv").write_text(
        f"activityid,filename,body\n{GUID},note.txt,{note}\n", encoding="utf-8"
    )
    (folder / "pieces.xml").write_text(
        f'<?xml version="1.0"?>\n<pieces><piece filename="note.txt">{note}</piece></pieces>\n',
        encoding="utf-8",
    )
    pipeline = Pipeline(config)
    analysis = pipeline.analyze([folder])
    by_format = {d.format: d for d in analysis.documents}
    for fmt in ("csv", "xml"):
        assert [c.name for c in by_format[fmt].children] == ["fichier-incorpore-1.txt"], fmt
    result = pipeline.apply(analysis, tmp_path / "out")
    pipeline.close(analysis)
    for top in result.results:
        assert top.status in (DocumentStatus.ANONYMIZED, DocumentStatus.NEEDS_REVIEW)
        data = top.output_path.read_text(encoding="utf-8")
        assert note not in data and "Quintavalle" not in data
        assert GUID in data or top.format == "xml"
        assert "activityid,filename,body" in data or "<piece filename=" in data


# ---------------------------------------------------------------------------
# Raw backstop: embedded files nobody verified
# ---------------------------------------------------------------------------


def test_backstop_flags_unverified_embedded_files(tmp_path: Path) -> None:
    png = _png()
    encoded = base64.b64encode(png).decode()
    text = tmp_path / "sortie.txt"
    escaped = "\\r\\n".join(encoded[i : i + 76] for i in range(0, len(encoded), 76))
    text.write_text(f'{{"body": "{escaped}"}}', encoding="utf-8")
    scanner = RawScanner([], set(), set(), [], Config())
    output = Document(source=text, name="sortie.txt", format="txt")
    residues = raw_residues(scanner.scan(output))
    assert [r.entity_type for r in residues] == [EMBEDDED_TYPE]
    assert residues[0].certain and "octet 10" in residues[0].segment_label

    # The same bytes re-read as an item of the output (an extracted attachment): fine.
    child_path = tmp_path / "piece.png"
    child_path.write_bytes(png)
    output.children.append(Document(source=child_path, name="piece.png", format="png"))
    assert scanner.scan(output) == []


# ---------------------------------------------------------------------------
# Structural keys and headers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "createdon",
        "statuscode",
        "_ownerid_value",
        "@odata.etag",
        "@odata.context",
        "directioncode@OData.Community.Display.V1.FormattedValue",
        "_regardingobjectid_value@Microsoft.Dynamics.CRM.lookuplogicalname",
        "email_activity_mime_attachment",
        "sender_name",
        "telephone1",
    ],
)
def test_structural_names(name: str) -> None:
    assert is_structural(name)


@pytest.mark.parametrize(
    "name",
    [CANARY_PERSON, CANARY_EMAIL, "Nom du client", "michel", "tel0612345678", GUID, "1er"],
)
def test_names_that_stay_exposed(name: str) -> None:
    assert not is_structural(name)


def test_structural_keys_are_not_exposed(config: Config, tmp_path: Path) -> None:
    source = tmp_path / "page.json"
    data = {"value": [{"createdon@OData.Community.Display.V1.FormattedValue": "01/09/2026"}]}
    data["value"][0][CANARY_PERSON] = "clé nominative"
    source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    doc = extract_file(source, "page.json", config, tmp_path / "work")
    texts = [s.text for s in doc.segments]
    assert CANARY_PERSON in texts
    assert not any("OData" in t for t in texts)
    where = {s.text: s.context.where for s in doc.segments}
    assert where["01/09/2026"] == (
        "$.value[0]['createdon@OData.Community.Display.V1.FormattedValue'] (ligne 1 du fichier)"
    )
    assert where[CANARY_PERSON] == "$.value[0].* (clé, ligne 1 du fichier)"
    assert "odata" in doc.properties["structure"]


def test_structural_csv_header(config: Config, tmp_path: Path) -> None:
    source = tmp_path / "emails.csv"
    source.write_text(f"activityid,sender_name\n{GUID},{CANARY_PERSON}\n", encoding="utf-8")
    doc = extract_file(source, "emails.csv", config, tmp_path / "work")
    assert [s.text for s in doc.segments if s.id != "__filename__"] == [GUID, CANARY_PERSON]
    cell = next(s for s in doc.segments if s.text == CANARY_PERSON)
    assert cell.context.where == "ligne 2 du tableau, colonne B « sender_name »"
    # A list of identifiers has no header: every cell stays exposed.
    source.write_text("jdupont,mmartin\nppaul,ddurand\n", encoding="utf-8")
    doc = extract_file(source, "emails.csv", config, tmp_path / "work")
    assert len([s for s in doc.segments if s.id != "__filename__"]) == 4


def test_raw_backstop_never_searches_structure(tmp_path: Path) -> None:
    sensitive = [
        ("directioncode", "PERSONNE"),
        ("directioncode@OData.Community.Display.V1.FormattedValue", "EMAIL"),
        ("_ownerid_value", "PERSONNE"),
        (CANARY_PERSON, "PERSONNE"),
    ]
    assert is_structural_token("_ownerid_value")
    structure = vocabulary(["directioncode@OData.Community.Display.V1.FormattedValue"])
    scanner = RawScanner(sensitive, set(), set(), [], Config(), structure)
    path = tmp_path / "message.eml"
    data = f"X-D365-DirectionCode: Entrant\r\nX-Owner: _ownerid_value\r\n\r\n{CANARY_PERSON}\r\n"
    path.write_bytes(data.encode())
    hits = scanner.scan(Document(source=path, name="message.eml", format="eml"))
    assert [h.value for _, h in hits] == [CANARY_PERSON]
    (residue,) = raw_residues(hits)
    assert residue.segment_label == f"contenu brut, octet {data.index(CANARY_PERSON)}"


# ---------------------------------------------------------------------------
# Atomic identifiers
# ---------------------------------------------------------------------------


def test_spans_never_cut_identifiers() -> None:
    text = f"id {GUID} index {DATAVERSE_INDEX} Jean Dupont"
    tokens = atomic_spans(text)
    assert [text[s:e] for s, e in tokens] == [GUID, DATAVERSE_INDEX]
    inside = text.index("a-b")
    assert trimmed(text, inside, inside + 3, tokens) is None  # "a-b" of "4e8a-b93c"
    digits = text.index("8221")
    assert trimmed(text, digits, digits + 4, tokens) is None
    name = text.index("Jean")
    assert trimmed(text, name - 6, name + 11, tokens) == (name, name + 11)
    assert trimmed(text, 3, 3 + len(GUID), tokens) == (3, 3 + len(GUID))  # whole token


def test_plan_never_rewrites_inside_identifiers(config: Config) -> None:
    text = f"{GUID}.eml"
    doc = Document(source=Path("x"), name=text, format="eml")
    doc.segments.append(
        Segment("s", text, (), SegmentContext(SegmentKind.FILENAME, label="nom de fichier"))
    )
    start = text.index("a-b")
    entities = [
        Entity("PERSONNE", "a-b", doc.id, "s", start, start + 3, 0.9, "test"),
        Entity("TELEPHONE", "5216", doc.id, "s", 32, 36, 0.9, "test"),
    ]
    entities, clusters = resolve(entities, config)
    table = Replacer(config).assign(clusters, entities, [doc])
    assert build_plan(doc, entities, table).by_segment.get("s", []) == []


def test_guids_survive_the_pipeline(config: Config, tmp_path: Path) -> None:
    folder = tmp_path / "in"
    folder.mkdir()
    guid = "12345678-0612-3456-7890-0612345678ab"  # a phone number inside
    (folder / f"{guid}.txt").write_text(
        f"X-D365-ActivityId: {guid}\nThread: {DATAVERSE_INDEX}\nDe {CANARY_PERSON}, "
        f"tel 06 12 34 56 78\n",
        encoding="utf-8",
    )
    _, result = _run(config, folder, tmp_path / "out")
    top = result.results[0]  # type: ignore[attr-defined]
    assert top.output_name.endswith(f"{guid}.txt")
    out = top.output_path.read_text(encoding="utf-8")
    assert f"X-D365-ActivityId: {guid}" in out and DATAVERSE_INDEX in out
    assert "Quintavalle" not in out and "06 12 34 56 78" not in out


# ---------------------------------------------------------------------------
# Residue locations
# ---------------------------------------------------------------------------


def test_residue_place() -> None:
    text = "ligne un\nligne deux Dupont"
    segment = Segment("c0", text, (0,), SegmentContext(SegmentKind.BODY, label="bloc 1", line=40))
    assert place(segment, text.index("Dupont")) == "bloc 1 (ligne 41 du fichier)"
    where = "$.value[27].description (ligne 845 du fichier)"
    json_segment = replace(
        segment, context=SegmentContext(SegmentKind.BODY, label="$.*[27].*", where=where)
    )
    assert place(json_segment, 3) == where


def test_mark_is_one_character() -> None:
    assert len(MARK) == 1
    assert Base64Style(prefix="data:x/y;base64,").encode(b"ab") == "data:x/y;base64,YWI="
