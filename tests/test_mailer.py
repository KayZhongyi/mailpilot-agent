import importlib.util
import json
from argparse import Namespace
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mailer", ROOT / "mailpilot" / "mailer.py")
mailer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mailer)


def test_column_detection_supports_chinese_headers():
    df = pd.DataFrame({"姓名": ["Ada"], "邮箱": ["ada@example.com"], "组别": ["confirmed"]})

    assert mailer._find_col(df, mailer.NAME_ALIASES) == "姓名"
    assert mailer._find_col(df, mailer.EMAIL_ALIASES) == "邮箱"
    assert mailer._find_col(df, mailer.GROUP_ALIASES) == "组别"


def test_preview_flags_invalid_email_and_missing_template(tmp_path):
    source = tmp_path / "people.csv"
    out = tmp_path / "sendable.csv"
    source.write_text(
        "name,email,group\n"
        "Ada,ada@example.com,confirmed\n"
        "Bad,not-an-email,confirmed\n"
        "Missing,missing@example.com,no_such_template\n",
        encoding="utf-8",
    )

    mailer.cmd_preview(
        Namespace(
            file=str(source),
            out=str(out),
            template="default",
            email_col=None,
            name_col=None,
            group_col=None,
        )
    )

    rows = pd.read_csv(out, dtype=object).fillna("")
    assert rows.loc[0, "sendable"] == "yes"
    assert rows.loc[1, "sendable"] == "no"
    assert rows.loc[1, "reason"] == "no valid email address"
    assert rows.loc[2, "sendable"] == "no"
    assert "template not found" in rows.loc[2, "reason"]


def test_ledger_recovery_overrides_csv_status(tmp_path):
    path = tmp_path / "sendable.csv"
    ledger = tmp_path / "sendable.csv.sendlog.jsonl"
    df = pd.DataFrame(
        [
            {
                "email": "ada@example.com",
                "status": "",
                "send_time": "",
                "send_error": "",
            }
        ]
    )
    df.to_csv(path, index=False)
    mailer._append_ledger(
        str(ledger),
        {
            "event": "send_result",
            "row": 0,
            "email": "ada@example.com",
            "status": "sent",
            "send_time": "2026-07-01 12:00:00",
            "send_error": "",
        },
    )

    recovered = pd.read_csv(path, dtype=object).fillna("")
    assert mailer._apply_ledger(recovered, str(ledger)) == 1
    assert recovered.loc[0, "status"] == "sent"
    assert recovered.loc[0, "send_time"] == "2026-07-01 12:00:00"


def test_append_ledger_is_json_lines(tmp_path):
    ledger = tmp_path / "events.jsonl"
    mailer._append_ledger(str(ledger), {"row": 3, "status": "error", "send_error": "boom"})

    line = ledger.read_text(encoding="utf-8").strip()
    event = json.loads(line)
    assert event["row"] == 3
    assert event["status"] == "error"


def test_send_syncs_recovered_ledger_when_no_new_targets(tmp_path):
    sendable = tmp_path / "sendable.csv"
    config = tmp_path / "config.yaml"
    ledger = tmp_path / "sendable.csv.sendlog.jsonl"
    sendable.write_text(
        "name,email,template,subject,body,status,sendable,reason,send_time,send_error\n"
        "Ada,ada@example.com,default,Hi,Body,,yes,,,\n",
        encoding="utf-8",
    )
    config.write_text(
        "smtp:\n"
        "  host: smtp.example.com\n"
        "  port: 465\n"
        "  use_ssl: true\n"
        "  user: sender@example.com\n"
        "  password: not-a-placeholder\n"
        "from_addr: sender@example.com\n",
        encoding="utf-8",
    )
    mailer._append_ledger(
        str(ledger),
        {
            "event": "send_result",
            "row": 0,
            "email": "ada@example.com",
            "status": "sent",
            "send_time": "2026-07-01 12:00:00",
            "send_error": "",
        },
    )

    class FakeSMTP:
        sent = []

        def sendmail(self, *args):
            self.sent.append(args)

        def quit(self):
            pass

    fake = FakeSMTP()
    original_connect = mailer.connect_smtp
    try:
        mailer.connect_smtp = lambda cfg: fake
        mailer.cmd_send(
            Namespace(
                input=str(sendable),
                config=str(config),
                only=None,
                redirect_to=None,
                limit=None,
                sleep=0,
                at=None,
                dry_run=False,
                ledger=None,
            )
        )
    finally:
        mailer.connect_smtp = original_connect

    rows = pd.read_csv(sendable, dtype=object).fillna("")
    assert rows.loc[0, "status"] == "sent"
    assert rows.loc[0, "send_time"] == "2026-07-01 12:00:00"
    assert fake.sent == []
