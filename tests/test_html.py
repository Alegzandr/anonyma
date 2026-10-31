from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest
from PIL import Image

from anonyma import ocr
from anonyma.config import Config
from anonyma.extractors._html import HtmlSource, build_segments, plan_edits
from anonyma.models import DocumentStatus, OcrLine, Replacement, ReplacementPlan, SegmentKind
from anonyma.pipeline import Pipeline, extract_file
from anonyma.textutils import find_all
from tests.canary import CANARY_EMAIL, CANARY_PERSON


def _plan(source: HtmlSource, values: list[str], substitute: str = "[X_001]") -> ReplacementPlan:
    plan = ReplacementPlan("doc")
    for segment in build_segments(source, "h"):
        reps: list[Replacement] = []
        for value in values:
            for start, end in find_all(segment.text, value):
                if not any(r.start < end and start < r.end for r in reps):
                    reps.append(
                        Replacement(start, end, segment.text[start:end], substitute, "X", "c")
                    )
        if reps:
            plan.by_segment[segment.id] = sorted(reps, key=lambda r: r.start)
    return plan


def _rewrite(html: str, values: list[str], substitute: str = "[X_001]") -> str:
    source = HtmlSource(html)
    segments = build_segments(source, "h")
    return source.rewrite(plan_edits(source, segments, _plan(source, values, substitute)))


def test_segments_cover_text_attributes_comments_scripts_title() -> None:
    html = (
        "<html><head><title>Dossier Z&eacute;phyrin</title>"
        '<meta name="author" content="Z&#233;phyrin">'
        '<script type="application/ld+json">{"name": "Zéphyrin"}</script></head>'
        "<body><!-- note Zéphyrin --><p>Bonjour <b>Z&eacute;phyrin</b> !</p>"
        "<a href=\"mailto:a%40exemple.test\" title='Zéphyrin'>x</a>"
        '<input value=Zéphyrin placeholder="Zéphyrin" data-owner="Zéphyrin"></body></html>'
    )
    segments = build_segments(HtmlSource(html), "h")
    by_label = {(s.context.kind, s.context.label): s.text for s in segments}
    assert by_label[(SegmentKind.HEADER, "titre")] == "Dossier Zéphyrin"
    assert by_label[(SegmentKind.ATTRIBUTE, "content")] == "Zéphyrin"
    assert by_label[(SegmentKind.ATTRIBUTE, "href")] == "mailto:a@exemple.test"
    for label in ("title", "value", "placeholder", "data-owner"):
        assert by_label[(SegmentKind.ATTRIBUTE, label)] == "Zéphyrin"
    assert "Zéphyrin" in by_label[(SegmentKind.COMMENT, "commentaire")]
    assert "Zéphyrin" in by_label[(SegmentKind.HIDDEN, "script")]
    texts = [s.text for s in segments if s.context.kind is SegmentKind.BODY]
    assert "Bonjour Zéphyrin !" in texts  # one segment across inline tags
    assert not any(s.context.label == "type" for s in segments)


def test_rewrite_keeps_markup_byte_for_byte() -> None:
    html = (
        '<!DOCTYPE html>\n<p class="a"  id=x>Jean <b>Z&eacute;phyrin</b>&nbsp;Quintavalle'
        "<br/>suite &amp; fin</p>\n<!-- rien -->"
    )
    out = _rewrite(html, ["Zéphyrin Quintavalle"])
    assert out == (
        '<!DOCTYPE html>\n<p class="a"  id=x>Jean <b>[X_001]</b>'
        "<br/>suite &amp; fin</p>\n<!-- rien -->"
    )
    assert _rewrite(html, []) == html


def test_attribute_quoting_and_escaping() -> None:
    html = "<a title=Dupont alt='Dupont' data-x=\"Dupont\">t</a>"
    out = _rewrite(html, ["Dupont"], 'Jean d\'Arc "&"')
    assert "title=Jean&#32;d&#39;Arc&#32;&#34;&amp;&#34;" in out
    assert "alt='Jean d&#39;Arc \"&amp;\"'" in out
    assert 'data-x="Jean d\'Arc &quot;&amp;&quot;"' in out
    reread = build_segments(HtmlSource(out), "h")
    assert {s.text for s in reread if s.context.kind is SegmentKind.ATTRIBUTE} == {
        'Jean d\'Arc "&"'
    }


def test_text_escaping_and_percent_encoded_urls() -> None:
    html = '<a href="mailto:x?subject=Z%C3%A9phyrin%20Quintavalle">Z&eacute;phyrin Quintavalle</a>'
    out = _rewrite(html, [CANARY_PERSON], "<Jean & Paul>")
    assert "&lt;Jean &amp; Paul&gt;</a>" in out
    assert "subject=%3CJean%20&amp;%20Paul%3E" in out
    href = next(s for s in build_segments(HtmlSource(out), "h") if s.context.label == "href")
    assert href.text == "mailto:x?subject=<Jean & Paul>"


def test_data_uri_is_not_a_segment() -> None:
    source = HtmlSource('<img alt="x" src="data:image/png;base64,iVBORw0KGgo=">')
    assert [d.mime for d in source.data_uris] == ["image/png"]
    assert all(s.context.label != "src" for s in build_segments(source, "h"))


def _run(tmp_path: Path, name: str, data: bytes, config: Config) -> tuple[Path, str]:
    src = tmp_path / "in"
    src.mkdir()
    (src / name).write_bytes(data)
    result = Pipeline(config).run([src / name], tmp_path / "out")
    top = result.results[0]
    assert top.status is DocumentStatus.ANONYMIZED, [i.message for i in top.issues]
    assert top.output_path is not None
    return top.output_path, top.output_name


def test_mailto_attribute_leak_is_anonymized(tmp_path: Path, canary_config: Config) -> None:
    html = (
        f'<p>Contact : <a href="mailto:{CANARY_EMAIL}" title="{CANARY_PERSON}">ici</a></p>'
    ).encode()
    output, _ = _run(tmp_path, "page.html", html, canary_config)
    text = output.read_text(encoding="utf-8")
    assert "uintavalle" not in text.casefold()
    assert 'href="mailto:[EMAIL_001]"' in text


def test_legacy_charset_is_kept(tmp_path: Path, canary_config: Config) -> None:
    html = (
        '<html><head><meta charset="iso-8859-1"></head><body>'
        f"<p>{CANARY_PERSON} &eacute;crit</p></body></html>"
    ).encode("latin-1")
    output, name = _run(tmp_path, "ancien.htm", html, canary_config)
    data = output.read_bytes()
    assert b"[PERSONNE_001] &eacute;crit" in data
    assert data.startswith(b'<html><head><meta charset="iso-8859-1">')
    reread = extract_file(output, name, canary_config, tmp_path / "re")
    assert reread.status is DocumentStatus.EXTRACTED


@pytest.mark.parametrize("quote", ['"', "'"])
def test_unterminated_markup_does_not_crash(quote: str) -> None:
    source = HtmlSource(f"<p title={quote}Dupont<b>texte <!-- fin")
    segments = build_segments(source, "h")
    assert any("Dupont" in s.text for s in segments)


class _NoText:
    def recognize(self, image: Image.Image) -> list[OcrLine]:
        return []


def test_data_uri_image_becomes_child(
    tmp_path: Path, canary_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ocr, "create_engine", lambda config: _NoText())
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, "PNG")
    payload = base64.b64encode(buffer.getvalue()).decode()
    html = f'<p>{CANARY_PERSON}</p><img src="data:image/png;base64,{payload}">'.encode()
    src = tmp_path / "in"
    src.mkdir()
    (src / "p.html").write_bytes(html)
    top = Pipeline(canary_config).run([src / "p.html"], tmp_path / "out").results[0]
    assert top.status is DocumentStatus.ANONYMIZED, [i.message for i in top.issues]
    assert [c.format for c in top.children] == ["png"]
    assert top.output_path is not None
    out = top.output_path.read_text(encoding="utf-8")
    assert out.startswith('<p>[PERSONNE_001]</p><img src="data:image/png;base64,')


# -- markup a browser hides must never survive unseen ----------------------------------


def test_text_like_tags_are_text_and_escaped() -> None:
    html = f"<p>De : {CANARY_PERSON} <{CANARY_EMAIL}> et <Jean Dupont></p><o:p></o:p>"
    source = HtmlSource(html)
    texts = [s.text for s in build_segments(source, "h")]
    assert texts == [f"De : {CANARY_PERSON} <{CANARY_EMAIL}> et <Jean Dupont>"]
    out = _rewrite(html, [CANARY_EMAIL, "Jean Dupont"])
    assert out == "<p>De : Zéphyrin Quintavalle &lt;[X_001]> et &lt;[X_001]></p><o:p></o:p>"
    # Unchanged text-like tags are escaped too: the output reads as it was analysed.
    assert _rewrite("<p><Jean Dupont></p>", []) == "<p>&lt;Jean Dupont></p>"
    assert _rewrite("<p></Jean Dupont></p>", []) == "<p>&lt;/Jean Dupont></p>"


@pytest.mark.parametrize(
    ("html", "hidden"),
    [
        (f"<!DOCTYPE html PUBLIC '-//{CANARY_PERSON}//FR'><p>x</p>", "balisage"),
        (f"<p>x</p><?note {CANARY_PERSON} ?>", "balisage"),
        (f"<p>x</p><!{CANARY_PERSON}>", "balisage"),
        (f'<p>x</p title="{CANARY_PERSON}">', "balise"),
        ("<p data-zephyrin-quintavalle>x</p>", "nom d'attribut"),
        ("<zephyrin-quintavalle>x</zephyrin-quintavalle>", "balise"),
    ],
)
def test_hidden_markup_is_exposed_and_dropped_when_touched(html: str, hidden: str) -> None:
    segments = build_segments(HtmlSource(html), "h")
    exposed = [s for s in segments if s.context.label == hidden]
    assert exposed and all(s.context.kind is SegmentKind.HIDDEN for s in exposed)
    assert _rewrite(html, []) == html  # untouched: kept byte for byte
    out = _rewrite(html, ["Zéphyrin Quintavalle", "zephyrin-quintavalle"])
    assert "uintavalle" not in out.casefold()
    assert "x" in out


def test_standard_markup_is_not_exposed() -> None:
    html = (
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" '
        '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">'
        '<p class=MsoNormal style="x" aria-label="y" onclick="z" xml:lang=fr>t<o:p></o:p>'
        '<st1:PersonName w:st="on">t</st1:PersonName></p><svg viewBox="0 0 1 1"></svg>'
    )
    segments = build_segments(HtmlSource(html), "h")
    assert not [s for s in segments if s.context.kind is SegmentKind.HIDDEN]


def test_html_mail_markup_leaks_are_anonymized(tmp_path: Path, canary_config: Config) -> None:
    mail = (
        "From: a@exemple.test\nTo: b@exemple.test\nSubject: x\nMIME-Version: 1.0\n"
        "Content-Type: text/html; charset=utf-8\n\n"
        f"<html><body><p>De : {CANARY_PERSON} <{CANARY_EMAIL}></p>"
        f"<!DOCTYPE {CANARY_PERSON}><?pi {CANARY_EMAIL}?></body></html>\n"
    ).encode()
    output, _ = _run(tmp_path, "m.eml", mail, canary_config)
    data = output.read_bytes().decode("ascii", "replace").casefold()
    assert "uintavalle" not in data.replace("=\n", "")
    assert "&lt;[email_003]></p></body>" in data.replace("=\n", "")
