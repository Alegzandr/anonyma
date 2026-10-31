from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from anonyma.config import Config
from anonyma.extractors._xml import XmlError, parse_xml
from anonyma.models import DocumentStatus
from anonyma.pipeline import Pipeline
from tests.canary import CANARY_EMAIL, CANARY_PERSON

FIXTURES = Path(__file__).parent / "fixtures" / "contract"


def _run(tmp_path: Path, name: str, data: bytes, config: Config) -> tuple[DocumentStatus, bytes]:
    src = tmp_path / "in"
    src.mkdir()
    (src / name).write_bytes(data)
    top = Pipeline(config).run([src / name], tmp_path / "out").results[0]
    output = top.output_path.read_bytes() if top.output_path else b""
    return top.status, output


def test_internal_subset_is_expanded_and_dropped(tmp_path: Path, canary_config: Config) -> None:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE fiche [\n"
        f'  <!ENTITY nom "{CANARY_PERSON}">\n'
        f'  <!ENTITY mel "{CANARY_EMAIL}">\n'
        f'  <!ATTLIST client ref CDATA "{CANARY_PERSON}">\n'
        "]>\n<fiche><client>Contact : &nom; (&mel;)</client></fiche>\n"
    ).encode()
    status, output = _run(tmp_path, "fiche.xml", xml, canary_config)
    assert status is DocumentStatus.ANONYMIZED
    assert b"uintavalle" not in output.lower()
    assert b"<!DOCTYPE" not in output and b"ENTITY" not in output
    assert b"<client>Contact : [PERSONNE_001] ([EMAIL_001])</client>" in output


@pytest.mark.parametrize(
    ("system_id", "kept"),
    [("fiche.dtd", True), (f"{CANARY_EMAIL}.dtd", False)],
)
def test_external_doctype_ids_kept_unless_sensitive(
    tmp_path: Path, canary_config: Config, system_id: str, kept: bool
) -> None:
    xml = f'<?xml version="1.0"?>\n<!DOCTYPE fiche SYSTEM "{system_id}">\n<fiche>x</fiche>\n'
    status, output = _run(tmp_path, "fiche.xml", xml.encode(), canary_config)
    assert status is DocumentStatus.ANONYMIZED
    assert (b'<!DOCTYPE fiche SYSTEM "fiche.dtd">' in output) is kept
    assert b"uintavalle" not in output.lower()


def test_office_part_with_doctype_fails_closed(tmp_path: Path, canary_config: Config) -> None:
    with pytest.raises(XmlError):
        parse_xml(b'<!DOCTYPE w [<!ENTITY n "x">]><w>&n;</w>')
    buffer = io.BytesIO()
    with (
        zipfile.ZipFile(FIXTURES / "rapport.docx") as source,
        zipfile.ZipFile(buffer, "w") as target,
    ):
        for item in source.infolist():
            data = source.read(item)
            if item.filename == "word/document.xml":
                head, _, body = data.partition(b"?>")
                data = (
                    head + b'?><!DOCTYPE d [<!ENTITY n "' + CANARY_PERSON.encode() + b'">]>' + body
                )
            target.writestr(item, data)
    status, output = _run(tmp_path, "rapport.docx", buffer.getvalue(), canary_config)
    assert status is DocumentStatus.FAILED
    assert output == b""


def test_declaration_and_top_level_layout_are_kept(tmp_path: Path, canary_config: Config) -> None:
    xml = (
        '<?xml version="1.0" encoding="iso-8859-1" standalone="yes"?>\r\n'
        f"<?auteur {CANARY_PERSON}?>\r\n"
        "<!-- export -->\r\n\r\n"
        '<export>\r\n  <salarie nom="Quintavalle">texte</salarie>\r\n</export>\r\n'
        "<!-- fin -->\r\n"
    ).encode("iso-8859-1")
    status, output = _run(tmp_path, "export.xml", xml, canary_config)
    assert status is DocumentStatus.ANONYMIZED
    assert output == (
        '<?xml version="1.0" encoding="iso-8859-1" standalone="yes"?>\r\n'
        "<?auteur [PERSONNE_001]?>\r\n"
        "<!-- export -->\r\n\r\n"
        '<export>\r\n  <salarie nom="[PERSONNE_001]">texte</salarie>\r\n</export>\r\n'
        "<!-- fin -->\r\n"
    ).encode("iso-8859-1")
    # Without declaration nor trailing newline: nothing added.
    (tmp_path / "b").mkdir()
    status, output = _run(tmp_path / "b", "b.xml", b"<a>Quintavalle</a>", canary_config)
    assert output == b"<a>[PERSONNE_001]</a>"
