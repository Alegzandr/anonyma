"""Generate Office/PDF/data contract fixtures: ``python -m tests.fixtures.generators.office``.

Every fixture carries the canary person and email (``tests/canary.py``) in its
body and in every metadata field the format supports. The docx fixture also
splits the canary person across differently formatted runs.

Documents are built from raw XML so that the fixtures exercise exactly the
structures the extractors must handle (tracked changes, comments, notes...).
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Mapping
from pathlib import Path

from tests.canary import CANARY_EMAIL, CANARY_PERSON

OUT = Path(__file__).resolve().parent.parent / "contract"

P = CANARY_PERSON
E = CANARY_EMAIL
FIRST, LAST = P.split(" ", 1)

XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def write_zip(path: Path, entries: Mapping[str, str | bytes], *, stored: set[str] = set()) -> None:  # noqa: B006
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            info = zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED if name in stored else zipfile.ZIP_DEFLATED
            data = content.encode("utf-8") if isinstance(content, str) else content
            zf.writestr(info, data)
    path.write_bytes(buffer.getvalue())


# ---------------------------------------------------------------------------
# OOXML common parts
# ---------------------------------------------------------------------------


def core_xml() -> str:
    return (
        XML_DECL
        + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
        'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>Dossier {P}</dc:title><dc:subject>Suivi {P}</dc:subject>"
        f"<dc:creator>{P}</dc:creator><cp:keywords>{P}; {E}</cp:keywords>"
        f"<dc:description>Contact {E}</dc:description>"
        f"<cp:lastModifiedBy>{P}</cp:lastModifiedBy><cp:category>{P}</cp:category>"
        f"<cp:contentStatus>{P}</cp:contentStatus>"
        '<dcterms:created xsi:type="dcterms:W3CDTF">2024-01-01T00:00:00Z</dcterms:created>'
        "</cp:coreProperties>"
    )


def app_xml(application: str) -> str:
    return (
        XML_DECL + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
        'extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/'
        'docPropsVTypes">'
        f"<Application>{application}</Application><Company>{P}</Company>"
        f"<Manager>{P}</Manager><HyperlinkBase>mailto:{E}</HyperlinkBase>"
        '<TitlesOfParts><vt:vector size="1" baseType="lpstr">'
        f"<vt:lpstr>{P}</vt:lpstr></vt:vector></TitlesOfParts>"
        "</Properties>"
    )


def custom_xml() -> str:
    return (
        XML_DECL + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
        'custom-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/'
        'docPropsVTypes">'
        '<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="2" name="Client">'
        f"<vt:lpwstr>{P}</vt:lpwstr></property>"
        '<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="3" name="Courriel">'
        f"<vt:lpwstr>{E}</vt:lpwstr></property></Properties>"
    )


def package_rels(main: str) -> str:
    return (
        XML_DECL + f'<Relationships xmlns="{PKG_REL}">'
        f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="{main}"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/'
        'relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        f'<Relationship Id="rId3" Type="{REL}/extended-properties" Target="docProps/app.xml"/>'
        f'<Relationship Id="rId4" Type="{REL}/custom-properties" Target="docProps/custom.xml"/>'
        "</Relationships>"
    )


def rels(items: list[tuple[str, str, str, bool]]) -> str:
    body = "".join(
        f'<Relationship Id="{rid}" Type="{REL}/{kind}" Target="{target}"'
        + (' TargetMode="External"' if external else "")
        + "/>"
        for rid, kind, target, external in items
    )
    return XML_DECL + f'<Relationships xmlns="{PKG_REL}">{body}</Relationships>'


def content_types(overrides: Mapping[str, str]) -> str:
    body = "".join(
        f'<Override PartName="/{name}" ContentType="{ctype}"/>' for name, ctype in overrides.items()
    )
    return (
        XML_DECL + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
        'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        f"{body}"
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-'
        'package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.extended-properties+xml"/>'
        '<Override PartName="/docProps/custom.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.custom-properties+xml"/>'
        "</Types>"
    )


WML = "application/vnd.openxmlformats-officedocument.wordprocessingml"


def w_doc(root: str, body: str) -> str:
    return XML_DECL + f'<w:{root} xmlns:w="{NS_W}" xmlns:r="{NS_R}">{body}</w:{root}>'


def w_para(text: str) -> str:
    return f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def docx_entries(*, image: bytes | None = None) -> dict[str, str | bytes]:
    """Entries of the docx fixture (``image`` adds an embedded picture)."""
    split_name = (
        '<w:p w:rsidR="00A1B2C3"><w:r><w:t xml:space="preserve">Rapport rédigé par </w:t></w:r>'
        f"<w:r><w:rPr><w:b/></w:rPr><w:t>{FIRST[:2]}</w:t></w:r>"
        f"<w:r><w:rPr><w:i/></w:rPr><w:t>{FIRST[2:]}</w:t></w:r>"
        f'<w:r><w:t xml:space="preserve"> {LAST}</w:t></w:r>'
        "<w:r><w:t>.</w:t></w:r>"
        '<w:commentRangeStart w:id="0"/><w:r><w:t xml:space="preserve"> Voir note</w:t></w:r>'
        '<w:commentRangeEnd w:id="0"/><w:r><w:commentReference w:id="0"/></w:r>'
        '<w:r><w:footnoteReference w:id="1"/></w:r><w:r><w:endnoteReference w:id="1"/></w:r>'
        "</w:p>"
    )
    tracked = (
        '<w:p><w:r><w:t xml:space="preserve">Validé par </w:t></w:r>'
        f'<w:ins w:id="10" w:author="{P}" w:date="2024-01-01T00:00:00Z">'
        f"<w:r><w:t>{P}</w:t></w:r></w:ins>"
        f'<w:del w:id="11" w:author="{P}" w:date="2024-01-01T00:00:00Z">'
        f'<w:r><w:delText xml:space="preserve"> ancien contact {E}</w:delText></w:r></w:del>'
        "</w:p>"
    )
    field = (
        '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        f'<w:r><w:instrText xml:space="preserve"> HYPERLINK "mailto:{E}" </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        '<w:r><w:t xml:space="preserve">Écrire à </w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        f'<w:hyperlink r:id="rIdLink" w:tooltip="{P}"><w:r><w:t>{E}</w:t></w:r></w:hyperlink>'
        "</w:p>"
    )
    table = (
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Courriel</w:t></w:r></w:p></w:tc>"
        f"<w:tc><w:p><w:r><w:t>{E}</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    )
    sdt = (
        f'<w:sdt><w:sdtPr><w:alias w:val="{P}"/><w:tag w:val="{E}"/></w:sdtPr>'
        f"<w:sdtContent>{w_para('Champ ' + P)}</w:sdtContent></w:sdt>"
    )
    drawing = ""
    if image is not None:
        drawing = (
            '<w:p><w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/'
            'drawingml/2006/wordprocessingDrawing">'
            f'<wp:extent cx="9525" cy="9525"/><wp:docPr id="1" name="Photo" descr="{P}"/>'
            f'<a:graphic xmlns:a="{NS_A}"><a:graphicData uri="http://schemas.openxmlformats.org/'
            'drawingml/2006/picture"><pic:pic xmlns:pic="http://schemas.openxmlformats.org/'
            'drawingml/2006/picture"><pic:nvPicPr><pic:cNvPr id="1" name="Photo"/>'
            "<pic:cNvPicPr/></pic:nvPicPr><pic:blipFill>"
            '<a:blip r:embed="rIdImg"/></pic:blipFill><pic:spPr/></pic:pic></a:graphicData>'
            "</a:graphic></wp:inline></w:drawing></w:r></w:p>"
        )
    body = (
        split_name
        + tracked
        + field
        + table
        + sdt
        + drawing
        + w_para(f"M. {P.upper()} ({E}) a signé.")
        + '<w:sectPr><w:headerReference w:type="default" r:id="rIdH"/>'
        '<w:footerReference w:type="default" r:id="rIdF"/></w:sectPr>'
    )
    doc_rels = [
        ("rIdLink", "hyperlink", f"mailto:{E}", True),
        ("rIdH", "header", "header1.xml", False),
        ("rIdF", "footer", "footer1.xml", False),
        ("rIdFn", "footnotes", "footnotes.xml", False),
        ("rIdEn", "endnotes", "endnotes.xml", False),
        ("rIdC", "comments", "comments.xml", False),
        ("rIdS", "settings", "settings.xml", False),
    ]
    if image is not None:
        doc_rels.append(("rIdImg", "image", "media/image1.png", False))
    entries: dict[str, str | bytes] = {
        "[Content_Types].xml": content_types(
            {
                "word/document.xml": f"{WML}.document.main+xml",
                "word/header1.xml": f"{WML}.header+xml",
                "word/footer1.xml": f"{WML}.footer+xml",
                "word/footnotes.xml": f"{WML}.footnotes+xml",
                "word/endnotes.xml": f"{WML}.endnotes+xml",
                "word/comments.xml": f"{WML}.comments+xml",
                "word/settings.xml": f"{WML}.settings+xml",
            }
        ),
        "_rels/.rels": package_rels("word/document.xml"),
        "word/document.xml": w_doc("document", f"<w:body>{body}</w:body>"),
        "word/_rels/document.xml.rels": rels(doc_rels),
        "word/header1.xml": w_doc("hdr", w_para(f"Confidentiel — {P}")),
        "word/footer1.xml": w_doc("ftr", w_para(f"Contact : {E}")),
        "word/footnotes.xml": w_doc(
            "footnotes", f'<w:footnote w:id="1">{w_para("Source : " + P)}</w:footnote>'
        ),
        "word/endnotes.xml": w_doc(
            "endnotes", f'<w:endnote w:id="1">{w_para("Annexe : " + E)}</w:endnote>'
        ),
        "word/comments.xml": w_doc(
            "comments",
            f'<w:comment w:id="0" w:author="{P}" w:initials="ZQ" w:date="2024-01-01T00:00:00Z">'
            f"{w_para('Relu par ' + P + ', ' + E)}</w:comment>",
        ),
        "word/settings.xml": w_doc(
            "settings",
            '<w:rsids><w:rsidRoot w:val="00A1B2C3"/><w:rsid w:val="00A1B2C3"/></w:rsids>'
            f'<w:docVars><w:docVar w:name="client" w:val="{P}"/></w:docVars>',
        ),
        "docProps/core.xml": core_xml(),
        "docProps/app.xml": app_xml("Microsoft Office Word"),
        "docProps/custom.xml": custom_xml(),
    }
    if image is not None:
        entries["word/media/image1.png"] = image
    return entries


def make_docx(path: Path) -> None:
    write_zip(path, docx_entries())


SML = "application/vnd.openxmlformats-officedocument.spreadsheetml"


def s_doc(root: str, body: str, extra_ns: str = "") -> str:
    return XML_DECL + f'<{root} xmlns="{NS_S}" xmlns:r="{NS_R}"{extra_ns}>{body}</{root}>'


def xlsx_entries() -> dict[str, str | bytes]:
    sheet_name = P  # sheet named after the canary: must be renamed consistently
    shared = (
        f'<sst count="4" uniqueCount="4"><si><t>{P}</t></si>'
        f'<si><r><rPr><b/></rPr><t>{FIRST[:2]}</t></r><r><t xml:space="preserve">{FIRST[2:]} '
        f"{LAST}</t></r></si>"
        f"<si><t>{E}</t></si><si><t>Nom</t></si></sst>"
    )
    sheet1 = (
        "<sheetData>"
        '<row r="1"><c r="A1" t="s"><v>3</v></c><c r="B1" t="s"><v>0</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>1</v></c>'
        f'<c r="B2" t="inlineStr"><is><t>{E}</t></is></c></row>'
        '<row r="3"><c r="A3" t="s"><v>2</v></c><c r="B3"><v>42</v></c></row>'
        '<row r="4"><c r="A4" t="str"><f>CONCATENATE("Contact ","'
        f'{P}")</f><v>Contact {P}</v></c></row>'
        "</sheetData>"
        '<dataValidations count="1"><dataValidation type="list" sqref="C1">'
        f'<formula1>"{P},Autre"</formula1></dataValidation></dataValidations>'
        f'<hyperlinks><hyperlink ref="B2" r:id="rIdL" tooltip="{P}" display="{E}"/></hyperlinks>'
        '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
        f"<headerFooter><oddHeader>&amp;L{P}</oddHeader><oddFooter>&amp;R{E}</oddFooter>"
        "</headerFooter>"
        '<legacyDrawing r:id="rIdV"/>'
    )
    quoted = "'" + sheet_name + "'"
    sheet2 = (
        "<sheetData>"
        f'<row r="1"><c r="A1" t="str"><f>{quoted}!B1</f><v>{P}</v></c></row>'
        "</sheetData>"
    )
    vml = (
        '<xml xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:'
        'office:office" xmlns:x="urn:schemas-microsoft-com:office:excel">'
        '<v:shape id="_x0000_s1025" type="#_x0000_t202"><v:textbox>'
        f"<div style='text-align:left'>{P}<br></div></v:textbox>"
        '<x:ClientData ObjectType="Note"><x:Row>0</x:Row><x:Column>1</x:Column>'
        "</x:ClientData></v:shape></xml>"
    )
    comments = (
        f"<authors><author>{P}</author></authors><commentList>"
        f'<comment ref="B1" authorId="0"><text><r><rPr><b/></rPr><t>{P}:</t></r>'
        f'<r><t xml:space="preserve"> écrire à {E}</t></r></text></comment></commentList>'
    )
    return {
        "[Content_Types].xml": content_types(
            {
                "xl/workbook.xml": f"{SML}.sheet.main+xml",
                "xl/worksheets/sheet1.xml": f"{SML}.worksheet+xml",
                "xl/worksheets/sheet2.xml": f"{SML}.worksheet+xml",
                "xl/sharedStrings.xml": f"{SML}.sharedStrings+xml",
                "xl/comments1.xml": f"{SML}.comments+xml",
            }
        ).replace(
            '<Default Extension="png"',
            '<Default Extension="vml" ContentType="application/vnd.openxmlformats-'
            'officedocument.vmlDrawing"/><Default Extension="png"',
        ),
        "_rels/.rels": package_rels("xl/workbook.xml"),
        "xl/workbook.xml": s_doc(
            "workbook",
            f'<sheets><sheet name="{sheet_name}" sheetId="1" r:id="rId1"/>'
            '<sheet name="Synthèse" sheetId="2" r:id="rId2"/></sheets>'
            f'<definedNames><definedName name="Responsable">{quoted}!$B$1</definedName>'
            "</definedNames>",
        ),
        "xl/_rels/workbook.xml.rels": rels(
            [
                ("rId1", "worksheet", "worksheets/sheet1.xml", False),
                ("rId2", "worksheet", "worksheets/sheet2.xml", False),
                ("rId3", "sharedStrings", "sharedStrings.xml", False),
            ]
        ),
        "xl/sharedStrings.xml": XML_DECL + shared.replace("<sst ", f'<sst xmlns="{NS_S}" ', 1),
        "xl/worksheets/sheet1.xml": s_doc("worksheet", sheet1),
        "xl/worksheets/_rels/sheet1.xml.rels": rels(
            [
                ("rIdL", "hyperlink", f"mailto:{E}", True),
                ("rIdC", "comments", "../comments1.xml", False),
                ("rIdV", "vmlDrawing", "../drawings/vmlDrawing1.vml", False),
            ]
        ),
        "xl/worksheets/sheet2.xml": s_doc("worksheet", sheet2),
        "xl/comments1.xml": s_doc("comments", comments),
        "xl/drawings/vmlDrawing1.vml": vml,
        "docProps/core.xml": core_xml(),
        "docProps/app.xml": app_xml("Microsoft Excel"),
        "docProps/custom.xml": custom_xml(),
    }


def make_xlsx(path: Path) -> None:
    write_zip(path, xlsx_entries())


PML = "application/vnd.openxmlformats-officedocument.presentationml"


def p_doc(root: str, body: str) -> str:
    return (
        XML_DECL + f'<p:{root} xmlns:a="{NS_A}" xmlns:r="{NS_R}" xmlns:p="{NS_P}">{body}</p:{root}>'
    )


def p_shape(shape_id: int, paragraphs: str, *, descr: str = "") -> str:
    attr = f' descr="{descr}"' if descr else ""
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="Zone {shape_id}"{attr}/><p:cNvSpPr/>'
        "<p:nvPr/></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/>"
        f"{paragraphs}</p:txBody></p:sp>"
    )


def pptx_entries(*, image: bytes | None = None) -> dict[str, str | bytes]:
    title = (
        f'<a:p><a:r><a:rPr lang="fr-FR" b="1"/><a:t>{FIRST[:2]}</a:t></a:r>'
        f'<a:r><a:rPr lang="fr-FR"/><a:t>{FIRST[2:]} {LAST}</a:t></a:r></a:p>'
    )
    link = (
        '<a:p><a:r><a:rPr lang="fr-FR"><a:hlinkClick r:id="rIdL" '
        f'tooltip="{P}"/></a:rPr><a:t>{E}</a:t></a:r></a:p>'
    )
    table = (
        '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="4" name="Tableau"/>'
        "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm/>"
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">'
        f'<a:tbl><a:tr h="0"><a:tc><a:txBody><a:bodyPr/><a:p><a:r><a:t>{E}</a:t></a:r></a:p>'
        "</a:txBody></a:tc></a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    )
    picture = ""
    if image is not None:
        picture = (
            '<p:pic><p:nvPicPr><p:cNvPr id="5" name="Photo"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
            '<p:blipFill><a:blip r:embed="rIdImg"/></p:blipFill><p:spPr/></p:pic>'
        )
    slide = p_doc(
        "sld",
        '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/>'
        "</p:nvGrpSpPr><p:grpSpPr/>"
        + p_shape(2, title, descr=P)
        + p_shape(3, link)
        + table
        + picture
        + "</p:spTree></p:cSld>",
    )
    notes = p_doc(
        "notes",
        '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/>'
        "</p:nvGrpSpPr><p:grpSpPr/>"
        + p_shape(2, f"<a:p><a:r><a:t>Appeler {P} ({E})</a:t></a:r></a:p>")
        + "</p:spTree></p:cSld>",
    )
    layout = p_doc(
        "sldLayout",
        '<p:cSld name="Titre"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/>'
        "<p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>"
        + p_shape(2, f"<a:p><a:r><a:t>Présenté par {P}</a:t></a:r></a:p>")
        + "</p:spTree></p:cSld>",
    )
    master = p_doc(
        "sldMaster",
        '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/>'
        "</p:nvGrpSpPr><p:grpSpPr/>"
        + p_shape(2, f"<a:p><a:r><a:t>{E}</a:t></a:r></a:p>")
        + '</p:spTree></p:cSld><p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/>'
        "</p:sldLayoutIdLst>",
    )
    slide_rels = [
        ("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml", False),
        ("rIdL", "hyperlink", f"mailto:{E}", True),
        ("rIdN", "notesSlide", "../notesSlides/notesSlide1.xml", False),
        ("rIdC", "comments", "../comments/comment1.xml", False),
    ]
    if image is not None:
        slide_rels.append(("rIdImg", "image", "../media/image1.png", False))
    entries: dict[str, str | bytes] = {
        "[Content_Types].xml": content_types(
            {
                "ppt/presentation.xml": f"{PML}.presentation.main+xml",
                "ppt/slides/slide1.xml": f"{PML}.slide+xml",
                "ppt/notesSlides/notesSlide1.xml": f"{PML}.notesSlide+xml",
                "ppt/slideLayouts/slideLayout1.xml": f"{PML}.slideLayout+xml",
                "ppt/slideMasters/slideMaster1.xml": f"{PML}.slideMaster+xml",
                "ppt/commentAuthors.xml": f"{PML}.commentAuthors+xml",
                "ppt/comments/comment1.xml": f"{PML}.comments+xml",
            }
        ),
        "_rels/.rels": package_rels("ppt/presentation.xml"),
        "ppt/presentation.xml": p_doc(
            "presentation",
            '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
            '<p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>'
            '<p:sldSz cx="9144000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/>',
        ),
        "ppt/_rels/presentation.xml.rels": rels(
            [
                ("rId1", "slideMaster", "slideMasters/slideMaster1.xml", False),
                ("rId2", "slide", "slides/slide1.xml", False),
                ("rId3", "commentAuthors", "commentAuthors.xml", False),
            ]
        ),
        "ppt/slides/slide1.xml": slide,
        "ppt/slides/_rels/slide1.xml.rels": rels(slide_rels),
        "ppt/notesSlides/notesSlide1.xml": notes,
        "ppt/notesSlides/_rels/notesSlide1.xml.rels": rels(
            [("rId1", "slide", "../slides/slide1.xml", False)]
        ),
        "ppt/slideLayouts/slideLayout1.xml": layout,
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": rels(
            [("rId1", "slideMaster", "../slideMasters/slideMaster1.xml", False)]
        ),
        "ppt/slideMasters/slideMaster1.xml": master,
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": rels(
            [("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml", False)]
        ),
        "ppt/commentAuthors.xml": p_doc(
            "cmAuthorLst",
            f'<p:cmAuthor id="0" name="{P}" initials="ZQ" lastIdx="1" clrIdx="0"/>',
        ),
        "ppt/comments/comment1.xml": p_doc(
            "cmLst",
            '<p:cm authorId="0" dt="2024-01-01T00:00:00.000" idx="1"><p:pos x="10" y="10"/>'
            f"<p:text>À valider avec {P} ({E})</p:text></p:cm>",
        ),
        "docProps/core.xml": core_xml(),
        "docProps/app.xml": app_xml("Microsoft Office PowerPoint"),
        "docProps/custom.xml": custom_xml(),
    }
    if image is not None:
        entries["ppt/media/image1.png"] = image
    return entries


def make_pptx(path: Path) -> None:
    write_zip(path, pptx_entries())


# ---------------------------------------------------------------------------
# OpenDocument
# ---------------------------------------------------------------------------

ODF_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" '
    'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
    'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
    'xmlns:config="urn:oasis:names:tc:opendocument:xmlns:config:1.0" '
    'xmlns:of="urn:oasis:names:tc:opendocument:xmlns:of:1.2"'
)
ODF_DECL = '<?xml version="1.0" encoding="UTF-8"?>\n'


def odf_meta() -> str:
    return (
        ODF_DECL + f'<office:document-meta {ODF_NS} office:version="1.3"><office:meta>'
        "<meta:generator>Anonyma-fixtures</meta:generator>"
        f"<dc:title>Dossier {P}</dc:title><dc:subject>Suivi {P}</dc:subject>"
        f"<dc:description>Contact {E}</dc:description>"
        f"<meta:initial-creator>{P}</meta:initial-creator><dc:creator>{P}</dc:creator>"
        f"<meta:printed-by>{P}</meta:printed-by><meta:keyword>{E}</meta:keyword>"
        f'<meta:user-defined meta:name="Client">{P}</meta:user-defined>'
        f'<meta:template xlink:type="simple" xlink:href="file:///C:/Users/{LAST}/modele.ott" '
        f'xlink:title="{P}"/>'
        "</office:meta></office:document-meta>"
    )


def odf_settings() -> str:
    return (
        ODF_DECL + f'<office:document-settings {ODF_NS} office:version="1.3"><office:settings>'
        '<config:config-item-set config:name="ooo:configuration-settings">'
        f'<config:config-item config:name="PrinterName" config:type="string">Imprimante {P}'
        "</config:config-item></config:config-item-set></office:settings>"
        "</office:document-settings>"
    )


def odf_styles(header: str, footer: str) -> str:
    return (
        ODF_DECL + f'<office:document-styles {ODF_NS} office:version="1.3"><office:master-styles>'
        '<style:master-page style:name="Standard">'
        f"<style:header><text:p>{header}</text:p></style:header>"
        f"<style:footer><text:p>{footer}</text:p></style:footer>"
        "</style:master-page></office:master-styles></office:document-styles>"
    )


def odf_manifest(mimetype: str, extra: tuple[str, ...] = ()) -> str:
    entries = "".join(
        f'<manifest:file-entry manifest:full-path="{name}" manifest:media-type="text/xml"/>'
        for name in ("content.xml", "styles.xml", "meta.xml", "settings.xml", *extra)
    )
    return (
        ODF_DECL
        + '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"'
        ' manifest:version="1.3">'
        f'<manifest:file-entry manifest:full-path="/" manifest:media-type="{mimetype}"/>'
        f"{entries}</manifest:manifest>"
    )


def odf_annotation(text: str) -> str:
    return (
        f"<office:annotation><dc:creator>{P}</dc:creator><dc:date>2024-01-01T00:00:00</dc:date>"
        f"<text:p>{text}</text:p></office:annotation>"
    )


def odf_package(
    path: Path, mimetype: str, body: str, *, extra: Mapping[str, bytes] | None = None
) -> None:
    content = (
        ODF_DECL + f'<office:document-content {ODF_NS} office:version="1.3"><office:body>{body}'
        "</office:body></office:document-content>"
    )
    entries: dict[str, str | bytes] = {
        "mimetype": mimetype,
        "content.xml": content,
        "styles.xml": odf_styles(f"Confidentiel — {P}", f"Contact : {E}"),
        "meta.xml": odf_meta(),
        "settings.xml": odf_settings(),
        "Thumbnails/thumbnail.png": b"\x89PNG fake thumbnail",
    }
    entries.update(extra or {})
    entries["META-INF/manifest.xml"] = odf_manifest(mimetype, tuple(extra or {}))
    write_zip(path, entries, stored={"mimetype"})


def odt_body(*, image: bool = False) -> str:
    frame = ""
    if image:
        frame = (
            '<text:p><draw:frame draw:name="Photo" svg:width="1cm" svg:height="1cm">'
            '<draw:image xlink:href="Pictures/photo.png" xlink:type="simple"/>'
            f"<svg:title>{P}</svg:title><svg:desc>Portrait de {P}</svg:desc>"
            "</draw:frame></text:p>"
        )
    return (
        "<office:text>"
        '<text:tracked-changes><text:changed-region text:id="ct1"><text:deletion>'
        f"<office:change-info><dc:creator>{P}</dc:creator><dc:date>2024-01-01T00:00:00</dc:date>"
        f"</office:change-info><text:p>Ancien contact {E}</text:p></text:deletion>"
        "</text:changed-region></text:tracked-changes>"
        f'<text:h text:outline-level="1">Dossier de {P}</text:h>'
        f'<text:p>Rapport rédigé par {FIRST[:2]}<text:span text:style-name="T1">{FIRST[2:]}'
        f'</text:span><text:s/>{LAST}.<text:change text:change-id="ct1"/>'
        f"{odf_annotation('Relu par ' + P + ', ' + E)}"
        '<text:note text:id="n1" text:note-class="footnote"><text:note-citation>1'
        f"</text:note-citation><text:note-body><text:p>Source : {P}</text:p></text:note-body>"
        "</text:note></text:p>"
        f'<text:p>Écrire à <text:a xlink:type="simple" xlink:href="mailto:{E}">{E}</text:a>'
        "</text:p>"
        '<table:table table:name="Contacts"><table:table-row><table:table-cell>'
        f"<text:p>{E}</text:p></table:table-cell></table:table-row></table:table>"
        f"{frame}"
        f"<text:p>M. {P.upper()} a signé.</text:p>"
        "</office:text>"
    )


def make_odt(path: Path) -> None:
    odf_package(path, "application/vnd.oasis.opendocument.text", odt_body())


def ods_body() -> str:
    sheet = P
    return (
        f'<office:spreadsheet><table:table table:name="{sheet}">'
        "<table:table-row>"
        '<table:table-cell office:value-type="string"><text:p>Nom</text:p></table:table-cell>'
        f'<table:table-cell office:value-type="string"><text:p>{FIRST[:2]}<text:span>'
        f"{FIRST[2:]}</text:span> {LAST}</text:p>{odf_annotation('Vu ' + E)}</table:table-cell>"
        "</table:table-row><table:table-row>"
        f'<table:table-cell table:number-columns-repeated="2"/>'
        f'<table:table-cell office:value-type="string" office:string-value="{E}">'
        f"<text:p>{E}</text:p></table:table-cell></table:table-row></table:table>"
        '<table:table table:name="Synthèse"><table:table-row>'
        f'<table:table-cell table:formula="of:=[\'{sheet}\'.B1]" office:value-type="string" '
        f'office:string-value="{P}"><text:p>{P}</text:p></table:table-cell>'
        "</table:table-row></table:table>"
        "</office:spreadsheet>"
    )


def make_ods(path: Path) -> None:
    odf_package(path, "application/vnd.oasis.opendocument.spreadsheet", ods_body())


def odp_body() -> str:
    return (
        "<office:presentation>"
        f'<draw:page draw:name="{P}" draw:master-page-name="Standard">'
        '<draw:frame presentation:class="title"><draw:text-box>'
        f"<text:p>{FIRST[:2]}<text:span>{FIRST[2:]}</text:span> {LAST}</text:p>"
        f"</draw:text-box></draw:frame>"
        f"<draw:frame><draw:text-box><text:p>{E}</text:p></draw:text-box>"
        f"<svg:title>{P}</svg:title></draw:frame>"
        f"{odf_annotation('À valider ' + P)}"
        "<presentation:notes><draw:frame><draw:text-box>"
        f"<text:p>Appeler {P} ({E})</text:p></draw:text-box></draw:frame></presentation:notes>"
        "</draw:page></office:presentation>"
    )


def make_odp(path: Path) -> None:
    odf_package(path, "application/vnd.oasis.opendocument.presentation", odp_body())


# ---------------------------------------------------------------------------
# Data formats (no metadata fields)
# ---------------------------------------------------------------------------


def make_csv(path: Path) -> None:
    text = (
        "Nom;Courriel;Note\r\n"
        f'"{P}";{E};"Rappeler ; {P}"\r\n'
        f'Autre;;"Ligne 1\r\nsuivi par {FIRST}\r\n{LAST}"\r\n'
    )
    path.write_bytes(text.encode("utf-8-sig"))


def make_tsv(path: Path) -> None:
    text = f"nom\tcourriel\n{P}\t{E}\nM. {P.upper()}\t\n"
    path.write_bytes(text.encode("cp1252"))


def make_json(path: Path) -> None:
    data = {
        "dossier": {
            "titulaire": P,
            "contact": {"courriel": E, "telephone": 612345678},
            P: "clé nominative",
            "historique": [f"Créé par {P}", None, True, 3.5],
        }
    }
    text = json.dumps(data, ensure_ascii=False, indent=2)
    # One value written with \u escapes to exercise the escaping style.
    text = text.replace(f'"Créé par {P}"', json.dumps(f"Créé par {P}", ensure_ascii=True))
    path.write_bytes((text + "\n").encode("utf-8"))


def make_xml(path: Path) -> None:
    text = (
        '<?xml version="1.0" encoding="ISO-8859-1"?>\n'
        '<!DOCTYPE dossier [<!ENTITY org "Exemple">]>\n'
        f"<!-- Export réalisé par {P} -->\n"
        f'<dossier auteur="{P}" xmlns:x="urn:exemple:test">\n'
        f"  <titulaire>{P}</titulaire>\n"
        f'  <contact courriel="{E}">Écrire à {E}</contact>\n'
        f"  <note><![CDATA[Vu avec <b>{P}</b> & co]]></note>\n"
        f'  <?traitement responsable="{P}"?>\n'
        f"  <x:organisation>&org;</x:organisation>\n"
        f"  <mixte>Texte <i>libre</i> {P}</mixte>\n"
        "</dossier>\n"
    )
    path.write_bytes(text.encode("iso-8859-1"))


GENERATORS = {
    "contacts.csv": make_csv,
    "export.tsv": make_tsv,
    "dossier.json": make_json,
    "dossier.xml": make_xml,
    "rapport.docx": make_docx,
    "tableau.xlsx": make_xlsx,
    "presentation.pptx": make_pptx,
    "rapport.odt": make_odt,
    "tableau.ods": make_ods,
    "presentation.odp": make_odp,
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, generator in GENERATORS.items():
        generator(OUT / name)


if __name__ == "__main__":
    main()
