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
        "confirm_phrase": "发送 1 封",
    }
    values.update(overrides)
    return values


def test_column_detection_supports_chinese_headers():
    df = pd.DataFrame({"姓名": ["Ada"], "邮箱": ["ada@example.com"], "组别": ["confirmed"]})

    assert mailer._find_col(df, mailer.NAME_ALIASES) == "姓名"
    assert mailer._find_col(df, mailer.EMAIL_ALIASES) == "邮箱"
    assert mailer._find_col(df, mailer.GROUP_ALIASES) == "组别"


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


def test_batch_lock_rejects_a_second_sender(tmp_path):
    lock_path = tmp_path / "send.lock"
    with mailer.BatchLock(str(lock_path)):
        with pytest.raises(mailer.BatchAlreadyLocked):
            with mailer.BatchLock(str(lock_path)):
                pass


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
    assert b"redirected test inbox is required" in response.data
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
    assert b"also present in the customer list" in response.data
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
    assert b"successful redirected test" in response.data
    assert not Path(f"{sendable}.sendlog.jsonl").exists()


def test_web_real_send_requires_matching_redirect_test(tmp_path):
    run_dir, sendable, token = _prepare_web_run(tmp_path, row_count=1)
    web_app.app.config.update(TESTING=True)
    response = web_app.app.test_client().post(
        "/send", data=_web_send_form(run_dir, sendable, token)
    )

    assert response.status_code == 200
    assert b"successful redirected test" in response.data
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
    assert b"already used" in second.data


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
    assert b"Existing batch reopened" in response.data
    assert b'<div class="label">Skipped</div><div class="value">1</div>' in response.data


def test_web_equivalent_reexport_reopens_original_batch(tmp_path):
    web_app.RUNS_DIR = tmp_path
    tmp_path.mkdir(exist_ok=True)
    web_app.app.config.update(TESTING=True)
    client = web_app.app.test_client()
    csv_bytes = b"name,email,group\nAda,ada@example.com,confirmed\n"
    form = {
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
    assert b"uploaded recipient copy was deleted" in response.data
    assert list(tmp_path.glob("mailpilot_*")) == []


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
    assert b"sendable.csv is missing" in response.data
    assert list(tmp_path.glob("mailpilot_*")) == [broken]
