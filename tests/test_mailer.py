import importlib.util
import io
import json
import smtplib
import threading
from argparse import Namespace
from pathlib import Path

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mailer", ROOT / "mailpilot" / "mailer.py")
mailer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mailer)
APP_SPEC = importlib.util.spec_from_file_location("mailpilot_web_app", ROOT / "app.py")
web_app = importlib.util.module_from_spec(APP_SPEC)
APP_SPEC.loader.exec_module(web_app)


def _write_config(path):
    path.write_text(
        "smtp:\n"
        "  host: smtp.example.com\n"
        "  port: 465\n"
        "  use_ssl: true\n"
        "  user: sender@example.com\n"
        "  password: not-a-placeholder\n"
        "from_addr: sender@example.com\n",
        encoding="utf-8",
    )


def _seal_sendable(path, engine=mailer):
    df = pd.read_csv(path, dtype=object).fillna("")
    for column, default in (("status", ""), ("sendable", "yes"), ("reason", "")):
        if column not in df.columns:
            df[column] = default
    if "initial_status" not in df.columns:
        df["initial_status"] = df["status"]
    df["record_id"] = [
        engine._record_id(
            idx,
            str(row.get("email", "")).strip(),
            str(row.get("template", "")).strip(),
            str(row.get("subject", "")),
            str(row.get("body", "")),
            str(row.get("sendable", "")).strip().lower(),
            str(row.get("reason", "")),
            str(row.get("initial_status", "")).strip().lower(),
            str(row.get("activity_id", "")).strip(),
        )
        for idx, row in df.iterrows()
    ]
    df["batch_id"] = engine._batch_id_for_record_ids(df["record_id"].astype(str))
    columns = ["batch_id", "record_id"] + [
        column for column in df.columns if column not in ("batch_id", "record_id")
    ]
    df[columns].to_csv(path, index=False)


def _send_args(sendable, config, **overrides):
    values = {
        "input": str(sendable),
        "config": str(config),
        "only": None,
        "redirect_to": None,
        "limit": 1,
        "sleep": 1,
        "at": None,
        "dry_run": False,
        "ledger": None,
    }
    values.update(overrides)
    return Namespace(**values)


def _prepare_web_run(tmp_path, row_count=3):
    web_app.RUNS_DIR = tmp_path
    run_dir = tmp_path / "mailpilot_test_run"
    run_dir.mkdir()
    sendable = run_dir / "sendable.csv"
    rows = [
        f"user{i}@example.com,default,Hi,Body,,yes" for i in range(row_count)
    ]
    sendable.write_text(
        "email,template,subject,body,status,sendable\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    _seal_sendable(sendable, engine=web_app.mailer)
    reviewed = pd.read_csv(sendable, dtype=object).fillna("")
    web_app._write_manifest(
        run_dir,
        source_hash="test-source-hash",
        content_hash=web_app._content_hash(reviewed),
        batch_fingerprint=web_app._batch_fingerprint(reviewed),
    )
    token = web_app._new_action_token(run_dir)
    return run_dir, sendable, token


def _web_send_form(run_dir, sendable, token, **overrides):
    values = {
        "sendable_path": str(sendable),
        "run_dir": str(run_dir),
        "action_token": token,
        "action": "send",
        "host": "smtp.example.com",
        "port": "465",
        "use_ssl": "on",
        "user": "sender@example.com",
        "password": "app-password",
        "from_addr": "sender@example.com",
        "redirect_to": "",
        "only": "",
        "limit": "1",
        "sleep": "1",
        "confirm_real_send": "on",
        "confirm_test_received": "on",
        "confirm_provider_quota": "on",
        "confirm_phrase": "发送 1 封",
    }
    values.update(overrides)
    return values


def _fake_bounce_extraction(bounces, complete=True, matching_messages=12):
    normalized = {}
    for address, value in bounces.items():
        item = dict(value)
        item.setdefault("verified_failure", True)
        item.setdefault("action", "failed")
        item.setdefault("status", "5.1.1")
        item.setdefault("bounce_class", "hard")
        normalized[address] = item
    return {
        "bounces": normalized,
        "unverified_candidates": [],
        "non_permanent": [],
        "coverage": {
            "mailbox": "INBOX",
            "uidvalidity": "test-uidvalidity-1",
            "query_since": "13-Jul-2026",
            "first_uid": "1",
            "last_uid": str(matching_messages),
            "matching_messages": matching_messages,
            "scanned_messages": matching_messages if complete else 1,
            "complete": complete,
        },
    }


def _dsn_message(
    *,
    action="failed",
    status="5.1.1",
    recipient="bad@example.com",
    message_id="<mailpilot.test@example.com>",
):
    action_line = f"Action: {action}\r\n" if action is not None else ""
    status_line = f"Status: {status}\r\n" if status is not None else ""
    return (
        "From: Mail Delivery Subsystem <mailer-daemon@example.net>\r\n"
        "Subject: Delivery Status Notification\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/report; report-type=delivery-status; boundary="dsn"\r\n'
        "\r\n"
        "--dsn\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        "Delivery report.\r\n"
        "--dsn\r\n"
        "Content-Type: message/delivery-status\r\n\r\n"
        "Reporting-MTA: dns; mx.example.net\r\n\r\n"
        f"Final-Recipient: rfc822; {recipient}\r\n"
        f"{action_line}"
        f"{status_line}"
        f"Diagnostic-Code: smtp; {status or 'unknown'} simulated response\r\n"
        "\r\n"
        "--dsn\r\n"
        "Content-Type: message/rfc822\r\n\r\n"
        "From: sender@example.com\r\n"
        f"To: {recipient}\r\n"
        f"Message-ID: {message_id}\r\n"
        "Subject: Original message\r\n\r\n"
        "Body\r\n"
        "--dsn--\r\n"
    ).encode("utf-8")


def _trusted_sent_batch(tmp_path, address="bad@example.com"):
    sendable = tmp_path / "sendable.csv"
    sendable.write_text(
        "email,template,subject,body,status,sendable,reason\n"
        f"{address},default,Hi,Body,,yes,\n",
        encoding="utf-8",
    )
    _seal_sendable(sendable)
    rows = pd.read_csv(sendable, dtype=object).fillna("")
    ledger = mailer._default_ledger_path(str(sendable))
    mailer._ensure_ledger_anchor(ledger, str(rows.loc[0, "batch_id"]))
    mailer._append_ledger(
        ledger,
        {
            "event": "send_result",
            "row": 0,
            "batch_id": str(rows.loc[0, "batch_id"]),
            "record_id": str(rows.loc[0, "record_id"]),
            "email": address,
            "status": "sent",
            "send_time": "2026-07-13T12:00:00+08:00",
        },
    )
    expected_message_id = mailer._send_message_id(
        str(rows.loc[0, "record_id"]), "sender@example.com"
    )
    args = Namespace(
        input=str(sendable),
        config="",
        config_data={
            "smtp": {
                "host": "smtp.example.com",
                "user": "smtp-user@example.com",
                "password": "smtp-password",
            },
            "imap": {
                "host": "imap.example.com",
                "port": 993,
                "user": "bounce-reader@example.com",
                "password": "imap-password",
            },
            "from_addr": "sender@example.com",
        },
        ledger=None,
        lookback=50,
        apply=False,
    )
    return sendable, rows, expected_message_id, args


def test_column_detection_supports_chinese_headers():
    df = pd.DataFrame({"姓名": ["Ada"], "邮箱": ["ada@example.com"], "组别": ["confirmed"]})

    assert mailer._find_col(df, mailer.NAME_ALIASES) == "姓名"
    assert mailer._find_col(df, mailer.EMAIL_ALIASES) == "邮箱"
    assert mailer._find_col(df, mailer.GROUP_ALIASES) == "组别"


def test_web_column_preflight_returns_safe_local_suggestions(tmp_path):
    web_app.RUNS_DIR = tmp_path
    web_app.app.config.update(TESTING=True)
    response = web_app.app.test_client().post(
        "/columns",
        data={
            "recipients": (
                io.BytesIO("姓名,邮箱,邮件类型,已发送\nAda,a@example.com,confirmed,否\n".encode()),
                "people.csv",
            )
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["columns"] == ["姓名", "邮箱", "邮件类型", "已发送"]
    assert payload["suggestions"] == {
        "email": ["邮箱"],
        "name": ["姓名"],
        "group": ["邮件类型"],
        "sent": ["已发送"],
    }


def test_email_detection_prefers_exact_column_and_blocks_ambiguous_candidates(tmp_path):
    exact = pd.DataFrame(
        {"backup_email": ["backup@example.com"], "email": ["right@example.com"]}
    )
    assert mailer._find_col(exact, mailer.EMAIL_ALIASES) == "email"

    source = tmp_path / "ambiguous.csv"
    source.write_text(
        "name,customer_email,dealer_email\nAda,customer@example.com,dealer@example.com\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="Multiple possible email columns"):
        mailer.cmd_preview(
            Namespace(
                file=str(source),
                out=str(tmp_path / "sendable.csv"),
                template="default",
                email_col=None,
                name_col="name",
                group_col="__none__",
                sent_col="__none__",
            )
        )


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
    _seal_sendable(sendable)
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
            "batch_id": str(pd.read_csv(sendable).loc[0, "batch_id"]),
            "record_id": str(pd.read_csv(sendable).loc[0, "record_id"]),
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


def test_preview_can_disable_group_auto_detection(tmp_path):
    source = tmp_path / "people.csv"
    out = tmp_path / "sendable.csv"
    source.write_text(
        "name,email,status\n"
        "Ada,ada@example.com,confirmed\n",
        encoding="utf-8",
    )

    mailer.cmd_preview(
        Namespace(
            file=str(source),
            out=str(out),
            template="default",
            email_col=None,
            name_col=None,
            group_col="__none__",
        )
    )

    rows = pd.read_csv(out, dtype=object).fillna("")
    assert rows.loc[0, "template"] == "default"
    assert rows.loc[0, "sendable"] == "yes"


def test_preview_blocks_blank_group_and_duplicate_email(tmp_path):
    source = tmp_path / "people.csv"
    out = tmp_path / "sendable.csv"
    source.write_text(
        "name,email,group\n"
        "Ada,ada@example.com,confirmed\n"
        "Ada again,ada@example.com,confirmed\n"
        "No group,nogroup@example.com,\n",
        encoding="utf-8",
    )

    mailer.cmd_preview(
        Namespace(
            file=str(source),
            out=str(out),
            template="default",
            email_col="email",
            name_col="name",
            group_col="group",
            sent_col="__none__",
        )
    )

    rows = pd.read_csv(out, dtype=object).fillna("")
    assert rows.loc[0, "sendable"] == "no"
    assert rows.loc[1, "reason"] == "duplicate email address in this batch"
    assert rows.loc[2, "sendable"] == "no"
    assert "missing business classification" in rows.loc[2, "reason"]


def test_preview_rejects_unknown_explicit_group_column(tmp_path):
    source = tmp_path / "people.csv"
    source.write_text("name,email,group\nAda,ada@example.com,confirmed\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="Group/template column not found"):
        mailer.cmd_preview(
            Namespace(
                file=str(source),
                out=str(tmp_path / "sendable.csv"),
                template="default",
                email_col="email",
                name_col="name",
                group_col="typo",
                sent_col="__none__",
            )
        )


def test_redirect_test_does_not_change_production_status(tmp_path):
    sendable = tmp_path / "sendable.csv"
    config = tmp_path / "config.yaml"
    sendable.write_text(
        "email,template,subject,body,status,sendable\n"
        "ada@example.com,default,Hi,Body,,yes\n",
        encoding="utf-8",
    )
    _seal_sendable(sendable)
    _write_config(config)

    class FakeSMTP:
        actual = []

        def sendmail(self, _from, recipients, _message):
            self.actual.extend(recipients)
            return {}

        def quit(self):
            pass

    fake = FakeSMTP()
    original_connect = mailer.connect_smtp
    try:
        mailer.connect_smtp = lambda cfg: fake
        mailer.cmd_send(
            _send_args(
                sendable,
                config,
                redirect_to="sender@example.com",
                limit=1,
                sleep=0,
            )
        )
    finally:
        mailer.connect_smtp = original_connect

    rows = pd.read_csv(sendable, dtype=object).fillna("")
    assert rows.loc[0, "status"] == ""
    assert fake.actual == ["sender@example.com"]
    assert not Path(f"{sendable}.sendlog.jsonl").exists()
    events = [
        json.loads(line)
        for line in Path(f"{sendable}.testlog.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["status"] == "test_sent"


def test_cli_redirect_test_rejects_customer_address(tmp_path):
    sendable = tmp_path / "sendable.csv"
    config = tmp_path / "config.yaml"
    sendable.write_text(
        "email,template,subject,body,status,sendable\n"
        "sender@example.com,default,Hi,Body,,yes\n",
        encoding="utf-8",
    )
    _seal_sendable(sendable)
    _write_config(config)

    with pytest.raises(SystemExit, match="also a customer recipient"):
        mailer.cmd_send(
            _send_args(
                sendable,
                config,
                redirect_to="sender@example.com",
                limit=1,
                sleep=0,
            )
        )

    assert not Path(mailer._default_test_ledger_path(str(sendable))).exists()


def test_limit_caps_attempts_even_when_every_send_fails(tmp_path):
    sendable = tmp_path / "sendable.csv"
    config = tmp_path / "config.yaml"
    sendable.write_text(
        "email,template,subject,body,status,sendable\n"
        "a@example.com,default,Hi,Body,,yes\n"
        "b@example.com,default,Hi,Body,,yes\n"
        "c@example.com,default,Hi,Body,,yes\n",
        encoding="utf-8",
    )
    _seal_sendable(sendable)
    _write_config(config)

    class RejectingSMTP:
        calls = 0

        def sendmail(self, _from, recipients, _message):
            self.calls += 1
            raise smtplib.SMTPRecipientsRefused({recipients[0]: (550, b"rejected")})

        def quit(self):
            pass

    fake = RejectingSMTP()
    original_connect = mailer.connect_smtp
    original_sleep = mailer.time.sleep
    try:
        mailer.connect_smtp = lambda cfg: fake
        mailer.time.sleep = lambda _seconds: None
        mailer.cmd_send(_send_args(sendable, config, limit=2, sleep=1))
    finally:
        mailer.connect_smtp = original_connect
        mailer.time.sleep = original_sleep

    assert fake.calls == 2
    rows = pd.read_csv(sendable, dtype=object).fillna("")
    assert list(rows["status"]) == ["error", "error", ""]


def test_bounced_row_is_suppressed_from_future_sends(tmp_path):
    sendable = tmp_path / "sendable.csv"
    config = tmp_path / "config.yaml"
    sendable.write_text(
        "email,template,subject,body,status,sendable\n"
        "bounce@example.com,default,Hi,Body,bounced,yes\n",
        encoding="utf-8",
    )
    _seal_sendable(sendable)
    _write_config(config)

    mailer.cmd_send(_send_args(sendable, config, limit=None, sleep=0))


def test_inflight_checkpoint_recovers_as_unknown(tmp_path):
    sendable = tmp_path / "sendable.csv"
    ledger = tmp_path / "sendable.csv.sendlog.jsonl"
    sendable.write_text(
        "email,template,subject,body,status,sendable\n"
        "ada@example.com,default,Hi,Body,,yes\n",
        encoding="utf-8",
    )
    mailer._append_ledger(
        str(ledger),
        {"row": 0, "email": "ada@example.com", "status": "attempting"},
    )

    rows = pd.read_csv(sendable, dtype=object).fillna("")
    assert mailer._apply_ledger(rows, str(ledger)) == 1
    assert rows.loc[0, "status"] == "unknown"


def test_damaged_ledger_fails_closed(tmp_path):
    rows = pd.DataFrame([{"email": "ada@example.com", "status": ""}])
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        '{"row": 0, "email": "ada@example.com", "status": "sent"}\n{"broken"',
        encoding="utf-8",
    )

    with pytest.raises(mailer.LedgerIntegrityError, match="damaged"):
        mailer._apply_ledger(rows, str(ledger))


def test_unknown_or_identity_free_ledger_event_fails_closed(tmp_path):
    rows = pd.DataFrame([{"email": "ada@example.com", "status": ""}])
    unknown = tmp_path / "unknown.jsonl"
    unknown.write_text(
        '{"row": 0, "email": "ada@example.com", "status": "snt"}\n',
        encoding="utf-8",
    )
    with pytest.raises(mailer.LedgerIntegrityError, match="unknown status"):
        mailer._apply_ledger(rows.copy(), str(unknown))

    missing_identity = tmp_path / "missing-identity.jsonl"
    missing_identity.write_text('{"row": 0, "status": "sent"}\n', encoding="utf-8")
    with pytest.raises(mailer.LedgerIntegrityError, match="no email identity"):
        mailer._apply_ledger(rows.copy(), str(missing_identity))


def test_send_rejects_content_changed_after_preview(tmp_path):
    sendable = tmp_path / "sendable.csv"
    config = tmp_path / "config.yaml"
    sendable.write_text(
        "email,template,subject,body,status,sendable,reason\n"
        "ada@example.com,default,Hi,Body,,yes,\n",
        encoding="utf-8",
    )
    _seal_sendable(sendable)
    rows = pd.read_csv(sendable, dtype=object).fillna("")
    rows.at[0, "subject"] = "Changed after review"
    rows.to_csv(sendable, index=False)
    _write_config(config)

    with pytest.raises(mailer.LedgerIntegrityError, match="Reviewed content mismatch"):
        mailer.cmd_send(
            _send_args(sendable, config, redirect_to="test@example.com", limit=1, sleep=0)
        )


def test_batch_seal_detects_deleted_rows(tmp_path):
    sendable = tmp_path / "sendable.csv"
    sendable.write_text(
        "email,template,subject,body,status,sendable,reason\n"
        "a@example.com,default,Hi,Body,,yes,\n"
        "b@example.com,default,Hi,Body,,yes,\n",
        encoding="utf-8",
    )
    _seal_sendable(sendable)
    rows = pd.read_csv(sendable, dtype=object).fillna("").iloc[:1]

    with pytest.raises(mailer.LedgerIntegrityError, match="Batch seal mismatch"):
        mailer._validate_reviewed_batch(rows)


def test_batch_seal_rejects_one_blank_batch_id(tmp_path):
    sendable = tmp_path / "sendable.csv"
    sendable.write_text(
        "email,template,subject,body,status,sendable,reason\n"
        "a@example.com,default,Hi,Body,,yes,\n"
        "b@example.com,default,Hi,Body,,yes,\n",
        encoding="utf-8",
    )
    _seal_sendable(sendable)
    rows = pd.read_csv(sendable, dtype=object).fillna("")
    rows.at[0, "batch_id"] = ""

    with pytest.raises(mailer.LedgerIntegrityError, match="exactly one non-empty batch_id"):
        mailer._validate_reviewed_batch(rows)


def test_initial_sent_status_cannot_be_cleared_to_resend(tmp_path):
    sendable = tmp_path / "sendable.csv"
    config = tmp_path / "config.yaml"
    sendable.write_text(
        "email,template,subject,body,status,sendable,reason\n"
        "a@example.com,default,Hi,Body,sent,yes,\n",
        encoding="utf-8",
    )
    _seal_sendable(sendable)
    rows = pd.read_csv(sendable, dtype=object).fillna("")
    rows.at[0, "status"] = ""
    rows.to_csv(sendable, index=False)
    _write_config(config)

    mailer.cmd_send(_send_args(sendable, config, limit=1, sleep=1))

    assert not Path(mailer._default_ledger_path(str(sendable))).exists()


def test_missing_started_production_ledger_blocks_recovery(tmp_path):
    ledger = tmp_path / "sendable.csv.sendlog.jsonl"
    mailer._ensure_ledger_anchor(str(ledger), "batch-1")

    with pytest.raises(mailer.LedgerIntegrityError, match="ledger is missing"):
        list(mailer._iter_ledger(str(ledger)) or [])


def test_empty_or_test_only_production_ledger_blocks_recovery(tmp_path):
    empty_ledger = tmp_path / "empty.sendlog.jsonl"
    mailer._ensure_ledger_anchor(str(empty_ledger), "batch-1")
    empty_ledger.touch()
    with pytest.raises(mailer.LedgerIntegrityError, match="empty"):
        list(mailer._iter_ledger(str(empty_ledger)) or [])

    test_only_ledger = tmp_path / "test-only.sendlog.jsonl"
    mailer._ensure_ledger_anchor(str(test_only_ledger), "batch-2")
    mailer._append_ledger(
        str(test_only_ledger),
        {"row": 0, "email": "a@example.com", "status": "test_sent"},
    )
    rows = pd.DataFrame([{"email": "a@example.com", "status": ""}])
    with pytest.raises(mailer.LedgerIntegrityError, match="unknown status"):
        mailer._apply_ledger(rows, str(test_only_ledger))


def test_complete_production_ledger_event_deletion_is_fail_closed(tmp_path):
    ledger = tmp_path / "sendable.csv.sendlog.jsonl"
    mailer._ensure_ledger_anchor(str(ledger), "batch-1")
    for row in (0, 1):
        mailer._append_ledger(
            str(ledger),
            {
                "row": row,
                "email": f"user{row}@example.com",
                "status": "sent",
            },
        )

    integrity = Path(mailer._ledger_integrity_path(str(ledger)))
    assert integrity.exists()
    lines = ledger.read_text(encoding="utf-8").splitlines()
    ledger.write_text(lines[1] + "\n", encoding="utf-8")

    with pytest.raises(mailer.LedgerIntegrityError, match="does not match"):
        list(mailer._iter_ledger(str(ledger)) or [])

    integrity.unlink()
    with pytest.raises(mailer.LedgerIntegrityError, match="marker is missing"):
        list(mailer._iter_ledger(str(ledger)) or [])


def test_one_complete_ledger_tail_recovers_if_integrity_update_lost_power(tmp_path):
    ledger = tmp_path / "sendable.csv.sendlog.jsonl"
    mailer._ensure_ledger_anchor(str(ledger), "batch-1")
    mailer._append_ledger(
        str(ledger),
        {"event": "batch_started", "batch_id": "batch-1", "status": "batch_started"},
    )
    original_atomic = mailer._atomic_write_json_file

    def fail_integrity_update(path, payload):
        if path == mailer._ledger_integrity_path(str(ledger)) and payload.get(
            "event_count"
        ) == 2:
            raise OSError("simulated power loss before integrity update")
        return original_atomic(path, payload)

    try:
        mailer._atomic_write_json_file = fail_integrity_update
        with pytest.raises(OSError, match="power loss"):
            mailer._append_ledger(
                str(ledger),
                {
                    "event": "send_attempt",
                    "row": 0,
                    "email": "a@example.com",
                    "status": "attempting",
                },
            )
    finally:
        mailer._atomic_write_json_file = original_atomic

    stale_integrity = json.loads(
        Path(mailer._ledger_integrity_path(str(ledger))).read_text(encoding="utf-8")
    )
    assert stale_integrity["event_count"] == 1
    recovered = list(mailer._iter_ledger(str(ledger)) or [])
    repaired_integrity = json.loads(
        Path(mailer._ledger_integrity_path(str(ledger))).read_text(encoding="utf-8")
    )
    assert [event["event"] for event in recovered] == ["batch_started", "send_attempt"]
    assert repaired_integrity["event_count"] == 2


def test_complete_production_evidence_removal_is_fail_closed(tmp_path):
    sendable = tmp_path / "sendable.csv"
    config = tmp_path / "config.yaml"
    sendable.write_text(
        "email,template,subject,body,status,sendable,reason\n"
        "a@example.com,default,Hi,Body,,yes,\n",
        encoding="utf-8",
    )
    _seal_sendable(sendable)
    _write_config(config)

    class FakeSMTP:
        def __init__(self):
            self.accepted = []

        def sendmail(self, _from, recipients, _message):
            self.accepted.extend(recipients)
            return {}

        def quit(self):
            pass

    first = FakeSMTP()
    original_connect = mailer.connect_smtp
    original_sleep = mailer.time.sleep
    try:
        mailer.connect_smtp = lambda cfg: first
        mailer.time.sleep = lambda _seconds: None
        mailer.cmd_send(_send_args(sendable, config, limit=1, sleep=1))
    finally:
        mailer.connect_smtp = original_connect
        mailer.time.sleep = original_sleep
    assert first.accepted == ["a@example.com"]

    stale = pd.read_csv(sendable, dtype=object).fillna("")
    stale.at[0, "status"] = ""
    stale.at[0, "send_time"] = ""
    stale.at[0, "send_error"] = ""
    mailer._atomic_write_csv(stale, str(sendable))
    ledger = mailer._default_ledger_path(str(sendable))
    Path(ledger).unlink()
    Path(mailer._ledger_anchor_path(ledger)).unlink()
    Path(mailer._ledger_integrity_path(ledger)).unlink()

    second = FakeSMTP()
    try:
        mailer.connect_smtp = lambda cfg: second
        with pytest.raises(mailer.LedgerIntegrityError, match="start marker is missing"):
            mailer.cmd_send(_send_args(sendable, config, limit=1, sleep=1))
    finally:
        mailer.connect_smtp = original_connect
    assert second.accepted == []


def test_smtp_connection_failure_can_retry_without_unsafe_reset(tmp_path):
    sendable = tmp_path / "sendable.csv"
    config = tmp_path / "config.yaml"
    sendable.write_text(
        "email,template,subject,body,status,sendable,reason\n"
        "a@example.com,default,Hi,Body,,yes,\n",
        encoding="utf-8",
    )
    _seal_sendable(sendable)
    _write_config(config)

    original_connect = mailer.connect_smtp
    original_sleep = mailer.time.sleep
    try:
        mailer.connect_smtp = lambda cfg: (_ for _ in ()).throw(
            ConnectionError("wrong password or offline")
        )
        with pytest.raises(ConnectionError, match="wrong password"):
            mailer.cmd_send(_send_args(sendable, config, limit=1, sleep=1))

        class FakeSMTP:
            accepted = []

            def sendmail(self, _from, recipients, _message):
                self.accepted.extend(recipients)
                return {}

            def quit(self):
                pass

        recovered = FakeSMTP()
        mailer.connect_smtp = lambda cfg: recovered
        mailer.time.sleep = lambda _seconds: None
        mailer.cmd_send(_send_args(sendable, config, limit=1, sleep=1))
    finally:
        mailer.connect_smtp = original_connect
        mailer.time.sleep = original_sleep

    assert recovered.accepted == ["a@example.com"]
    events = list(
        mailer._iter_ledger(mailer._default_ledger_path(str(sendable))) or []
    )
    assert events[0]["event"] == "batch_started"
    assert [event["status"] for event in events if event.get("row") == 0][-1] == "sent"


def test_batch_lock_rejects_a_second_sender(tmp_path):
    lock_path = tmp_path / "send.lock"
    with mailer.BatchLock(str(lock_path)):
        with pytest.raises(mailer.BatchAlreadyLocked):
            with mailer.BatchLock(str(lock_path)):
                pass


def test_closed_lifecycle_blocks_smtp_before_connect(tmp_path):
    sendable = tmp_path / "sendable.csv"
    config = tmp_path / "config.yaml"
    sendable.write_text(
        "email,template,subject,body,status,sendable,reason\n"
        "a@example.com,default,Hi,Body,,yes,\n",
        encoding="utf-8",
    )
    _seal_sendable(sendable)
    rows = pd.read_csv(sendable, dtype=object).fillna("")
    batch_id = str(rows.loc[0, "batch_id"])
    mailer._append_lifecycle(
        str(sendable), {"event": "outbound_opened", "batch_id": batch_id}
    )
    mailer._append_lifecycle(
        str(sendable), {"event": "outbound_closed", "batch_id": batch_id}
    )
    _write_config(config)
    original_connect = mailer.connect_smtp
    try:
        mailer.connect_smtp = lambda cfg: pytest.fail("SMTP must not be connected")
        with pytest.raises(mailer.LedgerIntegrityError, match="OUTBOUND_CLOSED"):
            mailer.cmd_send(_send_args(sendable, config, limit=1, sleep=1))
    finally:
        mailer.connect_smtp = original_connect


def test_lifecycle_tail_deletion_and_missing_ledger_are_fail_closed(tmp_path):
    sendable = tmp_path / "sendable.csv"
    sendable.write_text("placeholder", encoding="utf-8")
    mailer._append_lifecycle(
        str(sendable), {"event": "outbound_opened", "batch_id": "batch-1"}
    )
    mailer._append_lifecycle(
        str(sendable), {"event": "outbound_closed", "batch_id": "batch-1"}
    )
    lifecycle = Path(mailer._default_lifecycle_path(str(sendable)))
    lifecycle.write_text(
        lifecycle.read_text(encoding="utf-8").splitlines()[0] + "\n",
        encoding="utf-8",
    )
    with pytest.raises(mailer.LedgerIntegrityError, match="tail is missing"):
        mailer._read_lifecycle(str(sendable))

    second = tmp_path / "missing-lifecycle.csv"
    second.write_text("placeholder", encoding="utf-8")
    ledger = mailer._default_ledger_path(str(second))
    mailer._ensure_ledger_anchor(ledger, "batch-2")
    with pytest.raises(mailer.LedgerIntegrityError, match="lifecycle ledger is missing"):
        mailer._ensure_lifecycle_open(str(second), "batch-2")


def test_lifecycle_head_one_event_behind_recovers_after_power_loss(tmp_path):
    sendable = tmp_path / "sendable.csv"
    sendable.write_text("placeholder", encoding="utf-8")
    mailer._append_lifecycle(
        str(sendable), {"event": "outbound_opened", "batch_id": "batch-1"}
    )
    mailer._append_lifecycle(
        str(sendable), {"event": "outbound_closed", "batch_id": "batch-1"}
    )
    original_atomic = mailer._atomic_write_json_file

    def fail_final_head(path, payload):
        if path == mailer._lifecycle_head_path(str(sendable)) and payload.get("seq") == 3:
            raise OSError("simulated power loss before lifecycle head update")
        return original_atomic(path, payload)

    try:
        mailer._atomic_write_json_file = fail_final_head
        with pytest.raises(OSError, match="power loss"):
            mailer._append_lifecycle(
                str(sendable),
                {"event": "archived", "batch_id": "batch-1"},
            )
    finally:
        mailer._atomic_write_json_file = original_atomic

    lifecycle_lines = Path(
        mailer._default_lifecycle_path(str(sendable))
    ).read_text(encoding="utf-8").splitlines()
    stale_head = json.loads(
        Path(mailer._lifecycle_head_path(str(sendable))).read_text(encoding="utf-8")
    )
    assert len(lifecycle_lines) == 3
    assert stale_head["seq"] == 2

    recovered = mailer._read_lifecycle(str(sendable))
    recovered_head = json.loads(
        Path(mailer._lifecycle_head_path(str(sendable))).read_text(encoding="utf-8")
    )
    assert recovered[-1]["event"] == "archived"
    assert recovered_head == {
        "seq": 3,
        "event_hash": recovered[-1]["event_hash"],
    }


def test_bounce_reconciliation_shares_the_send_lock(tmp_path):
    sendable = tmp_path / "sendable.csv"
    sendable.write_text("email,status\na@example.com,sent\n", encoding="utf-8")
    args = Namespace(
        input=str(sendable),
        config=str(tmp_path / "missing-config.yaml"),
        ledger=None,
        lookback=10,
        apply=False,
    )

    with mailer.BatchLock(f"{sendable}.lock"):
        with pytest.raises(SystemExit, match="currently sending"):
            mailer.cmd_bounces(args)


def test_bounce_apply_never_marks_an_unproven_pending_row(tmp_path):
    sendable = tmp_path / "sendable.csv"
    sendable.write_text(
        "email,template,subject,body,status,sendable,reason\n"
        "a@example.com,default,Hi,Body,,yes,\n",
        encoding="utf-8",
    )
    _seal_sendable(sendable)
    args = Namespace(
        input=str(sendable),
        config="",
        config_data={
            "smtp": {
                "host": "smtp.example.com",
                "user": "sender@example.com",
                "password": "app-password",
            },
            "imap": {"host": "imap.example.com", "port": 993},
            "from_addr": "sender@example.com",
        },
        ledger=None,
        lookback=50,
        apply=True,
    )
    original_extract = mailer._extract_bounces
    try:
        mailer._extract_bounces = lambda cfg, lookback, since_time: (
            _fake_bounce_extraction(
                {
                    "a@example.com": {
                        "reasons": ["old campaign bounced"],
                        "message_ids": {"<old-message@example.com>"},
                    }
                }
            )
        )
        result = mailer.cmd_bounces(args)
    finally:
        mailer._extract_bounces = original_extract

    rows = pd.read_csv(sendable, dtype=object).fillna("")
    assert {key: result[key] for key in ("found", "matched", "unverified", "applied")} == {
        "found": 1,
        "matched": 0,
        "unverified": 1,
        "applied": 0,
    }
    assert rows.loc[0, "status"] == ""
    assert not Path(mailer._default_ledger_path(str(sendable))).exists()


def test_bounce_apply_requires_sent_ledger_and_matching_message_id(tmp_path):
    sendable = tmp_path / "sendable.csv"
    sendable.write_text(
        "email,template,subject,body,status,sendable,reason\n"
        "a@example.com,default,Hi,Body,,yes,\n",
        encoding="utf-8",
    )
    _seal_sendable(sendable)
    rows = pd.read_csv(sendable, dtype=object).fillna("")
    ledger = Path(mailer._default_ledger_path(str(sendable)))
    mailer._ensure_ledger_anchor(str(ledger), str(rows.loc[0, "batch_id"]))
    mailer._append_ledger(
        str(ledger),
        {
            "row": 0,
            "batch_id": str(rows.loc[0, "batch_id"]),
            "record_id": str(rows.loc[0, "record_id"]),
            "email": "a@example.com",
            "status": "sent",
            "send_time": "2026-07-13T12:00:00+08:00",
        },
    )
    expected_message_id = mailer._send_message_id(
        str(rows.loc[0, "record_id"]), "sender@example.com"
    )
    args = Namespace(
        input=str(sendable),
        config="",
        config_data={
            "smtp": {
                "host": "smtp.example.com",
                "user": "sender@example.com",
                "password": "app-password",
            },
            "imap": {"host": "imap.example.com", "port": 993},
            "from_addr": "sender@example.com",
        },
        ledger=None,
        lookback=50,
        apply=True,
    )
    original_extract = mailer._extract_bounces
    try:
        mailer._extract_bounces = lambda cfg, lookback, since_time: (
            _fake_bounce_extraction(
                {
                    "a@example.com": {
                        "reasons": ["550 rejected"],
                        "message_ids": {expected_message_id},
                    }
                }
            )
        )
        result = mailer.cmd_bounces(args)
    finally:
        mailer._extract_bounces = original_extract

    updated = pd.read_csv(sendable, dtype=object).fillna("")
    assert {key: result[key] for key in ("found", "matched", "unverified", "applied")} == {
        "found": 1,
        "matched": 1,
        "unverified": 0,
        "applied": 1,
    }
    assert updated.loc[0, "status"] == "bounced"
    assert updated.loc[0, "send_error"] == "550 rejected"


def test_1033_rows_interrupt_and_resume_without_automatic_duplicates(tmp_path):
    sendable = tmp_path / "sendable.csv"
    config = tmp_path / "config.yaml"
    rows = [
        f"user{i}@example.com,default,Hi,Body,,yes," for i in range(1033)
    ]
    sendable.write_text(
        "email,template,subject,body,status,sendable,reason\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    _seal_sendable(sendable)
    _write_config(config)

    class InterruptingSMTP:
        calls = 0
        accepted = []

        def sendmail(self, _from, recipients, _message):
            self.calls += 1
            if self.calls == 38:
                raise KeyboardInterrupt
            self.accepted.extend(recipients)
            return {}

        def quit(self):
            pass

    first = InterruptingSMTP()
    original_connect = mailer.connect_smtp
    original_sleep = mailer.time.sleep
    try:
        mailer.connect_smtp = lambda cfg: first
        mailer.time.sleep = lambda _seconds: None
        with pytest.raises(KeyboardInterrupt):
            mailer.cmd_send(_send_args(sendable, config, limit=100, sleep=1))
    finally:
        mailer.connect_smtp = original_connect
        mailer.time.sleep = original_sleep

    class ResumeSMTP:
        accepted = []

        def sendmail(self, _from, recipients, _message):
            self.accepted.extend(recipients)
            return {}

        def quit(self):
            pass

    second = ResumeSMTP()
    try:
        mailer.connect_smtp = lambda cfg: second
        mailer.time.sleep = lambda _seconds: None
        with pytest.raises(mailer.LedgerIntegrityError, match="unknown result"):
            mailer.cmd_send(_send_args(sendable, config, limit=100, sleep=1))
        assert second.accepted == []
        reviewed = pd.read_csv(sendable, dtype=object).fillna("")
        mailer._apply_ledger(
            reviewed, mailer._default_ledger_path(str(sendable))
        )
        mailer._append_ledger(
            mailer._default_ledger_path(str(sendable)),
            {
                "event": "manual_resolution",
                "row": 37,
                "batch_id": str(reviewed.loc[37, "batch_id"]),
                "record_id": str(reviewed.loc[37, "record_id"]),
                "email": "user37@example.com",
                "status": "suppressed",
                "send_error": "Operator could not prove acceptance; permanently not retried.",
            },
        )
        mailer._append_lifecycle(
            str(sendable),
            {
                "event": "unknown_resolved_suppressed",
                "batch_id": str(reviewed.loc[37, "batch_id"]),
                "row": 37,
                "record_id": str(reviewed.loc[37, "record_id"]),
                "evidence": "Operator could not prove acceptance; permanently not retried.",
            },
        )
        mailer.cmd_send(_send_args(sendable, config, limit=100, sleep=1))
    finally:
        mailer.connect_smtp = original_connect
        mailer.time.sleep = original_sleep

    assert len(first.accepted) == 37
    assert len(second.accepted) == 100
    assert not set(first.accepted).intersection(second.accepted)
    assert "user37@example.com" not in second.accepted
    assert second.accepted[0] == "user38@example.com"


def test_web_real_send_honors_limit(tmp_path):
    run_dir, sendable, token = _prepare_web_run(tmp_path)
    rows = pd.read_csv(sendable, dtype=object).fillna("")
    form = _web_send_form(run_dir, sendable, token)
    web_app._write_test_marker(run_dir, rows, ["default"], form)

    class FakeSMTP:
        calls = 0

        def sendmail(self, _from, _recipients, _message):
            self.calls += 1
            return {}

        def quit(self):
            pass

    fake = FakeSMTP()
    original_connect = web_app.mailer.connect_smtp
    original_sleep = web_app.mailer.time.sleep
    try:
        web_app.mailer.connect_smtp = lambda cfg: fake
        web_app.mailer.time.sleep = lambda _seconds: None
        web_app.app.config.update(TESTING=True)
        response = web_app.app.test_client().post("/send", data=form)
    finally:
        web_app.mailer.connect_smtp = original_connect
        web_app.mailer.time.sleep = original_sleep

    assert response.status_code == 200
    assert fake.calls == 1
    result = pd.read_csv(sendable, dtype=object).fillna("")
    assert list(result["status"]) == ["sent", "", ""]


def test_web_run_marker_blocks_when_both_authoritative_ledgers_disappear(tmp_path):
    run_dir, sendable, token = _prepare_web_run(tmp_path, row_count=1)
    rows = pd.read_csv(sendable, dtype=object).fillna("")
    form = _web_send_form(run_dir, sendable, token)
    web_app._write_test_marker(run_dir, rows, ["default"], form)

    class FakeSMTP:
        def __init__(self):
            self.accepted = []

        def sendmail(self, _from, recipients, _message):
            self.accepted.extend(recipients)
            return {}

        def quit(self):
            pass

    first = FakeSMTP()
    original_connect = web_app.mailer.connect_smtp
    original_sleep = web_app.mailer.time.sleep
    try:
        web_app.mailer.connect_smtp = lambda cfg: first
        web_app.mailer.time.sleep = lambda _seconds: None
        web_app.app.config.update(TESTING=True)
        sent = web_app.app.test_client().post("/send", data=form)
    finally:
        web_app.mailer.connect_smtp = original_connect
        web_app.mailer.time.sleep = original_sleep
    assert sent.status_code == 200
    assert first.accepted == ["user0@example.com"]
    assert (run_dir / "production.started").exists()

    stale = pd.read_csv(sendable, dtype=object).fillna("")
    stale.at[0, "status"] = ""
    stale.at[0, "send_time"] = ""
    stale.at[0, "send_error"] = ""
    web_app.mailer._atomic_write_csv(stale, str(sendable))
    production_ledger = web_app.mailer._default_ledger_path(str(sendable))
    for evidence in (
        production_ledger,
        web_app.mailer._ledger_anchor_path(production_ledger),
        web_app.mailer._ledger_integrity_path(production_ledger),
        web_app.mailer._default_lifecycle_path(str(sendable)),
        web_app.mailer._lifecycle_head_path(str(sendable)),
    ):
        Path(evidence).unlink()

    second = FakeSMTP()
    retry_form = _web_send_form(
        run_dir, sendable, web_app._new_action_token(run_dir)
    )
    try:
        web_app.mailer.connect_smtp = lambda cfg: second
        blocked = web_app.app.test_client().post("/send", data=retry_form)
    finally:
        web_app.mailer.connect_smtp = original_connect

    assert blocked.status_code == 200
    assert "两套审计台账均缺失".encode() in blocked.data
    assert second.accepted == []


def test_web_test_action_requires_redirect_and_never_uses_customer_address(tmp_path):
    run_dir, sendable, token = _prepare_web_run(tmp_path, row_count=1)
    web_app.app.config.update(TESTING=True)
    response = web_app.app.test_client().post(
        "/send",
        data=_web_send_form(
            run_dir,
            sendable,
            token,
            action="test",
            redirect_to="",
            confirm_real_send="",
            confirm_test_received="",
            confirm_phrase="",
        ),
    )

    assert response.status_code == 200
    assert "测试模式必须填写重定向邮箱".encode() in response.data
    assert not Path(f"{sendable}.sendlog.jsonl").exists()


def test_web_redirect_test_rejects_an_address_in_customer_list(tmp_path):
    run_dir, sendable, token = _prepare_web_run(tmp_path, row_count=1)
    web_app.app.config.update(TESTING=True)
    form = _web_send_form(
        run_dir,
        sendable,
        token,
        action="test",
        redirect_to="user0@example.com",
        user="user0@example.com",
        from_addr="user0@example.com",
        confirm_real_send="",
        confirm_test_received="",
        confirm_phrase="",
    )

    response = web_app.app.test_client().post("/send", data=form)

    assert response.status_code == 200
    assert "测试邮箱也出现在客户名单中".encode() in response.data
    assert not Path(f"{sendable}.testlog.jsonl").exists()
    assert not (run_dir / "redirect-test-passed.json").exists()


def test_web_redirect_test_covers_every_pending_template(tmp_path):
    run_dir, sendable, token = _prepare_web_run(tmp_path, row_count=2)
    rows = pd.read_csv(sendable, dtype=object).fillna("")
    rows.at[1, "template"] = "waitlist"
    rows.to_csv(sendable, index=False)
    _seal_sendable(sendable, engine=web_app.mailer)
    reviewed = pd.read_csv(sendable, dtype=object).fillna("")
    web_app._write_manifest(
        run_dir,
        content_hash=web_app._content_hash(reviewed),
        batch_fingerprint=web_app._batch_fingerprint(reviewed),
    )

    class FakeSMTP:
        calls = 0

        def sendmail(self, _from, recipients, _message):
            assert recipients == ["sender@example.com"]
            self.calls += 1
            return {}

        def quit(self):
            pass

    fake = FakeSMTP()
    original_connect = web_app.mailer.connect_smtp
    try:
        web_app.mailer.connect_smtp = lambda cfg: fake
        web_app.app.config.update(TESTING=True)
        response = web_app.app.test_client().post(
            "/send",
            data=_web_send_form(
                run_dir,
                sendable,
                    token,
                    action="test",
                    redirect_to="sender@example.com",
                confirm_real_send="",
                confirm_test_received="",
                confirm_phrase="",
            ),
        )
    finally:
        web_app.mailer.connect_smtp = original_connect

    assert response.status_code == 200
    assert fake.calls == 2
    assert (run_dir / "redirect-test-passed.json").exists()
    assert list(run_dir.glob("smtp-*.yaml")) == []
    result = pd.read_csv(sendable, dtype=object).fillna("")
    assert list(result["status"]) == ["", ""]


def test_web_real_send_requires_same_sender_identity_as_test(tmp_path):
    run_dir, sendable, token = _prepare_web_run(tmp_path, row_count=1)
    rows = pd.read_csv(sendable, dtype=object).fillna("")
    tested_form = _web_send_form(run_dir, sendable, token)
    web_app._write_test_marker(run_dir, rows, ["default"], tested_form)
    changed_form = dict(tested_form)
    changed_form["from_addr"] = "different-sender@example.com"
    web_app.app.config.update(TESTING=True)

    response = web_app.app.test_client().post("/send", data=changed_form)

    assert response.status_code == 200
    assert "必须先为当前预览成功完成重定向测试".encode() in response.data
    assert not Path(f"{sendable}.sendlog.jsonl").exists()


def test_web_real_send_requires_matching_redirect_test(tmp_path):
    run_dir, sendable, token = _prepare_web_run(tmp_path, row_count=1)
    web_app.app.config.update(TESTING=True)
    response = web_app.app.test_client().post(
        "/send", data=_web_send_form(run_dir, sendable, token)
    )

    assert response.status_code == 200
    assert "必须先为当前预览成功完成重定向测试".encode() in response.data
    assert not Path(f"{sendable}.sendlog.jsonl").exists()


def test_web_action_token_is_one_time(tmp_path):
    run_dir, sendable, token = _prepare_web_run(tmp_path, row_count=1)
    web_app.app.config.update(TESTING=True)
    client = web_app.app.test_client()
    form = _web_send_form(
        run_dir,
        sendable,
        token,
        action="dry",
        confirm_real_send="",
        confirm_test_received="",
        confirm_phrase="",
    )

    first = client.post("/send", data=form)
    second = client.post("/send", data=form)

    assert first.status_code == 200
    assert second.status_code == 200
    assert "该操作已使用".encode() in second.data


def test_web_reopens_existing_batch_with_original_ledger(tmp_path):
    run_dir, sendable, _token = _prepare_web_run(tmp_path, row_count=2)
    rows = pd.read_csv(sendable, dtype=object).fillna("")
    web_app._write_manifest(
        run_dir,
        source_hash="source-hash",
        content_hash=web_app._content_hash(rows),
    )
    (run_dir / "preview.txt").write_text("preview", encoding="utf-8")
    web_app.mailer._append_ledger(
        web_app.mailer._default_ledger_path(str(sendable)),
        {
            "row": 0,
            "batch_id": str(rows.loc[0, "batch_id"]),
            "record_id": str(rows.loc[0, "record_id"]),
            "email": "user0@example.com",
            "status": "sent",
            "send_time": "2026-07-13T12:00:00+08:00",
        },
    )
    web_app.app.config.update(TESTING=True)

    response = web_app.app.test_client().get(f"/runs/{run_dir.name}")

    assert response.status_code == 200
    assert "已安全打开原批次".encode() in response.data
    assert b'SMTP \xe5\xb7\xb2\xe6\x8e\xa5\xe5\x8f\x97</div><div class="value">1</div>' in response.data


def test_web_equivalent_reexport_reopens_original_batch(tmp_path):
    web_app.RUNS_DIR = tmp_path
    tmp_path.mkdir(exist_ok=True)
    web_app.app.config.update(TESTING=True)
    client = web_app.app.test_client()
    csv_bytes = b"name,email,group\nAda,ada@example.com,confirmed\n"
    form = {
        "activity_name": "Test campaign",
        "mode": "grouped",
        "template": "default",
        "email_col": "email",
        "name_col": "name",
        "group_col": "group",
        "sent_col": "",
        "confirm_no_history": "on",
    }

    first = client.post(
        "/preview",
        data={**form, "recipients": (io.BytesIO(csv_bytes), "people.csv")},
        content_type="multipart/form-data",
    )
    runs_after_first = list(tmp_path.glob("mailpilot_*"))
    reexported_bytes = csv_bytes.replace(b"\n", b"\r\n")
    second = client.post(
        "/preview",
        data={**form, "recipients": (io.BytesIO(reexported_bytes), "people-reexport.csv")},
        content_type="multipart/form-data",
    )

    assert first.status_code == 200
    assert len(runs_after_first) == 1
    assert second.status_code == 302
    assert second.headers["Location"].endswith(f"/runs/{runs_after_first[0].name}")
    assert list(tmp_path.glob("mailpilot_*")) == runs_after_first


def test_web_same_source_can_correct_historical_sent_mapping(tmp_path):
    web_app.RUNS_DIR = tmp_path
    web_app.app.config.update(TESTING=True)
    client = web_app.app.test_client()
    csv_bytes = b"name,email,group,sent\nAda,ada@example.com,confirmed,yes\n"
    common = {
        "activity_name": "Test campaign",
        "mode": "grouped",
        "template": "default",
        "email_col": "email",
        "name_col": "name",
        "group_col": "group",
    }

    first = client.post(
        "/preview",
        data={
            **common,
            "sent_col": "",
            "confirm_no_history": "on",
            "recipients": (io.BytesIO(csv_bytes), "people.csv"),
        },
        content_type="multipart/form-data",
    )
    second = client.post(
        "/preview",
        data={
            **common,
            "sent_col": "sent",
            "recipients": (io.BytesIO(csv_bytes), "people.csv"),
        },
        content_type="multipart/form-data",
    )

    assert first.status_code == 200
    assert second.status_code == 200
    runs = list(tmp_path.glob("mailpilot_*"))
    assert len(runs) == 1
    assert pd.read_csv(runs[0] / "sendable.csv", dtype=object).fillna("").loc[0, "status"] == "sent"


def test_web_started_source_cannot_create_a_second_mapping(tmp_path):
    web_app.RUNS_DIR = tmp_path
    web_app.app.config.update(TESTING=True)
    client = web_app.app.test_client()
    csv_bytes = (
        b"name,email,group_a,group_b\n"
        b"Ada,ada@example.com,confirmed,waitlist\n"
    )
    common = {
        "activity_name": "Test campaign",
        "mode": "grouped",
        "template": "default",
        "email_col": "email",
        "name_col": "name",
        "sent_col": "",
        "confirm_no_history": "on",
    }
    first = client.post(
        "/preview",
        data={
            **common,
            "group_col": "group_a",
            "recipients": (io.BytesIO(csv_bytes), "people.csv"),
        },
        content_type="multipart/form-data",
    )
    first_run = next(tmp_path.glob("mailpilot_*"))
    sendable = first_run / "sendable.csv"
    batch_id = str(pd.read_csv(sendable, dtype=object).fillna("").loc[0, "batch_id"])
    mailer._ensure_ledger_anchor(mailer._default_ledger_path(str(sendable)), batch_id)

    second = client.post(
        "/preview",
        data={
            **common,
            "group_col": "group_b",
            "recipients": (io.BytesIO(csv_bytes), "people.csv"),
        },
        content_type="multipart/form-data",
    )

    assert first.status_code == 200
    assert second.status_code == 302
    assert second.headers["Location"].endswith(f"/runs/{first_run.name}")
    assert list(tmp_path.glob("mailpilot_*")) == [first_run]


def test_web_reexported_recipient_set_cannot_bypass_started_batch(tmp_path):
    web_app.RUNS_DIR = tmp_path
    web_app.app.config.update(TESTING=True)
    client = web_app.app.test_client()
    first_csv = (
        b"name,email,group_a,group_b\n"
        b"Ada,ada@example.com,confirmed,waitlist\n"
        b"Bob,bob@example.com,confirmed,waitlist\n"
    )
    second_csv = (
        b"name,email,group_a,group_b\r\n"
        b"Bob,bob@example.com,confirmed,waitlist\r\n"
        b"Ada,ada@example.com,confirmed,waitlist\r\n"
    )
    common = {
        "activity_name": "Test campaign",
        "mode": "grouped",
        "template": "default",
        "email_col": "email",
        "name_col": "name",
        "sent_col": "",
        "confirm_no_history": "on",
    }
    client.post(
        "/preview",
        data={
            **common,
            "group_col": "group_a",
            "recipients": (io.BytesIO(first_csv), "people.csv"),
        },
        content_type="multipart/form-data",
    )
    first_run = next(tmp_path.glob("mailpilot_*"))
    sendable = first_run / "sendable.csv"
    batch_id = str(pd.read_csv(sendable, dtype=object).fillna("").loc[0, "batch_id"])
    mailer._ensure_ledger_anchor(mailer._default_ledger_path(str(sendable)), batch_id)

    response = client.post(
        "/preview",
        data={
            **common,
            "group_col": "group_b",
            "recipients": (io.BytesIO(second_csv), "people-reexport.csv"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/runs/{first_run.name}")
    assert list(tmp_path.glob("mailpilot_*")) == [first_run]


def test_web_added_recipient_cannot_bypass_started_batch(tmp_path):
    web_app.RUNS_DIR = tmp_path
    web_app.app.config.update(TESTING=True)
    client = web_app.app.test_client()
    first_csv = (
        b"name,email,group\n"
        b"Ada,ada@example.com,confirmed\n"
        b"Bob,bob@example.com,confirmed\n"
    )
    expanded_csv = first_csv + b"Cara,cara@example.com,confirmed\n"
    form = {
        "activity_name": "Test campaign",
        "mode": "grouped",
        "template": "default",
        "email_col": "email",
        "name_col": "name",
        "group_col": "group",
        "sent_col": "",
        "confirm_no_history": "on",
    }
    client.post(
        "/preview",
        data={**form, "recipients": (io.BytesIO(first_csv), "people.csv")},
        content_type="multipart/form-data",
    )
    first_run = next(tmp_path.glob("mailpilot_*"))
    sendable = first_run / "sendable.csv"
    batch_id = str(pd.read_csv(sendable, dtype=object).fillna("").loc[0, "batch_id"])
    mailer._ensure_ledger_anchor(mailer._default_ledger_path(str(sendable)), batch_id)

    response = client.post(
        "/preview",
        data={
            **form,
            "recipients": (io.BytesIO(expanded_csv), "people-expanded.csv"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/runs/{first_run.name}")
    assert list(tmp_path.glob("mailpilot_*")) == [first_run]


def test_web_preview_replacement_cannot_race_production_start(tmp_path):
    web_app.RUNS_DIR = tmp_path
    web_app.app.config.update(TESTING=True)
    csv_bytes = b"name,email,group,sent\nAda,ada@example.com,confirmed,yes\n"
    base_form = {
        "activity_name": "Test campaign",
        "mode": "grouped",
        "template": "default",
        "email_col": "email",
        "name_col": "name",
        "group_col": "group",
        "sent_col": "",
        "confirm_no_history": "on",
    }
    first = web_app.app.test_client().post(
        "/preview",
        data={
            **base_form,
            "recipients": (io.BytesIO(csv_bytes), "people.csv"),
        },
        content_type="multipart/form-data",
    )
    assert first.status_code == 200
    run_dir = next(tmp_path.glob("mailpilot_*"))
    sendable = run_dir / "sendable.csv"
    token = web_app._new_action_token(run_dir)
    send_form = _web_send_form(run_dir, sendable, token)
    reviewed = pd.read_csv(sendable, dtype=object).fillna("")
    web_app._write_test_marker(run_dir, reviewed, ["confirmed"], send_form)

    marker_written = threading.Event()
    release_marker = threading.Event()
    original_write_manifest = web_app._write_manifest
    original_connect = web_app.mailer.connect_smtp
    original_sleep = web_app.mailer.time.sleep

    def controlled_manifest(target, **values):
        original_write_manifest(target, **values)
        if values.get("production_started") is True:
            marker_written.set()
            assert release_marker.wait(timeout=5)

    class FakeSMTP:
        calls = 0

        def sendmail(self, _from, _recipients, _message):
            self.calls += 1
            return {}

        def quit(self):
            pass

    fake = FakeSMTP()
    results = {}
    web_app._write_manifest = controlled_manifest
    web_app.mailer.connect_smtp = lambda cfg: fake
    web_app.mailer.time.sleep = lambda _seconds: None
    try:
        production = threading.Thread(
            target=lambda: results.setdefault(
                "production", web_app.app.test_client().post("/send", data=send_form)
            )
        )
        production.start()
        assert marker_written.wait(timeout=5)

        correction = threading.Thread(
            target=lambda: results.setdefault(
                "correction",
                web_app.app.test_client().post(
                    "/preview",
                    data={
                        **base_form,
                        "sent_col": "sent",
                        "confirm_no_history": "",
                        "recipients": (io.BytesIO(csv_bytes), "people.csv"),
                    },
                    content_type="multipart/form-data",
                ),
            )
        )
        correction.start()
        release_marker.set()
        production.join(timeout=10)
        correction.join(timeout=10)
    finally:
        release_marker.set()
        web_app._write_manifest = original_write_manifest
        web_app.mailer.connect_smtp = original_connect
        web_app.mailer.time.sleep = original_sleep

    assert not production.is_alive()
    assert not correction.is_alive()
    assert results["production"].status_code == 200
    assert results["correction"].status_code == 302
    assert fake.calls == 1
    assert list(tmp_path.glob("mailpilot_*")) == [run_dir]


def test_web_failed_preview_deletes_uploaded_recipient_copy(tmp_path):
    web_app.RUNS_DIR = tmp_path
    web_app.app.config.update(TESTING=True)
    response = web_app.app.test_client().post(
        "/preview",
        data={
            "activity_name": "Test campaign",
            "mode": "simple",
            "template": "default",
            "email_col": "missing_email_column",
            "name_col": "name",
            "sent_col": "",
            "confirm_no_history": "on",
            "recipients": (
                io.BytesIO(b"name,email\nAda,ada@example.com\n"),
                "people.csv",
            ),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert "名单副本已自动删除".encode() in response.data
    assert list(tmp_path.glob("mailpilot_*")) == []


def test_web_unknown_requires_evidence_before_batch_can_continue(tmp_path):
    run_dir, sendable, token = _prepare_web_run(tmp_path, row_count=2)
    rows = pd.read_csv(sendable, dtype=object).fillna("")
    ledger = web_app.mailer._default_ledger_path(str(sendable))
    web_app.mailer._ensure_lifecycle_open(
        str(sendable), str(rows.loc[0, "batch_id"])
    )
    web_app.mailer._ensure_ledger_anchor(ledger, str(rows.loc[0, "batch_id"]))
    web_app.mailer._append_ledger(
        ledger,
        {
            "event": "send_attempt",
            "row": 0,
            "batch_id": str(rows.loc[0, "batch_id"]),
            "record_id": str(rows.loc[0, "record_id"]),
            "email": "user0@example.com",
            "status": "attempting",
        },
    )
    web_app._ensure_production_marker(
        run_dir,
        rows,
        {
            "host": "smtp.example.com",
            "port": 465,
            "use_ssl": True,
            "user": "sender@example.com",
            "from_addr": "sender@example.com",
            "from_name": "",
            "unsubscribe": "",
        },
    )
    web_app.app.config.update(TESTING=True)

    attention_page = web_app.app.test_client().get(f"/runs/{run_dir.name}")
    assert "永久不重发 user0@example.com".encode() in attention_page.data
    assert "确认已接受 user0@example.com".encode() in attention_page.data
    token = web_app._new_action_token(run_dir)

    response = web_app.app.test_client().post(
        "/lifecycle",
        data={
            "sendable_path": str(sendable),
            "run_dir": str(run_dir),
            "action_token": token,
            "lifecycle_action": "unknown_suppress",
            "resolve_email": "user0@example.com",
            "evidence": "服务商无法确认，因此永久停止重发",
            "confirm_phrase": "永久不重发 user0@example.com",
        },
    )

    assert response.status_code == 302
    recovered = pd.read_csv(sendable, dtype=object).fillna("")
    web_app.mailer._apply_ledger(recovered, ledger)
    assert recovered.loc[0, "status"] == "suppressed"
    assert web_app.mailer._read_lifecycle(str(sendable))[-1]["event"] == "unknown_suppress"


def test_web_close_bounce_apply_and_archive_lifecycle(tmp_path):
    run_dir, sendable, token = _prepare_web_run(tmp_path, row_count=1)
    rows = pd.read_csv(sendable, dtype=object).fillna("")
    batch_id = str(rows.loc[0, "batch_id"])
    record_id = str(rows.loc[0, "record_id"])
    ledger = web_app.mailer._default_ledger_path(str(sendable))
    web_app.mailer._ensure_lifecycle_open(str(sendable), batch_id)
    web_app.mailer._ensure_ledger_anchor(ledger, batch_id)
    web_app.mailer._append_ledger(
        ledger,
        {
            "event": "send_result",
            "row": 0,
            "batch_id": batch_id,
            "record_id": record_id,
            "email": "user0@example.com",
            "status": "sent",
            "send_time": "2026-07-13T12:00:00+08:00",
        },
    )
    web_app._ensure_production_marker(
        run_dir,
        rows,
        {
            "host": "smtp.example.com",
            "port": 465,
            "use_ssl": True,
            "user": "sender@example.com",
            "from_addr": "sender@example.com",
            "from_name": "",
            "unsubscribe": "",
        },
    )
    web_app._write_manifest(run_dir, production_started=True)
    web_app.app.config.update(TESTING=True)
    client = web_app.app.test_client()

    closed = client.post(
        "/lifecycle",
        data={
            "sendable_path": str(sendable),
            "run_dir": str(run_dir),
            "action_token": token,
            "lifecycle_action": "close",
            "confirm_close": "on",
            "confirm_phrase": f"关闭外发 {batch_id[:8]}",
        },
    )
    assert closed.status_code == 302
    assert web_app.mailer._lifecycle_phase(str(sendable)) == "OUTBOUND_CLOSED"

    base_scan_form = {
        "sendable_path": str(sendable),
        "run_dir": str(run_dir),
        "bounce_action": "scan",
        "imap_host": "imap.example.com",
        "imap_port": "993",
        "imap_password": "app-password",
        "lookback": "200",
    }
    wrong_from = client.post(
        "/bounces",
        data={
            **base_scan_form,
            "action_token": web_app._new_action_token(run_dir),
            "imap_user": "sender@example.com",
            "from_addr": "wrong@example.net",
        },
    )
    wrong_imap = client.post(
        "/bounces",
        data={
            **base_scan_form,
            "action_token": web_app._new_action_token(run_dir),
            "imap_user": "unrelated@example.net",
            "from_addr": "sender@example.com",
        },
    )
    assert wrong_from.status_code == 200
    assert "必须与正式发送一致".encode() in wrong_from.data
    assert wrong_imap.status_code == 200
    assert "仅允许使用正式 SMTP 账号".encode() in wrong_imap.data
    assert not (run_dir / "bounce-reconciliation.json").exists()

    expected_message_id = web_app.mailer._send_message_id(
        record_id, "sender@example.com"
    )
    original_extract = web_app.mailer._extract_bounces
    try:
        web_app.mailer._extract_bounces = lambda cfg, lookback, since_time: (
            _fake_bounce_extraction(
                {
                    "user0@example.com": {
                        "reasons": ["550 rejected"],
                        "message_ids": {expected_message_id},
                    }
                }
            )
        )
        scan_token = web_app._new_action_token(run_dir)
        scanned = client.post(
            "/bounces",
            data={
                "sendable_path": str(sendable),
                "run_dir": str(run_dir),
                "action_token": scan_token,
                "bounce_action": "scan",
                "imap_host": "imap.example.com",
                "imap_port": "993",
                "imap_user": "sender@example.com",
                "imap_password": "app-password",
                "from_addr": "sender@example.com",
                "lookback": "200",
            },
        )
    finally:
        web_app.mailer._extract_bounces = original_extract
    assert scanned.status_code == 302
    report = json.loads(
        (run_dir / "bounce-reconciliation.json").read_text(encoding="utf-8")
    )
    assert report["matched"] == 1
    assert report["unapplied"] == 1
    bounce_page = client.get(f"/runs/{run_dir.name}")
    assert b"user0@example.com" in bounce_page.data
    assert b"550 rejected" in bounce_page.data
    assert client.get(f"/runs/{run_dir.name}/bounces.json").status_code == 200

    apply_token = web_app._new_action_token(run_dir)
    applied = client.post(
        "/bounces",
        data={
            "sendable_path": str(sendable),
            "run_dir": str(run_dir),
            "action_token": apply_token,
            "bounce_action": "apply",
            "confirm_phrase": "应用 1 条退信",
        },
    )
    assert applied.status_code == 302
    updated = pd.read_csv(sendable, dtype=object).fillna("")
    assert updated.loc[0, "status"] == "bounced"

    report_path = run_dir / "bounce-reconciliation.json"
    complete_report = json.loads(report_path.read_text(encoding="utf-8"))
    incomplete_report = dict(complete_report)
    incomplete_report["coverage"] = {
        **complete_report["coverage"],
        "complete": False,
        "scanned_messages": 1,
        "matching_messages": 12,
    }
    web_app._atomic_write_json(report_path, incomplete_report)
    incomplete_archive = client.post(
        "/archive",
        data={
            "sendable_path": str(sendable),
            "run_dir": str(run_dir),
            "action_token": web_app._new_action_token(run_dir),
            "confirm_waited_bounce_window": "on",
            "confirm_delivery_limits": "on",
            "confirm_phrase": f"归档 {batch_id[:8]}",
        },
    )
    assert incomplete_archive.status_code == 200
    assert "没有完整覆盖" in incomplete_archive.data.decode("utf-8")
    assert web_app.mailer._lifecycle_phase(str(sendable)) == "OUTBOUND_CLOSED"
    web_app._atomic_write_json(report_path, complete_report)

    archive_form = {
        "sendable_path": str(sendable),
        "run_dir": str(run_dir),
        "confirm_waited_bounce_window": "on",
        "confirm_delivery_limits": "on",
        "confirm_phrase": f"归档 {batch_id[:8]}",
    }
    original_append_lifecycle = web_app.mailer._append_lifecycle

    def interrupt_archive(input_path, event):
        if event.get("event") == "archived":
            raise OSError("simulated power loss after receipt")
        return original_append_lifecycle(input_path, event)

    try:
        web_app.mailer._append_lifecycle = interrupt_archive
        interrupted = client.post(
            "/archive",
            data={
                **archive_form,
                "action_token": web_app._new_action_token(run_dir),
            },
        )
    finally:
        web_app.mailer._append_lifecycle = original_append_lifecycle
    assert interrupted.status_code == 200
    assert web_app.mailer._lifecycle_phase(str(sendable)) == "OUTBOUND_CLOSED"
    assert (run_dir / "archive-receipt.json").exists()
    assert not web_app._archive_receipt(run_dir)

    archived = client.post(
        "/archive",
        data={
            **archive_form,
            "action_token": web_app._new_action_token(run_dir),
        },
    )
    assert archived.status_code == 302
    assert web_app.mailer._lifecycle_phase(str(sendable)) == "ARCHIVED"
    assert web_app._archive_receipt(run_dir)
    manifest_path = run_dir / "manifest.json"
    sealed_manifest = manifest_path.read_text(encoding="utf-8")
    tampered_manifest = json.loads(sealed_manifest)
    tampered_manifest["tampered"] = True
    manifest_path.write_text(json.dumps(tampered_manifest), encoding="utf-8")
    assert not web_app._archive_receipt(run_dir)
    manifest_path.write_text(sealed_manifest, encoding="utf-8")
    assert web_app._archive_receipt(run_dir)

    audit = client.get(f"/runs/{run_dir.name}/audit.json")
    status_csv = client.get(f"/runs/{run_dir.name}/status.csv")
    assert audit.status_code == 200
    assert json.loads(audit.data)["archived"] is True
    assert status_csv.status_code == 200
    assert b"bounced" in status_csv.data

    same_recipient = b"name,email\nAda,user0@example.com\n"
    default_preview = client.post(
        "/preview",
        data={
            "activity_name": "Second campaign",
            "mode": "simple",
            "template": "default",
            "email_col": "email",
            "name_col": "name",
            "sent_col": "",
            "confirm_no_history": "on",
            "recipients": (io.BytesIO(same_recipient), "second.csv"),
        },
        content_type="multipart/form-data",
    )
    assert default_preview.status_code == 302
    assert default_preview.headers["Location"].endswith(f"/runs/{run_dir.name}")

    new_activity_token = web_app._new_action_token(run_dir)
    authorized = client.post(
        "/new-activity",
        data={
            "sendable_path": str(sendable),
            "run_dir": str(run_dir),
            "action_token": new_activity_token,
            "activity_name": "Second campaign",
            "confirm_phrase": "新活动 Second campaign",
        },
    )
    assert authorized.status_code == 200
    authorization_file = next(tmp_path.glob(".new-activity-*.json"))
    authorization = json.loads(authorization_file.read_text(encoding="utf-8"))

    new_preview = client.post(
        "/preview",
        data={
            "activity_token": authorization["token"],
            "activity_from": run_dir.name,
            "activity_name": "Second campaign",
            "mode": "simple",
            "template": "default",
            "email_col": "email",
            "name_col": "name",
            "sent_col": "",
            "confirm_no_history": "on",
            "recipients": (io.BytesIO(same_recipient), "second.csv"),
        },
        content_type="multipart/form-data",
    )
    assert new_preview.status_code == 200
    runs = list(tmp_path.glob("mailpilot_*"))
    assert len(runs) == 2
    new_run = next(run for run in runs if run != run_dir)
    new_rows = pd.read_csv(new_run / "sendable.csv", dtype=object).fillna("")
    assert new_rows.loc[0, "activity_id"]
    assert new_rows.loc[0, "sendable"] == "no"
    assert "archived MailPilot activity" in new_rows.loc[0, "reason"]
    assert not authorization_file.exists()


def test_new_activity_suppression_unions_every_overlapping_archive(tmp_path):
    archived_runs = []
    for number, status in (
        (0, "bounced"),
        (1, "unsubscribed"),
        (2, "soft_bounced"),
    ):
        run = tmp_path / f"mailpilot_archive_{number}"
        run.mkdir()
        sendable = run / "sendable.csv"
        sendable.write_text(
            "email,template,subject,body,status,sendable,reason\n"
            f"user{number}@example.com,default,Hi,Body,{status},yes,\n",
            encoding="utf-8",
        )
        _seal_sendable(sendable)
        archived_runs.append(run)

    incoming = pd.DataFrame(
        [
            {"email": "user0@example.com", "sendable": "yes", "reason": ""},
            {"email": "user1@example.com", "sendable": "yes", "reason": ""},
            {"email": "user2@example.com", "sendable": "yes", "reason": ""},
            {"email": "safe@example.com", "sendable": "yes", "reason": ""},
        ]
    )
    original_receipt = web_app._archive_receipt
    try:
        web_app._archive_receipt = lambda run: {"valid": True}
        suppressed, count = web_app._apply_archived_suppression(
            incoming, archived_runs
        )
    finally:
        web_app._archive_receipt = original_receipt

    assert count == 2
    assert list(suppressed["sendable"]) == ["no", "no", "yes", "yes"]


def test_main_binds_local_server_before_opening_browser():
    events = []

    class FakeServer:
        def serve_forever(self):
            events.append("serve")

        def server_close(self):
            events.append("close")

    class ImmediateTimer:
        daemon = False

        def __init__(self, _delay, callback):
            self.callback = callback

        def start(self):
            self.callback()

    original_make_server = web_app.make_server
    original_timer = web_app.threading.Timer
    original_open = web_app.webbrowser.open
    try:
        web_app.make_server = lambda host, port, flask_app: (
            events.append("bind") or FakeServer()
        )
        web_app.threading.Timer = ImmediateTimer
        web_app.webbrowser.open = lambda url: events.append("browser")
        assert web_app.main() == 0
    finally:
        web_app.make_server = original_make_server
        web_app.threading.Timer = original_timer
        web_app.webbrowser.open = original_open

    assert events == ["bind", "browser", "serve", "close"]


def test_offline_web_end_to_end_preview_test_interrupt_resume_bounce_archive(tmp_path):
    web_app.RUNS_DIR = tmp_path
    web_app.app.config.update(TESTING=True)
    client = web_app.app.test_client()
    csv_bytes = (
        b"name,email\n"
        b"A,user0@example.com\n"
        b"B,user1@example.com\n"
        b"C,user2@example.com\n"
        b"D,user3@example.com\n"
    )
    preview = client.post(
        "/preview",
        data={
            "activity_name": "Offline acceptance",
            "mode": "simple",
            "template": "default",
            "email_col": "email",
            "name_col": "name",
            "sent_col": "",
            "confirm_no_history": "on",
            "recipients": (io.BytesIO(csv_bytes), "acceptance.csv"),
        },
        content_type="multipart/form-data",
    )
    assert preview.status_code == 200
    run_dir = next(tmp_path.glob("mailpilot_*"))
    sendable = run_dir / "sendable.csv"

    class RecordingSMTP:
        def __init__(self, failure=None):
            self.failure = failure
            self.accepted = []

        def sendmail(self, _from, recipients, _message):
            if self.failure:
                raise self.failure
            self.accepted.extend(recipients)
            return {}

        def quit(self):
            pass

    original_connect = web_app.mailer.connect_smtp
    original_sleep = web_app.mailer.time.sleep
    test_smtp = RecordingSMTP()
    try:
        web_app.mailer.connect_smtp = lambda cfg: test_smtp
        test_token = web_app._new_action_token(run_dir)
        test_response = client.post(
            "/send",
            data=_web_send_form(
                run_dir,
                sendable,
                test_token,
                action="test",
                redirect_to="sender@example.com",
                confirm_real_send="",
                confirm_test_received="",
                confirm_phrase="",
            ),
        )
    finally:
        web_app.mailer.connect_smtp = original_connect
    assert test_response.status_code == 200
    assert test_smtp.accepted == ["sender@example.com"]

    first_smtp = RecordingSMTP()
    try:
        web_app.mailer.connect_smtp = lambda cfg: first_smtp
        web_app.mailer.time.sleep = lambda _seconds: None
        first_token = web_app._new_action_token(run_dir)
        first_send = client.post(
            "/send",
            data=_web_send_form(run_dir, sendable, first_token, limit="1"),
        )
    finally:
        web_app.mailer.connect_smtp = original_connect
        web_app.mailer.time.sleep = original_sleep
    assert first_send.status_code == 200
    assert first_smtp.accepted == ["user0@example.com"]

    uncertain_smtp = RecordingSMTP(TimeoutError("simulated timeout"))
    try:
        web_app.mailer.connect_smtp = lambda cfg: uncertain_smtp
        web_app.mailer.time.sleep = lambda _seconds: None
        uncertain_token = web_app._new_action_token(run_dir)
        uncertain_send = client.post(
            "/send",
            data=_web_send_form(run_dir, sendable, uncertain_token, limit="1"),
        )
    finally:
        web_app.mailer.connect_smtp = original_connect
        web_app.mailer.time.sleep = original_sleep
    assert uncertain_send.status_code == 200
    uncertain_rows = pd.read_csv(sendable, dtype=object).fillna("")
    assert uncertain_rows.loc[1, "status"] == "unknown"

    resolve_token = web_app._new_action_token(run_dir)
    resolved = client.post(
        "/lifecycle",
        data={
            "sendable_path": str(sendable),
            "run_dir": str(run_dir),
            "action_token": resolve_token,
            "lifecycle_action": "unknown_suppress",
            "resolve_email": "user1@example.com",
            "evidence": "模拟服务商无法确认，验收选择永久不重发",
            "confirm_phrase": "永久不重发 user1@example.com",
        },
    )
    assert resolved.status_code == 302

    resume_smtp = RecordingSMTP()
    try:
        web_app.mailer.connect_smtp = lambda cfg: resume_smtp
        web_app.mailer.time.sleep = lambda _seconds: None
        resume_token = web_app._new_action_token(run_dir)
        resumed = client.post(
            "/send",
            data=_web_send_form(
                run_dir,
                sendable,
                resume_token,
                limit="2",
                confirm_phrase="发送 2 封",
            ),
        )
    finally:
        web_app.mailer.connect_smtp = original_connect
        web_app.mailer.time.sleep = original_sleep
    assert resumed.status_code == 200
    assert resume_smtp.accepted == ["user2@example.com", "user3@example.com"]
    assert not set(first_smtp.accepted).intersection(resume_smtp.accepted)

    finished_rows = pd.read_csv(sendable, dtype=object).fillna("")
    batch_id = str(finished_rows.loc[0, "batch_id"])
    close_token = web_app._new_action_token(run_dir)
    closed = client.post(
        "/lifecycle",
        data={
            "sendable_path": str(sendable),
            "run_dir": str(run_dir),
            "action_token": close_token,
            "lifecycle_action": "close",
            "confirm_close": "on",
            "confirm_phrase": f"关闭外发 {batch_id[:8]}",
        },
    )
    assert closed.status_code == 302

    record_id = str(finished_rows.loc[0, "record_id"])
    activity_id = str(finished_rows.loc[0, "activity_id"])
    expected_message_id = web_app.mailer._send_message_id(
        record_id, "sender@example.com", activity_id
    )
    original_extract = web_app.mailer._extract_bounces
    try:
        web_app.mailer._extract_bounces = lambda cfg, lookback, since_time: (
            _fake_bounce_extraction(
                {
                    "user0@example.com": {
                        "reasons": ["550 simulated bounce"],
                        "message_ids": {expected_message_id},
                    }
                }
            )
        )
        scan_token = web_app._new_action_token(run_dir)
        scan = client.post(
            "/bounces",
            data={
                "sendable_path": str(sendable),
                "run_dir": str(run_dir),
                "action_token": scan_token,
                "bounce_action": "scan",
                "imap_host": "imap.example.com",
                "imap_port": "993",
                "imap_user": "sender@example.com",
                "imap_password": "fake-app-password",
                "from_addr": "sender@example.com",
                "lookback": "200",
            },
        )
    finally:
        web_app.mailer._extract_bounces = original_extract
    assert scan.status_code == 302

    apply_token = web_app._new_action_token(run_dir)
    applied = client.post(
        "/bounces",
        data={
            "sendable_path": str(sendable),
            "run_dir": str(run_dir),
            "action_token": apply_token,
            "bounce_action": "apply",
            "confirm_phrase": "应用 1 条退信",
        },
    )
    assert applied.status_code == 302

    archive_token = web_app._new_action_token(run_dir)
    archived = client.post(
        "/archive",
        data={
            "sendable_path": str(sendable),
            "run_dir": str(run_dir),
            "action_token": archive_token,
            "confirm_waited_bounce_window": "on",
            "confirm_delivery_limits": "on",
            "confirm_phrase": f"归档 {batch_id[:8]}",
        },
    )
    assert archived.status_code == 302
    final_rows = pd.read_csv(sendable, dtype=object).fillna("")
    assert list(final_rows["status"]) == ["bounced", "suppressed", "sent", "sent"]
    assert web_app._archive_receipt(run_dir)


def test_web_damaged_matching_source_blocks_new_batch(tmp_path):
    web_app.RUNS_DIR = tmp_path
    broken = tmp_path / "mailpilot_broken"
    broken.mkdir()
    csv_bytes = b"name,email,group\nAda,ada@example.com,confirmed\n"
    source = broken / "recipients.csv"
    source.write_bytes(csv_bytes)
    web_app._write_manifest(
        broken,
        source_hash=web_app._sha256_path(source),
        batch_fingerprint="old-fingerprint",
    )
    web_app.app.config.update(TESTING=True)

    response = web_app.app.test_client().post(
        "/preview",
        data={
            "activity_name": "Test campaign",
            "mode": "grouped",
            "template": "default",
            "email_col": "email",
            "name_col": "name",
            "group_col": "group",
            "sent_col": "",
            "confirm_no_history": "on",
            "recipients": (io.BytesIO(csv_bytes), "people.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "sendable.csv 缺失".encode() in response.data
    assert list(tmp_path.glob("mailpilot_*")) == [broken]


def test_smtp_connection_rotation_preserves_every_checkpoint(tmp_path):
    sendable = tmp_path / "sendable.csv"
    config = tmp_path / "config.yaml"
    sendable.write_text(
        "email,template,subject,body,status,sendable\n"
        "a@example.com,default,Hi,Body,,yes\n"
        "b@example.com,default,Hi,Body,,yes\n"
        "c@example.com,default,Hi,Body,,yes\n",
        encoding="utf-8",
    )
    _seal_sendable(sendable)
    config.write_text(
        "smtp:\n"
        "  host: smtp.example.com\n"
        "  port: 465\n"
        "  use_ssl: true\n"
        "  user: sender@example.com\n"
        "  password: app-password\n"
        "  max_messages_per_connection: 2\n"
        "from_addr: sender@example.com\n",
        encoding="utf-8",
    )

    class FakeSMTP:
        def __init__(self):
            self.messages = []
            self.quit_called = False

        def sendmail(self, _from, _recipients, message):
            self.messages.append(message)
            return {}

        def quit(self):
            self.quit_called = True

    servers = []

    def connect(_cfg):
        server = FakeSMTP()
        servers.append(server)
        return server

    original_connect = mailer.connect_smtp
    original_sleep = mailer.time.sleep
    try:
        mailer.connect_smtp = connect
        mailer.time.sleep = lambda _seconds: None
        mailer.cmd_send(_send_args(sendable, config, limit=3, sleep=1))
    finally:
        mailer.connect_smtp = original_connect
        mailer.time.sleep = original_sleep

    assert [len(server.messages) for server in servers] == [2, 1]
    assert all(server.quit_called for server in servers)
    events = list(mailer._iter_ledger(mailer._default_ledger_path(str(sendable))))
    assert sum(event.get("status") == "attempting" for event in events) == 3
    assert sum(event.get("status") == "sent" for event in events) == 3
    assert list(pd.read_csv(sendable)["status"]) == ["sent", "sent", "sent"]


def test_smtp_reconnect_failure_never_creates_a_phantom_attempt(tmp_path):
    sendable = tmp_path / "sendable.csv"
    config = tmp_path / "config.yaml"
    sendable.write_text(
        "email,template,subject,body,status,sendable\n"
        "a@example.com,default,Hi,Body,,yes\n"
        "b@example.com,default,Hi,Body,,yes\n",
        encoding="utf-8",
    )
    _seal_sendable(sendable)
    config.write_text(
        "smtp:\n"
        "  host: smtp.example.com\n"
        "  user: sender@example.com\n"
        "  password: app-password\n"
        "  max_messages_per_connection: 1\n"
        "from_addr: sender@example.com\n",
        encoding="utf-8",
    )

    class FirstSMTP:
        def sendmail(self, _from, _recipients, _message):
            return {}

        def quit(self):
            pass

    calls = 0

    def connect(_cfg):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated reconnect failure")
        return FirstSMTP()

    original_connect = mailer.connect_smtp
    original_sleep = mailer.time.sleep
    try:
        mailer.connect_smtp = connect
        mailer.time.sleep = lambda _seconds: None
        with pytest.raises(OSError, match="reconnect failure"):
            mailer.cmd_send(_send_args(sendable, config, limit=2, sleep=1))
    finally:
        mailer.connect_smtp = original_connect
        mailer.time.sleep = original_sleep

    rows = pd.read_csv(sendable, dtype=object).fillna("")
    assert list(rows["status"]) == ["sent", ""]
    events = list(mailer._iter_ledger(mailer._default_ledger_path(str(sendable))))
    assert [event.get("row") for event in events if event.get("status") == "attempting"] == [0]


@pytest.mark.parametrize(
    ("error", "expected_calls"),
    [
        (smtplib.SMTPDataError(421, b"temporary rate limit"), 1),
        (smtplib.SMTPSenderRefused(550, b"sender rejected", "sender@example.com"), 1),
        (smtplib.SMTPDataError(550, b"same content rejected"), 3),
    ],
)
def test_smtp_circuit_breakers_stop_likely_batch_wide_failures(
    tmp_path, error, expected_calls
):
    sendable = tmp_path / "sendable.csv"
    config = tmp_path / "config.yaml"
    sendable.write_text(
        "email,template,subject,body,status,sendable\n"
        + "".join(
            f"user{i}@example.com,default,Hi,Body,,yes\n" for i in range(4)
        ),
        encoding="utf-8",
    )
    _seal_sendable(sendable)
    _write_config(config)

    class RejectingSMTP:
        calls = 0

        def sendmail(self, _from, _recipients, _message):
            self.calls += 1
            raise error

        def quit(self):
            pass

    server = RejectingSMTP()
    original_connect = mailer.connect_smtp
    original_sleep = mailer.time.sleep
    try:
        mailer.connect_smtp = lambda _cfg: server
        mailer.time.sleep = lambda _seconds: None
        mailer.cmd_send(_send_args(sendable, config, limit=4, sleep=1))
    finally:
        mailer.connect_smtp = original_connect
        mailer.time.sleep = original_sleep

    assert server.calls == expected_calls
    rows = pd.read_csv(sendable, dtype=object).fillna("")
    assert list(rows["status"]) == ["error"] * expected_calls + [""] * (
        4 - expected_calls
    )


def test_redirect_message_ids_are_unique_and_never_equal_production_id(tmp_path):
    sendable = tmp_path / "sendable.csv"
    config = tmp_path / "config.yaml"
    sendable.write_text(
        "email,template,subject,body,status,sendable\n"
        "a@example.com,default,Hi,Body,,yes\n",
        encoding="utf-8",
    )
    _seal_sendable(sendable)
    _write_config(config)

    class RecordingSMTP:
        messages = []

        def sendmail(self, _from, _recipients, message):
            self.messages.append(message)
            return {}

        def quit(self):
            pass

    server = RecordingSMTP()
    original_connect = mailer.connect_smtp
    try:
        mailer.connect_smtp = lambda _cfg: server
        for _ in range(2):
            mailer.cmd_send(
                _send_args(
                    sendable,
                    config,
                    redirect_to="sender@example.com",
                    limit=1,
                    sleep=0,
                )
            )
    finally:
        mailer.connect_smtp = original_connect

    rows = pd.read_csv(sendable, dtype=object).fillna("")
    production_id = mailer._send_message_id(
        str(rows.loc[0, "record_id"]), "sender@example.com"
    )
    observed_ids = []
    for message in server.messages:
        assert "\nDate:" in message
        message_id_line = next(
            line for line in message.splitlines() if line.startswith("Message-ID:")
        )
        observed_ids.append(message_id_line.split(":", 1)[1].strip())
    assert len(set(observed_ids)) == 2
    assert production_id not in observed_ids
    assert production_id == mailer._send_message_id(
        str(rows.loc[0, "record_id"]), "sender@example.com"
    )


@pytest.mark.parametrize(
    ("action", "status", "evidence_count", "pending_count", "bounce_class"),
    [
        ("failed", "5.1.1", 1, 0, "hard"),
        ("failed", "5.7.1", 1, 0, "soft"),
        ("failed", "4.2.2", 1, 0, "soft"),
        ("delayed", "4.2.0", 0, 1, None),
        ("delivered", "2.0.0", 0, 1, None),
        (None, "5.1.1", 1, 0, "unverified"),
    ],
)
def test_structured_dsn_requires_action_status_and_exact_message_identity(
    action, status, evidence_count, pending_count, bounce_class
):
    expected_message_id = "<mailpilot.exact@example.com>"
    parsed = mailer._parse_bounce_candidate(
        _dsn_message(
            action=action,
            status=status,
            message_id=expected_message_id,
        ),
        uid="42",
    )

    assert len(parsed["evidence"]) == evidence_count
    assert len(parsed["non_permanent"]) == pending_count
    candidates = parsed["evidence"] or parsed["non_permanent"]
    assert candidates[0]["message_ids"] == [expected_message_id]
    if bounce_class is not None:
        assert candidates[0]["bounce_class"] == bounce_class
        assert candidates[0]["verified_failure"] is (action == "failed")


def test_nonstandard_bounce_is_manual_review_only():
    parsed = mailer._parse_bounce_candidate(
        b"From: mailer-daemon@example.net\r\nSubject: Delivery failed\r\n\r\n"
        b"Something went wrong without a structured DSN.\r\n",
        uid="9",
    )

    assert parsed["evidence"] == []
    assert parsed["non_permanent"] == []
    assert len(parsed["unverified_candidates"]) == 1


def test_imap_scan_uses_distinct_credentials_and_body_peek(monkeypatch):
    raw = _dsn_message()
    header = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"

    class FakeIMAP:
        instance = None

        def __init__(self, *args, **kwargs):
            self.login_args = None
            self.fetch_specs = []
            FakeIMAP.instance = self

        def login(self, user, password):
            self.login_args = (user, password)

        def select(self, mailbox, readonly=False):
            assert (mailbox, readonly) == ("INBOX", True)
            return "OK", [b"1"]

        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"7"]
            uid, spec = args
            self.fetch_specs.append((uid, spec))
            payload = header if "HEADER.FIELDS" in spec else raw
            return "OK", [(b"metadata", payload)]

        def response(self, name):
            assert name == "UIDVALIDITY"
            return "UIDVALIDITY", [b"123"]

        def logout(self):
            pass

    monkeypatch.setattr(mailer.imaplib, "IMAP4_SSL", FakeIMAP)
    result = mailer._extract_bounces(
        {
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "imap_user": "bounce-reader@example.com",
            "imap_password": "imap-secret",
        },
        lookback=50,
    )

    assert FakeIMAP.instance.login_args == (
        "bounce-reader@example.com",
        "imap-secret",
    )
    assert FakeIMAP.instance.fetch_specs == [
        (b"7", "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT CONTENT-TYPE)])"),
        (b"7", "(BODY.PEEK[])"),
    ]
    assert result["coverage"]["complete"] is True
    assert result["coverage"]["uidvalidity"] == "123"
    assert result["bounces"]["bad@example.com"]["evidence"][0][
        "verified_failure"
    ] is True


def test_imap_fetch_failure_makes_coverage_incomplete(monkeypatch):
    raw = _dsn_message()
    header = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"

    class FailingIMAP:
        def __init__(self, *args, **kwargs):
            pass

        def login(self, _user, _password):
            pass

        def select(self, _mailbox, readonly=False):
            return "OK", [b"1"]

        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"8"]
            _uid, spec = args
            if "HEADER.FIELDS" in spec:
                return "OK", [(b"metadata", header)]
            return "NO", []

        def response(self, _name):
            return "UIDVALIDITY", [b"456"]

        def logout(self):
            pass

    monkeypatch.setattr(mailer.imaplib, "IMAP4_SSL", FailingIMAP)
    result = mailer._extract_bounces(
        {
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "imap_user": "reader@example.com",
            "imap_password": "secret",
        },
        lookback=50,
    )

    assert result["coverage"]["complete"] is False
    assert result["coverage"]["fetch_errors"] == [{"uid": "8", "stage": "message"}]


def test_dsn_evidence_from_two_messages_cannot_be_stitched_together(tmp_path):
    _sendable, _rows, expected_message_id, args = _trusted_sent_batch(tmp_path)
    extracted = {
        "bounces": {
            "bad@example.com": {
                "reasons": ["failed without current identity", "current but unverified"],
                "message_ids": {"<old@example.com>", expected_message_id},
                "evidence": [
                    {
                        "reason": "failed without current identity",
                        "message_ids": {"<old@example.com>"},
                        "verified_failure": True,
                        "action": "failed",
                        "status": "5.1.1",
                        "bounce_class": "hard",
                    },
                    {
                        "reason": "current but unverified",
                        "message_ids": {expected_message_id},
                        "verified_failure": False,
                        "action": "",
                        "status": "",
                        "bounce_class": "unverified",
                    },
                ],
            }
        },
        "unverified_candidates": [
            {
                "uid": "99",
                "email": "",
                "reason": "malformed extra candidate",
                "expected_message_id": "",
                "observed_message_ids": [],
            }
        ],
        "non_permanent": [],
        "coverage": {"complete": True},
    }
    original_extract = mailer._extract_bounces
    try:
        mailer._extract_bounces = lambda cfg, lookback, since_time: extracted
        result = mailer.cmd_bounces(args)
    finally:
        mailer._extract_bounces = original_extract

    assert result["matched"] == 0
    assert result["applied"] == 0
    assert result["unverified"] == 2


@pytest.mark.parametrize(("action", "expected_unverified"), [("delayed", 1), ("delivered", 0)])
def test_current_message_delivery_state_controls_pending_review(
    tmp_path, action, expected_unverified
):
    _sendable, _rows, expected_message_id, args = _trusted_sent_batch(tmp_path)
    extracted = {
        "bounces": {},
        "unverified_candidates": [],
        "non_permanent": [
            {
                "uid": "12",
                "email": "bad@example.com",
                "action": action,
                "status": "4.2.0" if action == "delayed" else "2.0.0",
                "reason": action,
                "message_ids": [expected_message_id],
            }
        ],
        "coverage": {"complete": True},
    }
    original_extract = mailer._extract_bounces
    try:
        mailer._extract_bounces = lambda cfg, lookback, since_time: extracted
        result = mailer.cmd_bounces(args)
    finally:
        mailer._extract_bounces = original_extract

    assert result["matched"] == 0
    assert result["unverified"] == expected_unverified
    if action == "delayed":
        assert result["unverified_candidates"][0]["bounce_class"] == "pending"


def test_gb18030_csv_works_in_cli_preview_and_web_column_preflight(tmp_path):
    source_bytes = "姓名,邮箱\n小凯,kay@example.com\n".encode("gb18030")
    source = tmp_path / "people.csv"
    output = tmp_path / "sendable.csv"
    source.write_bytes(source_bytes)

    mailer.cmd_preview(
        Namespace(
            file=str(source),
            out=str(output),
            template="default",
            email_col="邮箱",
            name_col="姓名",
            group_col="__none__",
            sent_col="__none__",
        )
    )
    rows = pd.read_csv(output, dtype=object).fillna("")
    assert rows.loc[0, "name"] == "小凯"
    assert rows.loc[0, "sendable"] == "yes"

    web_app.RUNS_DIR = tmp_path / "runs"
    web_app.app.config.update(TESTING=True)
    response = web_app.app.test_client().post(
        "/columns",
        data={"recipients": (io.BytesIO(source_bytes), "people.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.get_json()["columns"] == ["姓名", "邮箱"]


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("USER@例子.公司", "USER@xn--fsqu00a.xn--55qx5d"),
        ("用户@example.com", None),
        (f"{'a' * 65}@example.com", None),
        ("a@example", None),
        ("a..b@example.com", None),
        ("a@example.com\r\nBcc: victim@example.com", None),
    ],
)
def test_email_validation_is_conservative_and_idn_aware(address, expected):
    assert mailer._valid_email(address) == expected


def test_downloadable_status_csv_neutralizes_spreadsheet_formulas():
    exported = web_app._spreadsheet_safe_export(
        pd.DataFrame(
            {
                "name": ["=HYPERLINK(\"https://evil.invalid\")", " normal"],
                "note": ["+cmd", "safe"],
            }
        )
    )

    assert exported.loc[0, "name"].startswith("'=")
    assert exported.loc[0, "note"].startswith("'+")
    assert exported.loc[1, "name"] == " normal"


def test_local_web_app_rejects_untrusted_host_header():
    web_app.app.config.update(TESTING=True)
    response = web_app.app.test_client().get("/", headers={"Host": "evil.example"})

    assert response.status_code == 400


def test_normalized_config_keeps_smtp_and_imap_credentials_separate():
    normalized = mailer.normalize_config(
        {
            "smtp": {
                "host": "smtp.example.com",
                "user": "sender@example.com",
                "password": "smtp-secret",
            },
            "imap": {
                "host": "imap.example.com",
                "user": "bounce-reader@example.com",
                "password": "imap-secret",
            },
            "from_addr": "sender@example.com",
        }
    )

    assert normalized["user"] == "sender@example.com"
    assert normalized["password"] == "smtp-secret"
    assert normalized["imap_user"] == "bounce-reader@example.com"
    assert normalized["imap_password"] == "imap-secret"


def test_exchange_online_password_imap_is_blocked_before_network_access():
    with pytest.raises(mailer.LedgerIntegrityError, match="does not implement IMAP OAuth"):
        mailer._extract_bounces(
            {
                "imap_host": "outlook.office365.com",
                "imap_port": 993,
                "imap_user": "sender@example.com",
                "imap_password": "password",
            },
            lookback=10,
        )


def test_web_template_redirect_testing_stops_after_first_provider_failure(tmp_path):
    run_dir, sendable, token = _prepare_web_run(tmp_path, row_count=2)
    rows = pd.read_csv(sendable, dtype=object).fillna("")
    rows.at[1, "template"] = "waitlist"
    rows.to_csv(sendable, index=False)
    _seal_sendable(sendable, engine=web_app.mailer)
    reviewed = pd.read_csv(sendable, dtype=object).fillna("")
    web_app._write_manifest(
        run_dir,
        content_hash=web_app._content_hash(reviewed),
        batch_fingerprint=web_app._batch_fingerprint(reviewed),
    )

    class RejectingSMTP:
        calls = 0

        def sendmail(self, _from, _recipients, _message):
            self.calls += 1
            raise smtplib.SMTPDataError(421, b"temporary provider throttle")

        def quit(self):
            pass

    server = RejectingSMTP()
    original_connect = web_app.mailer.connect_smtp
    try:
        web_app.mailer.connect_smtp = lambda _cfg: server
        web_app.app.config.update(TESTING=True)
        response = web_app.app.test_client().post(
            "/send",
            data=_web_send_form(
                run_dir,
                sendable,
                token,
                action="test",
                redirect_to="sender@example.com",
                confirm_real_send="",
                confirm_test_received="",
                confirm_phrase="",
            ),
        )
    finally:
        web_app.mailer.connect_smtp = original_connect

    assert response.status_code == 200
    assert server.calls == 1
    assert "后续模板测试已停止".encode() in response.data
    assert not (run_dir / "redirect-test-passed.json").exists()


def test_web_blocks_microsoft_365_production_before_smtp(tmp_path):
    run_dir, sendable, token = _prepare_web_run(tmp_path, row_count=1)
    rows = pd.read_csv(sendable, dtype=object).fillna("")
    form = _web_send_form(
        run_dir,
        sendable,
        token,
        host="smtp.office365.com",
        port="587",
        use_ssl="",
    )
    web_app._write_test_marker(run_dir, rows, ["default"], form)
    calls = []
    original_connect = web_app.mailer.connect_smtp
    try:
        web_app.mailer.connect_smtp = lambda _cfg: calls.append("connected")
        web_app.app.config.update(TESTING=True)
        response = web_app.app.test_client().post("/send", data=form)
    finally:
        web_app.mailer.connect_smtp = original_connect

    assert response.status_code == 200
    assert "Microsoft 365 正式发送已阻断".encode() in response.data
    assert calls == []
    assert not Path(web_app.mailer._default_ledger_path(str(sendable))).exists()
