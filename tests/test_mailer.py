import importlib.util
import json
import smtplib
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
                redirect_to="test@example.com",
                limit=1,
                sleep=0,
            )
        )
    finally:
        mailer.connect_smtp = original_connect

    rows = pd.read_csv(sendable, dtype=object).fillna("")
    assert rows.loc[0, "status"] == ""
    assert fake.actual == ["test@example.com"]
    assert not Path(f"{sendable}.sendlog.jsonl").exists()
    events = [
        json.loads(line)
        for line in Path(f"{sendable}.testlog.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["status"] == "test_sent"


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
    ledger.write_text('{"row": 0, "status": "sent"}\n{"broken"', encoding="utf-8")

    with pytest.raises(mailer.LedgerIntegrityError, match="damaged"):
        mailer._apply_ledger(rows, str(ledger))


def test_batch_lock_rejects_a_second_sender(tmp_path):
    lock_path = tmp_path / "send.lock"
    with mailer.BatchLock(str(lock_path)):
        with pytest.raises(mailer.BatchAlreadyLocked):
            with mailer.BatchLock(str(lock_path)):
                pass


def test_web_real_send_honors_limit(tmp_path):
    run_dir, sendable, token = _prepare_web_run(tmp_path)
    rows = pd.read_csv(sendable, dtype=object).fillna("")
    (run_dir / "redirect-test-passed.txt").write_text(
        web_app._content_hash(rows), encoding="utf-8"
    )

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
        response = web_app.app.test_client().post(
            "/send", data=_web_send_form(run_dir, sendable, token)
        )
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
