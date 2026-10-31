"""One e-mail exported as JSON, eml and CSV (CRM export): the same original value
gets the same substitute in every format, and dates keep their order."""

from __future__ import annotations

import base64
import csv
import dataclasses
import email
import json
from datetime import datetime, timedelta
from email.message import Message
from email.policy import compat32
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

import pytest

from anonyma import threadindex
from anonyma.config import Config
from anonyma.models import DocumentStatus, ReplacementMode
from anonyma.pipeline import Pipeline

SENDER = "Jessica Morin"
#: A digit in the domain: ``<address>`` once looked like a message id in mail headers.
ADDRESS = "jessica.morin@leads-b2b.exemple.test"
CREATED = "2026-09-01T08:14:29Z"
MODIFIED = "2026-09-01T08:17:29Z"
REPLY = "2026-09-02T09:30:00Z"
LABEL = "01/09/2026 10:14"  # Dataverse formatted value, local time
#: Thread-Index of the first message (thread started 2026-09-01 08:14:29 UTC).
PARENT_INDEX = "AQHdOenojzqcIdTmS3+aHC49T1prfA=="
#: The same indexes as a Dataverse export writes them (hexadecimal, upper case).
PARENT_HEX = base64.b64decode(PARENT_INDEX).hex().upper()
CHILD_BLOCK = "0000ab12c3"


def _child_index(parent: str) -> str:
    raw = base64.b64decode(parent) + bytes.fromhex(CHILD_BLOCK)
    return base64.b64encode(raw).decode()


def _eml(message_id: str, date: str, index: str, reply_to: str | None = None) -> str:
    lines = [
        f"From: {SENDER} <{ADDRESS}>",
        "To: Service Client <service-client@hexagone.exemple.test>",
        "Subject: Devis borne de recharge",
        f"Date: {date}",
        f"Message-ID: {message_id}",
        f"Thread-Index: {index}",
    ]
    if reply_to:
        lines += [f"In-Reply-To: {reply_to}", f"References: {reply_to}"]
    lines += [
        "MIME-Version: 1.0",
        'Content-Type: text/plain; charset="utf-8"',
        "",
        "Bonjour, suite à notre échange du 12 mars 2026,",
        SENDER,
        ADDRESS,
        "",
    ]
    return "\r\n".join(lines)


def _export(root: Path) -> Path:
    src = root / "export"
    (src / "eml").mkdir(parents=True)
    (src / "webapi").mkdir()
    (src / "csv").mkdir()
    parent_id = "<CA+be4Sz9nAek5Ckq2gjKmq@mail.gmail.com>"
    (src / "eml" / "parent.eml").write_text(
        _eml(parent_id, "Tue, 01 Sep 2026 08:14:29 +0000", PARENT_INDEX),
        encoding="utf-8",
        newline="",
    )
    (src / "eml" / "reply.eml").write_text(
        _eml(
            "<CA+zz9Qw7Lk2Mn4Pq8Rs@mail.gmail.com>",
            "Wed, 02 Sep 2026 09:30:00 +0000",
            _child_index(PARENT_INDEX),
            reply_to=parent_id,
        ),
        encoding="utf-8",
        newline="",
    )
    records = [
        {
            "subject": "Devis borne de recharge",
            "sender": ADDRESS,
            "createdon": CREATED,
            "createdon_libelle": LABEL,
            "modifiedon": MODIFIED,
            "conversationindex": PARENT_HEX,
        },
        {
            "subject": "RE: Devis",
            "sender": ADDRESS,
            "createdon": REPLY,
            "modifiedon": REPLY,
            "conversationindex": PARENT_HEX + CHILD_BLOCK.upper(),
        },
    ]
    (src / "webapi" / "page.json").write_text(
        json.dumps({"value": records}, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (src / "csv" / "emails.csv").write_text(
        "subject,createdon,sender,conversationindex\n"
        f"Devis borne de recharge,{CREATED},{ADDRESS},{PARENT_HEX}\n",
        encoding="utf-8",
    )
    return src


def _outputs(tmp_path: Path, config: Config) -> dict[str, Path]:
    result = Pipeline(config).run([_export(tmp_path)], tmp_path / "out")
    out: dict[str, Path] = {}
    for top in result.results:
        assert top.status in (DocumentStatus.ANONYMIZED, DocumentStatus.NEEDS_REVIEW), [
            i.message for i in top.issues
        ]
        assert top.output_path is not None
        out[Path(top.output_name).name] = top.output_path
    return out


def _mail(path: Path) -> Message:
    return email.message_from_bytes(path.read_bytes(), policy=compat32)


def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _thread_index(message: Message) -> bytes:
    value = message["Thread-Index"]
    assert value is not None
    return base64.b64decode(str(value))


def test_same_sender_and_instants_in_every_format(tmp_path: Path, canary_config: Config) -> None:
    config = dataclasses.replace(canary_config, mode=ReplacementMode.REALISTIC)
    out = _outputs(tmp_path, config)
    parent, reply = _mail(out["parent.eml"]), _mail(out["reply.eml"])
    records = json.loads(out["page.json"].read_text(encoding="utf-8"))["value"]
    [row] = list(csv.DictReader(out["emails.csv"].open(encoding="utf-8", newline="")))

    # H4: the header address is the substitute the JSON and the CSV have.
    [(_, from_address)] = getaddresses([str(parent["From"])])
    assert from_address != ADDRESS and "@" in from_address
    assert from_address == records[0]["sender"] == records[1]["sender"] == row["sender"]
    assert getaddresses([str(reply["From"])])[0][1] == from_address

    # H1: one whole-day offset, time of day kept, order and intervals preserved.
    created = _iso(records[0]["createdon"])
    shift = created - _iso(CREATED)
    assert shift.days != 0 and shift.seconds == 0
    assert parsedate_to_datetime(str(parent["Date"])) == created
    assert _iso(row["createdon"]) == created
    assert _iso(records[0]["modifiedon"]) - created == timedelta(minutes=3)
    assert records[0]["createdon_libelle"] == (
        datetime.strptime(LABEL, "%d/%m/%Y %H:%M") + shift
    ).strftime("%d/%m/%Y %H:%M")
    replied = _iso(records[1]["createdon"])
    assert replied - created == _iso(REPLY) - _iso(CREATED)
    assert parsedate_to_datetime(str(reply["Date"])) == replied

    # M2: Thread-Index rebuilt from the shifted start time, reply still extends it.
    first, second = _thread_index(parent), _thread_index(reply)
    original = base64.b64decode(PARENT_INDEX)
    assert len(first) == 22 and len(second) == 27 and second[:22] == first
    assert first[6:] != original[6:]  # new GUID
    assert second[22:] == bytes.fromhex(CHILD_BLOCK)  # reply delta kept
    start = threadindex.start_time(str(parent["Thread-Index"]))
    original_start = threadindex.start_time(PARENT_INDEX)
    assert start is not None and original_start is not None
    assert abs((start - original_start) - shift) < timedelta(seconds=2)

    # H4: the hexadecimal index of the JSON and CSV is the very same rebuilt index
    # (left as is, it would give away the real date and the secret offset).
    assert records[0]["conversationindex"] == first.hex().upper() == row["conversationindex"]
    assert records[1]["conversationindex"] == second.hex().upper()


@pytest.mark.parametrize("mode", [ReplacementMode.TAG, ReplacementMode.BLACKOUT])
def test_thread_index_with_neutral_dates(
    tmp_path: Path, canary_config: Config, mode: ReplacementMode
) -> None:
    out = _outputs(tmp_path, dataclasses.replace(canary_config, mode=mode))
    parent, reply = _mail(out["parent.eml"]), _mail(out["reply.eml"])
    first, second = _thread_index(parent), _thread_index(reply)
    assert second[:22] == first and first[6:] != base64.b64decode(PARENT_INDEX)[6:]
    assert second[22:26] == bytes(4)  # no interval survives a neutral date
    start = threadindex.start_time(str(parent["Thread-Index"]))
    assert start is not None and start.year == 1970
    assert parsedate_to_datetime(str(parent["Date"])).year == 1970
