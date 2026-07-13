#!/usr/bin/env python3
"""
mailer — send bulk personalized emails from a spreadsheet, with delivery tracking.

Two modes:
  simple   one recipient list + one template -> the same email to everyone (personalized greeting)
  grouped  the list has a "group" column -> each group uses its own template

Commands:
  preview  read the list -> render templates -> print per-group counts + a sample + un-sendable rows
  send     send in batches: idempotent, schedulable (--at), throttled (--sleep), test redirect (--redirect-to)
  bounces  read bounce notifications from your mailbox and mark undelivered rows as bounced
  doctor   self-check: dependencies / config / SMTP connectivity

Design principle: this tool only faithfully sends the list you already grouped. Rows with no valid
email, or whose template can't be found, are surfaced for review and never sent automatically.
Deciding which group a row belongs to is your job (or the agent's, in chat) — the script never guesses.
"""
import argparse
import hashlib
import hmac
import imaplib
import json
import os
import re
import smtplib
import ssl
import sys
import time
import uuid
from datetime import datetime
from email import policy
from email.header import Header, decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.parser import BytesParser
from email.utils import formataddr, formatdate
from urllib.parse import urlsplit

import pandas as pd
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

if os.name == "nt":
    import msvcrt
else:
    import fcntl

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
PACKAGE_TEMPLATES_DIR = os.path.join(HERE, "templates")
PROJECT_TEMPLATES_DIR = os.path.join(PROJECT_ROOT, "templates")
TEMPLATES_DIR = PROJECT_TEMPLATES_DIR if os.path.isdir(PROJECT_TEMPLATES_DIR) else PACKAGE_TEMPLATES_DIR

# Column-name aliases for auto-detection. Chinese entries are intentional — they let the tool read
# spreadsheets with Chinese headers out of the box.
EMAIL_ALIASES = ["email", "e-mail", "mail", "邮箱", "邮件地址", "电子邮箱", "收件邮箱", "收件人邮箱", "收件人"]
NAME_ALIASES = ["name", "customer", "姓名", "名称", "客户", "客户名称", "经销商", "公司", "公司名称", "联系人"]
GROUP_ALIASES = [
    "group", "type", "category", "template", "status", "stage", "segment", "label", "tag",
    "组别", "分组", "类别", "类型", "组", "邮件类型", "模板", "状态", "报名状态", "用户状态",
    "客户状态", "客户阶段", "客户类型", "人群", "分层", "标签",
]
SENT_ALIASES = ["sent", "sent_flag", "已发", "已发送", "是否已发", "是否已发送", "是否发送", "发送状态", "已发状态"]

SUPPRESSED_STATUSES = {
    "sent",
    "error",
    "bounced",
    "soft_bounced",
    "unsubscribed",
    "suppressed",
    "unknown",
}
LEDGER_STATUSES = SUPPRESSED_STATUSES | {"attempting", "retry_authorized"}
MICROSOFT_PASSWORD_SMTP_HOSTS = {"smtp.office365.com", "smtp-mail.outlook.com"}
MICROSOFT_OAUTH_IMAP_HOSTS = {
    "outlook.office365.com",
    "imap.office365.com",
    "imap-mail.outlook.com",
}


class LedgerIntegrityError(RuntimeError):
    """Raised when a checkpoint ledger cannot be trusted for safe recovery."""


class BatchAlreadyLocked(RuntimeError):
    """Raised when another process already owns the batch send lock."""


class BatchLock:
    """Small cross-platform, crash-released, non-blocking file lock."""

    def __init__(self, path, blocking=False):
        self.path = path
        self.blocking = blocking
        self.handle = None

    def __enter__(self):
        self.handle = open(self.path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                self.handle.seek(0, os.SEEK_END)
                if self.handle.tell() == 0:
                    self.handle.write("0")
                    self.handle.flush()
                self.handle.seek(0)
                mode = msvcrt.LK_LOCK if self.blocking else msvcrt.LK_NBLCK
                msvcrt.locking(self.handle.fileno(), mode, 1)
            else:
                mode = fcntl.LOCK_EX if self.blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
                fcntl.flock(self.handle.fileno(), mode)
        except OSError as e:
            self.handle.close()
            self.handle = None
            raise BatchAlreadyLocked(self.path) from e
        return self

    def __exit__(self, exc_type, exc, traceback):
        if not self.handle:
            return
        try:
            if os.name == "nt":
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def _norm(s):
    return str(s).strip().lower().replace(" ", "").replace("　", "")


def _decode_hdr(v):
    if not v:
        return ""
    return "".join(t.decode(e or "utf-8", "ignore") if isinstance(t, bytes) else t
                   for t, e in decode_header(v))


def _find_col(df, aliases):
    norm = {_norm(c): c for c in df.columns}
    for a in aliases:
        exact = norm.get(_norm(a))
        if exact is not None:
            return exact
    candidates = []
    aliases_norm = [_norm(a) for a in aliases]
    for column in df.columns:
        normalized = _norm(column)
        if any(alias in normalized for alias in aliases_norm):
            candidates.append(column)
    return candidates[0] if len(candidates) == 1 else None


def _auto_col_candidates(df, aliases):
    aliases_norm = {_norm(a) for a in aliases}
    exact = [column for column in df.columns if _norm(column) in aliases_norm]
    if exact:
        return exact
    candidates = []
    for column in df.columns:
        normalized = _norm(column)
        if any(alias in normalized for alias in aliases_norm):
            candidates.append(column)
    return candidates


def _find_exact_cols(df, aliases):
    aliases_norm = {_norm(a) for a in aliases}
    return [c for c in df.columns if _norm(c) in aliases_norm]


def _require_column(df, requested, label):
    if not requested:
        return None
    if requested not in df.columns:
        sys.exit(f"❌ {label} column not found: {requested}. Choose one of: {list(df.columns)}")
    return requested


def _valid_email(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    if len(s) > 254 or any(c in s for c in "\r\n\t ,;<>") or s.count("@") != 1:
        return None
    local, domain = s.rsplit("@", 1)
    try:
        local.encode("ascii")
        domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if not local or len(local) > 64 or len(domain) > 253:
        return None
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local):
        return None
    labels = domain.split(".")
    if len(labels) < 2:
        return None
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not re.fullmatch(r"[a-z0-9-]+", label)
        for label in labels
    ):
        return None
    normalized = f"{local}@{domain}"
    return normalized if len(normalized) <= 254 else None


def _truthy_sent(v):
    # Recognizes both English and Chinese "already sent" markers.
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return False
    s = _norm(v)
    return s in ("y", "yes", "true", "1", "sent", "ok", "是", "已", "已发", "已发送") or "已发" in s


def _historical_send_state(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "unknown"
    s = _norm(v)
    if _truthy_sent(v):
        return "sent"
    if s in ("n", "no", "false", "0", "unsent", "pending", "否", "未", "未发", "未发送", "待发送"):
        return "unsent"
    return "unknown"


def get_env():
    return Environment(loader=FileSystemLoader(TEMPLATES_DIR), undefined=StrictUndefined,
                       trim_blocks=True, lstrip_blocks=True)


def _template_exists(name):
    return bool(name) and os.path.exists(os.path.join(TEMPLATES_DIR, f"{name}.txt"))


def render(env, template_name, ctx):
    tpl = env.get_template(f"{template_name}.txt")
    text = tpl.render(**ctx)
    lines = text.split("\n")
    subject = ""
    start = 0
    for i, line in enumerate(lines):
        if line.lower().startswith("subject:"):
            subject = line.split(":", 1)[1].strip()
            start = i + 1
            break
    return subject, "\n".join(lines[start:]).strip("\n")


def read_table(path):
    if path.lower().endswith(".xlsx"):
        return pd.read_excel(path, dtype=object)
    if path.lower().endswith(".xls"):
        sys.exit("❌ Legacy .xls is not supported. Save the file as .xlsx or CSV first.")
    utf8_error = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            frame = pd.read_csv(path, dtype=object, encoding=encoding)
            frame.attrs["mailpilot_encoding"] = encoding
            return frame
        except UnicodeDecodeError as e:
            utf8_error = e
    raise utf8_error or UnicodeDecodeError("utf-8", b"", 0, 1, "unknown encoding")


def _default_ledger_path(input_path):
    return f"{input_path}.sendlog.jsonl"


def _default_test_ledger_path(input_path):
    return f"{input_path}.testlog.jsonl"


def _default_lifecycle_path(input_path):
    return f"{input_path}.lifecycle.jsonl"


def _lifecycle_head_path(input_path):
    return f"{_default_lifecycle_path(input_path)}.head.json"


def _atomic_write_json_file(path, payload):
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "x", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_parent(path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _lifecycle_event_hash(event):
    payload = {key: value for key, value in event.items() if key != "event_hash"}
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_lifecycle(input_path):
    path = _default_lifecycle_path(input_path)
    if not os.path.exists(path):
        head_path = _lifecycle_head_path(input_path)
        if os.path.exists(head_path):
            try:
                with open(head_path, encoding="utf-8") as f:
                    empty_head = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                raise LedgerIntegrityError("Lifecycle head marker is damaged.") from e
            if empty_head.get("seq") == 0 and not empty_head.get("event_hash"):
                return []
            raise LedgerIntegrityError(
                "Lifecycle ledger is missing although its durable head marker exists."
            )
        return []
    events = []
    previous_hash = ""
    with open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as e:
                raise LedgerIntegrityError(
                    f"Lifecycle ledger is damaged at line {line_no}: {path}."
                ) from e
            if not isinstance(event, dict):
                raise LedgerIntegrityError(
                    f"Lifecycle ledger line {line_no} is not an event object."
                )
            if event.get("seq") != len(events) + 1:
                raise LedgerIntegrityError("Lifecycle ledger sequence is incomplete or reordered.")
            if str(event.get("prev_hash", "")) != previous_hash:
                raise LedgerIntegrityError("Lifecycle ledger hash chain is broken.")
            actual_hash = str(event.get("event_hash", ""))
            if not actual_hash or not hmac.compare_digest(
                actual_hash, _lifecycle_event_hash(event)
            ):
                raise LedgerIntegrityError("Lifecycle ledger event hash is invalid.")
            previous_hash = actual_hash
            events.append(event)
    head_path = _lifecycle_head_path(input_path)
    if not os.path.exists(head_path):
        raise LedgerIntegrityError("Lifecycle head marker is missing.")
    try:
        with open(head_path, encoding="utf-8") as f:
            head = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise LedgerIntegrityError("Lifecycle head marker is damaged.") from e
    head_seq = head.get("seq")
    head_hash = str(head.get("event_hash", ""))
    if head_seq == len(events) and hmac.compare_digest(head_hash, previous_hash):
        return events
    previous_event_hash = str(events[-2]["event_hash"]) if len(events) > 1 else ""
    if head_seq == len(events) - 1 and hmac.compare_digest(
        head_hash, previous_event_hash
    ):
        _atomic_write_json_file(
            head_path,
            {"seq": len(events), "event_hash": previous_hash},
        )
        return events
    raise LedgerIntegrityError(
        "Lifecycle ledger tail is missing or does not match its durable head marker."
    )


def _append_lifecycle(input_path, event):
    events = _read_lifecycle(input_path)
    head_path = _lifecycle_head_path(input_path)
    if not events and not os.path.exists(head_path):
        _atomic_write_json_file(head_path, {"seq": 0, "event_hash": ""})
    payload = dict(event)
    payload["seq"] = len(events) + 1
    payload["prev_hash"] = str(events[-1]["event_hash"]) if events else ""
    payload.setdefault(
        "time", datetime.now().astimezone().isoformat(timespec="seconds")
    )
    payload["event_hash"] = _lifecycle_event_hash(payload)
    _append_ledger(_default_lifecycle_path(input_path), payload)
    _atomic_write_json_file(
        head_path,
        {"seq": payload["seq"], "event_hash": payload["event_hash"]},
    )
    return payload


def _lifecycle_phase(input_path):
    phase = "PREVIEW"
    for event in _read_lifecycle(input_path):
        event_name = str(event.get("event", ""))
        if event_name == "outbound_opened":
            phase = "OUTBOUND_OPEN"
        elif event_name == "outbound_closed":
            phase = "OUTBOUND_CLOSED"
        elif event_name == "archived":
            phase = "ARCHIVED"
    return phase


def _ensure_lifecycle_open(input_path, batch_id, production_ledger=None):
    events = _read_lifecycle(input_path)
    production_ledger = production_ledger or _default_ledger_path(input_path)
    anchor = _ledger_anchor_path(production_ledger)

    def bootstrap_events():
        return list(_iter_ledger(production_ledger) or [])

    if not events and (
        os.path.exists(production_ledger)
        or os.path.exists(anchor)
        or os.path.exists(_ledger_integrity_path(production_ledger))
    ):
        try:
            existing = bootstrap_events()
        except LedgerIntegrityError as e:
            raise LedgerIntegrityError(
                "Production evidence exists but the lifecycle ledger is missing or incomplete. "
                "Reconcile manually."
            ) from e
        if not (
            len(existing) == 1
            and existing[0].get("event") == "batch_started"
            and str(existing[0].get("batch_id", "")) == str(batch_id)
        ):
            raise LedgerIntegrityError(
                "Production evidence exists but the lifecycle ledger is missing. Reconcile manually."
            )
    phase = _lifecycle_phase(input_path)
    if phase in ("OUTBOUND_CLOSED", "ARCHIVED"):
        raise LedgerIntegrityError(
            f"Batch lifecycle is {phase}; production sending can never resume from this batch."
        )
    if phase == "PREVIEW":
        if not events:
            if not os.path.exists(anchor):
                _ensure_ledger_anchor(production_ledger, batch_id)
            if not os.path.exists(production_ledger):
                _append_ledger(
                    production_ledger,
                    {
                        "event": "batch_started",
                        "batch_id": str(batch_id),
                        "status": "batch_started",
                        "send_time": datetime.now().astimezone().isoformat(
                            timespec="seconds"
                        ),
                    },
                )
        return _append_lifecycle(
            input_path,
            {"event": "outbound_opened", "batch_id": str(batch_id)},
        )
    if events and str(events[0].get("batch_id", "")) != str(batch_id):
        raise LedgerIntegrityError("Lifecycle ledger belongs to a different batch.")
    if phase == "OUTBOUND_OPEN":
        if not os.path.exists(anchor):
            raise LedgerIntegrityError(
                "Lifecycle says outbound is open, but the production start marker is missing. "
                "Do not resend until the batch is reconciled."
            )
        production_events = bootstrap_events()
        if not production_events or production_events[0].get("event") != "batch_started":
            raise LedgerIntegrityError(
                "Production ledger has no trusted batch-start event. Do not resend."
            )
        if str(production_events[0].get("batch_id", "")) != str(batch_id):
            raise LedgerIntegrityError(
                "Production ledger batch-start event belongs to a different batch."
            )
    return events[0] if events else None


def _ledger_anchor_path(ledger_path):
    return f"{ledger_path}.started"


def _ledger_integrity_path(ledger_path):
    return f"{ledger_path}.integrity.json"


def _ledger_digest(ledger_path):
    digest = hashlib.sha256()
    size = 0
    event_count = 0
    with open(ledger_path, "rb") as f:
        for line in f:
            digest.update(line)
            size += len(line)
            if line.strip():
                event_count += 1
    return {
        "sha256": digest.hexdigest(),
        "size": size,
        "event_count": event_count,
    }


def _validate_ledger_integrity(ledger_path, required=False):
    marker_path = _ledger_integrity_path(ledger_path)
    if not os.path.exists(marker_path):
        if required:
            raise LedgerIntegrityError(
                f"Production ledger integrity marker is missing: {marker_path}. "
                "Do not resend until the batch is reconciled."
            )
        return None
    try:
        with open(marker_path, encoding="utf-8") as f:
            stored = json.load(f)
        current = _ledger_digest(ledger_path)
    except (OSError, json.JSONDecodeError) as e:
        raise LedgerIntegrityError(
            f"Production ledger integrity marker is damaged: {marker_path}."
        ) from e
    try:
        matches = (
            hmac.compare_digest(str(stored.get("sha256", "")), current["sha256"])
            and int(stored.get("size", -1)) == current["size"]
            and int(stored.get("event_count", -1)) == current["event_count"]
        )
    except (TypeError, ValueError):
        matches = False
    if not matches:
        try:
            stored_size = int(stored.get("size", -1))
            stored_count = int(stored.get("event_count", -1))
            with open(ledger_path, "rb") as f:
                prefix = f.read(stored_size) if stored_size >= 0 else b""
                suffix = f.read()
            suffix_lines = [line for line in suffix.splitlines() if line.strip()]
            recoverable_tail = (
                stored_size >= 0
                and stored_count >= 0
                and current["size"] > stored_size
                and hmac.compare_digest(
                    hashlib.sha256(prefix).hexdigest(), str(stored.get("sha256", ""))
                )
                and (not prefix or prefix.endswith(b"\n"))
                and suffix.endswith(b"\n")
                and len(suffix_lines) == 1
                and current["event_count"] == stored_count + 1
                and isinstance(json.loads(suffix_lines[0].decode("utf-8")), dict)
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            recoverable_tail = False
        if recoverable_tail:
            _atomic_write_json_file(marker_path, current)
            return current
        raise LedgerIntegrityError(
            "Production ledger does not match its durable integrity marker. "
            "A complete event may be missing, added, or changed; do not resend."
        )
    return current


def _check_ledger_presence(ledger_path):
    anchor = _ledger_anchor_path(ledger_path)
    if os.path.exists(anchor) and not os.path.exists(ledger_path):
        raise LedgerIntegrityError(
            f"Production ledger is missing but its start marker exists: {ledger_path}. "
            "Do not resend until the batch is reconciled."
        )
    if os.path.exists(anchor) and os.path.exists(ledger_path) and os.path.getsize(ledger_path) == 0:
        raise LedgerIntegrityError(
            f"Production ledger is empty although sending was started: {ledger_path}."
        )


def _ensure_ledger_anchor(ledger_path, batch_id):
    anchor = _ledger_anchor_path(ledger_path)
    if os.path.exists(anchor):
        try:
            with open(anchor, encoding="utf-8") as f:
                stored = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise LedgerIntegrityError(f"Production start marker is damaged: {anchor}") from e
        if str(stored.get("batch_id", "")) != str(batch_id):
            raise LedgerIntegrityError("Production start marker belongs to a different batch.")
        return
    os.makedirs(os.path.dirname(os.path.abspath(anchor)) or ".", exist_ok=True)
    payload = json.dumps(
        {
            "batch_id": str(batch_id),
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    try:
        fd = os.open(anchor, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _ensure_ledger_anchor(ledger_path, batch_id)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    _fsync_parent(anchor)


def _record_id(
    idx,
    email_addr,
    template,
    subject,
    body,
    sendable="yes",
    reason="",
    initial_status="",
    activity_id="",
):
    payload = json.dumps(
        [
            int(idx),
            email_addr.lower(),
            template,
            subject,
            body,
            sendable,
            reason,
            initial_status,
            activity_id,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _batch_id_for_record_ids(record_ids):
    payload = json.dumps(list(record_ids), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_reviewed_batch(df):
    required = {
        "batch_id",
        "record_id",
        "email",
        "template",
        "subject",
        "body",
        "status",
        "initial_status",
        "sendable",
        "reason",
    }
    missing = sorted(required.difference(df.columns))
    if missing:
        raise LedgerIntegrityError(
            f"Sendable file is missing review-manifest columns: {missing}. Run preview again."
        )
    normalized_batch_ids = [str(value).strip() for value in df["batch_id"]]
    batch_ids = set(normalized_batch_ids)
    if len(batch_ids) != 1 or not normalized_batch_ids or not normalized_batch_ids[0]:
        raise LedgerIntegrityError("Sendable file must contain exactly one non-empty batch_id.")
    expected_batch_id = _batch_id_for_record_ids(df["record_id"].astype(str))
    if not hmac.compare_digest(normalized_batch_ids[0], expected_batch_id):
        raise LedgerIntegrityError(
            "Batch seal mismatch. Rows may have been removed, reordered, or replaced. Run preview again."
        )
    for idx, row in df.iterrows():
        expected = _record_id(
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
        if not hmac.compare_digest(str(row.get("record_id", "")).strip(), expected):
            raise LedgerIntegrityError(
                f"Reviewed content mismatch at row {idx}. Run preview again; do not edit sendable.csv."
            )


def _atomic_write_csv(df, path):
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
            df.to_csv(f, index=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            directory_fd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Directory fsync is not available on every platform (notably some Windows setups).
            pass
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _fsync_parent(path):
    try:
        directory_fd = os.open(os.path.dirname(os.path.abspath(path)) or ".", os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        # Directory fsync is not available on every platform (notably some Windows setups).
        pass


def _append_ledger(path, event):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    new_file = not os.path.exists(path)
    anchored = os.path.exists(_ledger_anchor_path(path))
    if anchored and not new_file:
        _check_ledger_presence(path)
        _validate_ledger_integrity(path, required=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
    if new_file:
        _fsync_parent(path)
    if anchored:
        _atomic_write_json_file(_ledger_integrity_path(path), _ledger_digest(path))


def _iter_ledger(path):
    _check_ledger_presence(path)
    if not path or not os.path.exists(path):
        return
    anchored = os.path.exists(_ledger_anchor_path(path))
    if anchored:
        _validate_ledger_integrity(path, required=True)
    seen_event = False
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as e:
                raise LedgerIntegrityError(
                    f"Checkpoint ledger is damaged at line {line_no}: {path}. "
                    "Stop and reconcile this batch before sending again."
                ) from e
            if not isinstance(event, dict):
                raise LedgerIntegrityError(
                    f"Checkpoint ledger line {line_no} is not an event object: {path}"
                )
            seen_event = True
            yield event
    if anchored and not seen_event:
        raise LedgerIntegrityError(
            f"Production ledger has no valid events although sending was started: {path}."
        )


def _apply_ledger(df, path):
    latest = {}
    for ev in _iter_ledger(path) or []:
        if ev.get("event") == "batch_started":
            current_batch_ids = set(df.get("batch_id", pd.Series(dtype=object)).astype(str))
            if len(current_batch_ids) != 1 or str(ev.get("batch_id", "")) not in current_batch_ids:
                raise LedgerIntegrityError(
                    "Checkpoint batch-start event does not match the reviewed batch."
                )
            continue
        try:
            idx = int(ev["row"])
        except (KeyError, TypeError, ValueError) as e:
            raise LedgerIntegrityError(f"Checkpoint event has no valid row: {ev}") from e
        if idx not in df.index:
            raise LedgerIntegrityError(
                f"Checkpoint row {idx} does not exist in the current sendable file. "
                "The file may have been reordered or replaced."
            )
        current_email = str(df.at[idx, "email"]).strip().lower() if "email" in df.columns else ""
        event_email = str(ev.get("email", "")).strip().lower()
        if not event_email:
            raise LedgerIntegrityError(f"Checkpoint event at row {idx} has no email identity.")
        if current_email != event_email:
            raise LedgerIntegrityError(
                f"Checkpoint identity mismatch at row {idx}: ledger={event_email}, "
                f"current={current_email}. Refusing unsafe recovery."
            )
        current_record_id = (
            str(df.at[idx, "record_id"]).strip() if "record_id" in df.columns else ""
        )
        event_record_id = str(ev.get("record_id", "")).strip()
        if current_record_id and not event_record_id:
            raise LedgerIntegrityError(f"Checkpoint event at row {idx} has no record_id identity.")
        if event_record_id and current_record_id and event_record_id != current_record_id:
            raise LedgerIntegrityError(
                f"Checkpoint content mismatch at row {idx}. The reviewed email changed."
            )
        current_batch_id = (
            str(df.at[idx, "batch_id"]).strip() if "batch_id" in df.columns else ""
        )
        event_batch_id = str(ev.get("batch_id", "")).strip()
        if current_batch_id and not event_batch_id:
            raise LedgerIntegrityError(f"Checkpoint event at row {idx} has no batch_id identity.")
        if event_batch_id and current_batch_id and event_batch_id != current_batch_id:
            raise LedgerIntegrityError(f"Checkpoint batch mismatch at row {idx}.")
        status = str(ev.get("status", "")).strip()
        if status not in LEDGER_STATUSES:
            raise LedgerIntegrityError(
                f"Checkpoint event at row {idx} has unknown status {status!r}."
            )
        latest[idx] = dict(ev)

    applied = 0
    for idx, ev in latest.items():
        status = str(ev.get("status", "")).strip()
        if status == "attempting":
            status = "unknown"
            ev["send_error"] = (
                "Previous attempt has no recorded SMTP result. Verify the mailbox/provider "
                "before deciding whether to retry."
            )
        df.at[idx, "status"] = status
        if ev.get("send_time"):
            df.at[idx, "send_time"] = ev["send_time"]
        if "send_error" in ev:
            df.at[idx, "send_error"] = ev.get("send_error") or ""
        applied += 1
    return applied


# ---------------- preview ----------------

def cmd_preview(args):
    df = read_table(args.file)
    source_encoding = str(df.attrs.get("mailpilot_encoding", ""))
    if source_encoding:
        print(f"CSV encoding: {source_encoding}")
    activity_id = str(getattr(args, "activity_id", "") or "").strip() or uuid.uuid4().hex
    if activity_id and not re.fullmatch(r"[A-Za-z0-9._-]{2,80}", activity_id):
        sys.exit(
            "❌ --activity-id must use 2-80 letters, numbers, dots, dashes, or underscores."
        )
    print(f"Activity ID: {activity_id}")
    if args.email_col:
        email_col = _require_column(df, args.email_col, "Email")
    else:
        email_candidates = _auto_col_candidates(df, EMAIL_ALIASES)
        if len(email_candidates) > 1:
            sys.exit(
                "❌ Multiple possible email columns found: "
                f"{email_candidates}. Choose the recipient column explicitly with --email-col."
            )
        email_col = email_candidates[0] if email_candidates else None
    if args.name_col:
        name_col = _require_column(df, args.name_col, "Name")
    else:
        name_candidates = _auto_col_candidates(df, NAME_ALIASES)
        if len(name_candidates) > 1:
            sys.exit(
                "❌ Multiple possible name columns found: "
                f"{name_candidates}. Choose one explicitly with --name-col."
            )
        name_col = name_candidates[0] if name_candidates else None
    requested_group = args.group_col
    if requested_group == "__none__":
        group_col = None
    elif requested_group:
        group_col = _require_column(df, requested_group, "Group/template")
    else:
        group_candidates = _find_exact_cols(df, GROUP_ALIASES)
        if len(group_candidates) > 1:
            sys.exit(
                "❌ Multiple possible group/template columns found: "
                f"{group_candidates}. Choose one explicitly with --group-col."
            )
        group_col = group_candidates[0] if group_candidates else None
    requested_sent = getattr(args, "sent_col", None)
    if requested_sent == "__none__":
        sent_col = None
    elif requested_sent:
        sent_col = _require_column(df, requested_sent, "Already-sent status")
    else:
        sent_candidates = _find_exact_cols(df, SENT_ALIASES)
        if len(sent_candidates) > 1:
            sys.exit(
                "❌ Multiple possible already-sent columns found: "
                f"{sent_candidates}. Choose one explicitly with --sent-col."
            )
        sent_col = sent_candidates[0] if sent_candidates else None
    if not email_col:
        sys.exit("❌ No email column found. Use --email-col to specify it, or make sure the file has an 'email' column.")

    env = get_env()
    rows = []
    per = {}
    unsendable = []
    present_cnt = 0
    normalized_emails = df[email_col].map(lambda v: (_valid_email(v) or "").lower())
    duplicate_emails = set(
        normalized_emails[normalized_emails.ne("") & normalized_emails.duplicated(keep=False)]
    )
    for source_idx, r in df.iterrows():
        email_v = _valid_email(r.get(email_col))
        name = ""
        if name_col:
            nv = r.get(name_col)
            name = "" if (nv is None or (isinstance(nv, float) and pd.isna(nv))) else str(nv).strip()
        tpl = args.template
        missing_group = False
        if group_col:
            gv = r.get(group_col)
            gv = "" if (gv is None or (isinstance(gv, float) and pd.isna(gv))) else str(gv).strip()
            if gv:
                tpl = gv
            else:
                tpl = ""
                missing_group = True

        subject = body = ""
        sendable = True
        reason = ""
        historical_state = _historical_send_state(r.get(sent_col)) if sent_col else "unsent"
        if not email_v:
            sendable, reason = False, "no valid email address"
        elif email_v.lower() in duplicate_emails:
            sendable, reason = False, "duplicate email address in this batch"
        elif missing_group:
            sendable, reason = False, f"missing business classification in column: {group_col}"
        elif historical_state == "unknown":
            sendable, reason = False, f"ambiguous history in already-sent column: {sent_col}"
        elif not _template_exists(tpl):
            sendable, reason = False, f"template not found: templates/{tpl}.txt"
        else:
            try:
                context = {
                    str(key): "" if pd.isna(value) else value for key, value in r.items()
                }
                context["name"] = name or "there"
                subject, body = render(env, tpl, context)
                if not subject.strip():
                    sendable, reason = False, "rendered subject is empty"
                elif not body.strip():
                    sendable, reason = False, "rendered body is empty"
            except Exception as e:
                sendable, reason = False, f"template render failed: {e}"

        status = ""
        if historical_state == "sent":
            status = "sent"
            present_cnt += 1

        sendable_value = "yes" if sendable else "no"
        record_id = _record_id(
            source_idx,
            email_v or "",
            tpl,
            subject,
            body,
            sendable_value,
            reason,
            status,
            activity_id,
        )
        rows.append({
            "record_id": record_id,
            "name": name, "email": email_v or "", "template": tpl,
            "subject": subject, "body": body, "status": status,
            "initial_status": status,
            "activity_id": activity_id,
            "sendable": sendable_value, "reason": reason,
            "send_time": "", "send_error": "",
        })
        if sendable and status != "sent":
            per.setdefault(tpl, []).append(len(rows) - 1)
        if not sendable:
            unsendable.append((email_v or "(no email)", reason))

    batch_id = _batch_id_for_record_ids(row["record_id"] for row in rows)
    out = pd.DataFrame(rows)
    out.insert(0, "batch_id", batch_id)
    out.to_csv(args.out, index=False, encoding="utf-8-sig")

    print(f"Read {len(df)} row(s) -> wrote {args.out}")
    if group_col:
        print(f"Mode: grouped (by column '{group_col}'; each group uses its own template)")
    else:
        print(f"Mode: simple (everyone gets template '{args.template}')")
    print(f"\nWill send (grouped by template, {sum(len(v) for v in per.values())} total):")
    for tpl, idxs in per.items():
        print(f"  ▶ [{tpl}] {len(idxs)} email(s)")
        r = rows[idxs[0]]
        print(f"      Subject: {r['subject']}")
        preview = "\n        ".join(r["body"].split("\n")[:6])
        print(f"      Sample body (first recipient):\n        {preview}")
    if present_cnt:
        print(f"\nMarked as already sent, will skip: {present_cnt} row(s)")
    if unsendable:
        print(f"\n⚠️  Cannot send, needs your attention: {len(unsendable)} row(s) (will NOT be sent)")
        for e, reason in unsendable[:10]:
            print(f"  - {e}: {reason}")
        if len(unsendable) > 10:
            print(f"  … {len(unsendable) - 10} more in {args.out}")
    print("\nReview the above, then run `send` (test with --dry-run or --redirect-to first).")


# ---------------- send ----------------

def _wait_until(at_str):
    target = _parse_dt(at_str)
    if target is None:
        sys.exit(f"❌ Could not parse send time: {at_str} (expected e.g. 2026-07-02 09:00)")
    delay = (target - datetime.now()).total_seconds()
    if delay <= 0:
        print(f"⚠️  Scheduled time {target} is in the past; sending now.")
        return
    print(f"⏰ Scheduled for {target.strftime('%Y-%m-%d %H:%M:%S')}; waiting {int(delay)}s… (keep this process running)")
    time.sleep(delay)
    print(f"⏰ Time reached; sending now ({datetime.now().strftime('%H:%M:%S')})")


def _parse_dt(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(s).strip(), fmt)
        except ValueError:
            continue
    try:
        return pd.to_datetime(s).to_pydatetime()
    except Exception:
        return None


def _is_ambiguous_smtp_error(exc):
    if isinstance(
        exc,
        (
            smtplib.SMTPRecipientsRefused,
            smtplib.SMTPSenderRefused,
            smtplib.SMTPDataError,
            smtplib.SMTPAuthenticationError,
            smtplib.SMTPNotSupportedError,
        ),
    ):
        return False
    return isinstance(exc, (smtplib.SMTPServerDisconnected, TimeoutError, OSError))


def _smtp_response_codes(exc):
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = []
        for value in exc.recipients.values():
            try:
                codes.append(int(value[0]))
            except (IndexError, TypeError, ValueError):
                continue
        return codes
    code = getattr(exc, "smtp_code", None)
    try:
        return [int(code)] if code is not None else []
    except (TypeError, ValueError):
        return []


def _is_transient_smtp_error(exc):
    return any(400 <= code < 500 for code in _smtp_response_codes(exc))


def _send_message_id(record_id, from_addr, activity_id="", namespace=""):
    domain = str(from_addr).rsplit("@", 1)[-1] if "@" in str(from_addr) else "mailpilot.local"
    identity = f"{activity_id}:{record_id}" if activity_id else str(record_id)
    if namespace:
        identity = f"{namespace}:{identity}"
    safe_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:40]
    return f"<mailpilot.{safe_id}@{domain}>"


def _cmd_send_locked(args):
    df = pd.read_csv(args.input, dtype=object).fillna("")
    if not args.dry_run:
        _validate_reviewed_batch(df)
    production_ledger = args.ledger or _default_ledger_path(args.input)
    event_ledger = (
        _default_test_ledger_path(args.input) if args.redirect_to else production_ledger
    )
    applied = _apply_ledger(df, production_ledger)
    if applied:
        print(f"Recovered {applied} status update(s) from ledger: {production_ledger}")
    unresolved_unknown = (
        df["status"].astype(str).str.strip().str.lower().eq("unknown")
        | df["initial_status"].astype(str).str.strip().str.lower().eq("unknown")
    )
    if unresolved_unknown.any() and not args.redirect_to and not args.dry_run:
        raise LedgerIntegrityError(
            f"Batch has {int(unresolved_unknown.sum())} unknown result(s). "
            "Resolve them with evidence before any further production sending."
        )
    targets = []
    for idx, r in df.iterrows():
        if str(r.get("sendable", "")).strip().lower() not in ("yes", "true", "1"):
            continue
        current_status = str(r.get("status", "")).strip().lower()
        initial_status = str(r.get("initial_status", "")).strip().lower()
        if current_status in SUPPRESSED_STATUSES or initial_status in SUPPRESSED_STATUSES:
            continue
        tpl = str(r.get("template", "")).strip()
        if args.only and tpl != args.only:
            continue
        to = _valid_email(r.get("email"))
        subject = str(r.get("subject", "")).strip()
        body = str(r.get("body", "")).strip()
        if not to or not subject or not body:
            continue
        targets.append((idx, r, tpl, to))

    print(f"To send: {len(targets)} email(s)" + (f" (only {args.only})" if args.only else ""))
    print(f"Production checkpoint ledger: {production_ledger}")
    if args.redirect_to:
        print(f"⚠️  TEST redirect: all emails actually go to {args.redirect_to}")
        print(f"Test-only ledger: {event_ledger} (production status will not change)")
    if args.dry_run:
        if args.at:
            print(f"(scheduled --at {args.at}; dry-run does not wait or send)")
        print("[dry-run] Not sending. Would send:")
        preview_limit = args.limit if args.limit is not None else len(targets)
        for idx, r, tpl, to in targets[:preview_limit]:
            print(f"  row {idx} [{tpl}] -> {args.redirect_to or to} | {r.get('subject','')}")
        return

    if not targets:
        if applied:
            _atomic_write_csv(df, args.input)
        print("Nothing to send. All eligible rows are already accepted or suppressed.")
        return

    if not args.dry_run and args.limit is None:
        sys.exit("❌ A positive --limit is required for every SMTP send. Send in reviewed batches.")
    if not args.dry_run and not args.redirect_to and args.limit > 200:
        sys.exit("❌ Production batches are capped at 200 emails. Resume with the next batch.")
    if not args.dry_run and not args.redirect_to and args.sleep < 1:
        sys.exit("❌ Production sending requires --sleep of at least 1 second between emails.")

    if args.at:
        _wait_until(args.at)

    config_data = getattr(args, "config_data", None)
    cfg = normalize_config(config_data) if config_data is not None else load_config(args.config)
    if args.redirect_to:
        redirect_address = (_valid_email(args.redirect_to) or "").lower()
        allowed_test_addresses = {
            str(cfg.get("user", "")).strip().lower(),
            str(cfg.get("from_addr", "")).strip().lower(),
        }
        customer_addresses = {to.lower() for _, _, _, to in targets}
        if not redirect_address or redirect_address not in allowed_test_addresses:
            sys.exit(
                "❌ Test redirect must exactly match the SMTP account or From address."
            )
        if redirect_address in customer_addresses:
            sys.exit(
                "❌ Test redirect is also a customer recipient. Use a separate sender test inbox."
            )
    if not args.redirect_to:
        batch_id = str(df["batch_id"].iloc[0])
        _ensure_lifecycle_open(args.input, batch_id, production_ledger)
        _ensure_ledger_anchor(production_ledger, batch_id)
    server = connect_smtp(cfg)
    messages_on_connection = 0
    max_messages_per_connection = cfg["max_messages_per_connection"]
    unsub = _unsubscribe_header(cfg.get("unsubscribe"))
    sent = 0
    attempted = 0
    stop_after_current = False
    repeated_data_error = None
    repeated_data_error_count = 0
    try:
        for idx, r, tpl, to in targets:
            if attempted >= args.limit:
                break
            if messages_on_connection >= max_messages_per_connection:
                try:
                    server.quit()
                except Exception:
                    pass
                server = None
                server = connect_smtp(cfg)
                messages_on_connection = 0
            actual = args.redirect_to or to
            record_id = str(r.get("record_id", "")).strip() or _record_id(
                idx,
                to,
                tpl,
                str(r.get("subject", "")),
                str(r.get("body", "")),
                str(r.get("sendable", "")).strip().lower(),
                str(r.get("reason", "")),
                str(r.get("initial_status", "")).strip().lower(),
                str(r.get("activity_id", "")).strip(),
            )
            batch_id = str(r.get("batch_id", "")).strip()
            attempt_id = uuid.uuid4().hex
            attempt_time = datetime.now().astimezone().isoformat(timespec="seconds")
            msg = MIMEMultipart()
            msg["From"] = formataddr((str(Header(cfg.get("from_name", ""), "utf-8")), cfg["from_addr"]))
            msg["To"] = actual
            msg["Subject"] = Header(str(r.get("subject", "")), "utf-8")
            msg["Date"] = formatdate(localtime=True)
            activity_id = str(r.get("activity_id", "")).strip()
            msg["Message-ID"] = _send_message_id(
                record_id,
                cfg["from_addr"],
                activity_id,
                f"test:{attempt_id}" if args.redirect_to else "",
            )
            if activity_id:
                msg["X-MailPilot-Activity-ID"] = activity_id
            if unsub:
                msg["List-Unsubscribe"] = unsub
            msg.attach(MIMEText(str(r.get("body", "")), "plain", "utf-8"))
            _append_ledger(event_ledger, {
                "event": "send_attempt",
                "attempt_id": attempt_id,
                "row": int(idx),
                "batch_id": batch_id,
                "record_id": record_id,
                "activity_id": activity_id,
                "message_id": str(msg["Message-ID"]),
                "email": to,
                "actual_recipient": actual,
                "template": tpl,
                "status": "attempting",
                "send_time": attempt_time,
                "send_error": "",
            })
            attempted += 1
            try:
                serialized_message = msg.as_string()
                try:
                    refused = server.sendmail(
                        cfg["from_addr"], [actual], serialized_message
                    )
                    if refused:
                        raise smtplib.SMTPRecipientsRefused(refused)
                finally:
                    messages_on_connection += 1
            except Exception as e:
                now = datetime.now().astimezone().isoformat(timespec="seconds")
                status = "unknown" if _is_ambiguous_smtp_error(e) else "error"
                _append_ledger(event_ledger, {
                    "event": "send_result",
                    "attempt_id": attempt_id,
                    "row": int(idx),
                    "batch_id": batch_id,
                    "record_id": record_id,
                    "message_id": str(msg["Message-ID"]),
                    "email": to,
                    "actual_recipient": actual,
                    "template": tpl,
                    "status": status,
                    "send_time": now,
                    "send_error": str(e),
                })
                if not args.redirect_to:
                    df.at[idx, "status"] = status
                    df.at[idx, "send_time"] = now
                    df.at[idx, "send_error"] = str(e)
                print(f"  ✗ row {idx} [{tpl}] -> {actual}: {status}: {e}")
                if status == "unknown":
                    print("  ⚠️  SMTP outcome is uncertain. Stopping to prevent an automatic duplicate.")
                    stop_after_current = True
                elif _is_transient_smtp_error(e):
                    print(
                        "  ⚠️  SMTP returned a temporary 4xx refusal. Stopping this batch "
                        "to avoid worsening provider throttling; retry only after review."
                    )
                    stop_after_current = True
                elif isinstance(
                    e,
                    (
                        smtplib.SMTPSenderRefused,
                        smtplib.SMTPAuthenticationError,
                        smtplib.SMTPNotSupportedError,
                    ),
                ):
                    print(
                        "  ⚠️  SMTP rejected the sender/account capability. Stopping because "
                        "the same configuration is likely to fail every remaining row."
                    )
                    stop_after_current = True
                elif isinstance(e, smtplib.SMTPDataError):
                    error_key = (
                        tuple(_smtp_response_codes(e)),
                        str(getattr(e, "smtp_error", e)),
                    )
                    if error_key == repeated_data_error:
                        repeated_data_error_count += 1
                    else:
                        repeated_data_error = error_key
                        repeated_data_error_count = 1
                    if repeated_data_error_count >= 3:
                        print(
                            "  ⚠️  The same SMTP DATA rejection occurred 3 times in a row. "
                            "Stopping for configuration/content review."
                        )
                        stop_after_current = True
                else:
                    repeated_data_error = None
                    repeated_data_error_count = 0
            else:
                now = datetime.now().astimezone().isoformat(timespec="seconds")
                result_status = "test_sent" if args.redirect_to else "sent"
                try:
                    _append_ledger(event_ledger, {
                        "event": "test_result" if args.redirect_to else "send_result",
                        "attempt_id": attempt_id,
                        "row": int(idx),
                        "batch_id": batch_id,
                        "record_id": record_id,
                        "message_id": str(msg["Message-ID"]),
                        "email": to,
                        "actual_recipient": actual,
                        "template": tpl,
                        "status": result_status,
                        "send_time": now,
                        "send_error": "",
                    })
                except Exception:
                    # The preceding durable `attempting` event will become `unknown` on recovery.
                    if not args.redirect_to:
                        df.at[idx, "status"] = "unknown"
                        df.at[idx, "send_time"] = now
                        df.at[idx, "send_error"] = "SMTP accepted, but the result ledger could not be written"
                    stop_after_current = True
                    raise
                if not args.redirect_to:
                    df.at[idx, "status"] = "sent"
                    df.at[idx, "send_time"] = now
                    df.at[idx, "send_error"] = ""
                sent += 1
                repeated_data_error = None
                repeated_data_error_count = 0
                print(f"  ✓ row {idx} [{tpl}] -> {actual}")
            if stop_after_current:
                break
            if args.sleep and args.sleep > 0:
                time.sleep(args.sleep)
    except KeyboardInterrupt:
        print("\n⚠️  Interrupted. Completed sends are safe in the ledger; resume by running the same command again.")
        raise
    finally:
        if not args.redirect_to and (attempted or applied):
            _atomic_write_csv(df, args.input)
        try:
            if server is not None:
                server.quit()
        except Exception:
            pass
    label = "test message(s) accepted" if args.redirect_to else "production message(s) accepted"
    print(f"\nDone: {sent} {label}; {attempted} attempted. Ledger: {event_ledger}")


def cmd_send(args):
    if args.limit is not None and args.limit <= 0:
        sys.exit("❌ --limit must be a positive number. Zero never means unlimited.")
    if args.redirect_to and not _valid_email(args.redirect_to):
        sys.exit("❌ --redirect-to must be a valid test inbox address.")
    try:
        with BatchLock(f"{args.input}.lock"):
            return _cmd_send_locked(args)
    except BatchAlreadyLocked:
        sys.exit("❌ This batch is already being processed. Wait for it to finish before retrying.")


# ---------------- bounces ----------------

# From/Subject keywords that flag a bounce notification (English + Chinese).
_BOUNCE_HINTS = ["postmaster", "mailer-daemon", "returned", "delivery", "undeliver", "failure",
                 "退信", "无法投递", "投递失败"]


def _imap_since_date(value):
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return ""
    months = (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    )
    return f"{parsed.day:02d}-{months[parsed.month - 1]}-{parsed.year:04d}"


def _imap_fetch_bytes(fetch_data):
    chunks = []
    for item in fetch_data or []:
        if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
            chunks.append(item[1])
    return b"".join(chunks)


def _dsn_recipient(value):
    raw = str(value or "").strip()
    if ";" in raw:
        raw = raw.split(";", 1)[1].strip()
    raw = raw.strip("<>")
    return (_valid_email(raw) or "").lower()


def _message_ids_from_dsn(message):
    found = set()

    def add(value):
        for match in re.findall(r"<[^<>\s]+@[^<>\s]+>", str(value or "")):
            found.add(match.strip())

    for part in message.walk():
        for header in ("Original-Message-ID", "X-Original-Message-ID"):
            for value in part.get_all(header, []):
                add(value)
        if part.get_content_type() == "message/rfc822":
            payload = part.get_payload()
            if isinstance(payload, list):
                for nested in payload:
                    for value in nested.get_all("Message-ID", []):
                        add(value)
        if part.get_content_type() == "text/rfc822-headers":
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                for match in re.findall(rb"(?im)^Message-ID:\s*(<[^>]+>)", payload):
                    add(match.decode("ascii", "ignore"))
    return found


def _decoded_text_parts(message):
    texts = []
    for part in message.walk():
        if part.get_content_maintype() != "text":
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = part.get_content_charset() or "utf-8"
            try:
                texts.append(payload.decode(charset, "replace"))
            except LookupError:
                texts.append(payload.decode("utf-8", "replace"))
        elif isinstance(payload, str):
            texts.append(payload)
        else:
            value = part.get_payload()
            if isinstance(value, str):
                texts.append(value)
    return texts


def _parse_bounce_candidate(raw_message, uid=""):
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    message_ids = _message_ids_from_dsn(message)
    evidence = []
    non_permanent = []
    unverified = []
    saw_structured_dsn = False

    for part in message.walk():
        if part.get_content_type() != "message/delivery-status":
            continue
        saw_structured_dsn = True
        payload = part.get_payload()
        if not isinstance(payload, list):
            unverified.append(
                {
                    "uid": str(uid),
                    "email": "",
                    "reason": "message/delivery-status payload could not be parsed",
                    "expected_message_id": "",
                    "observed_message_ids": sorted(message_ids),
                }
            )
            continue
        for block in payload:
            recipient = _dsn_recipient(
                block.get("Final-Recipient") or block.get("Original-Recipient")
            )
            action = str(block.get("Action", "")).strip().lower()
            status_code = str(block.get("Status", "")).strip()
            diagnostic = str(block.get("Diagnostic-Code", "")).strip()
            if not recipient and not action:
                continue
            reason = re.sub(
                r"\s+",
                " ",
                diagnostic or f"Action: {action or 'missing'}; Status: {status_code or 'missing'}",
            )[:160]
            valid_failure_status = bool(re.fullmatch(r"[45]\.\d{1,3}\.\d{1,3}", status_code))
            if action == "failed" and recipient and valid_failure_status:
                # An enhanced 5.x status is permanent for this *message*, but many
                # classes (for example 5.7.1 policy/authentication failures) do not
                # prove that the recipient address is permanently bad. Only the
                # narrow destination-address failures below are safe to carry into
                # MailPilot's cross-activity suppression history.
                hard_address_statuses = {"5.1.1", "5.1.2", "5.1.3"}
                evidence.append(
                    {
                        "email": recipient,
                        "reason": reason,
                        "message_ids": sorted(message_ids),
                        "verified_failure": True,
                        "action": action,
                        "status": status_code,
                        "bounce_class": (
                            "hard" if status_code in hard_address_statuses else "soft"
                        ),
                    }
                )
            elif action in {"delayed", "delivered", "relayed", "expanded"}:
                non_permanent.append(
                    {
                        "uid": str(uid),
                        "email": recipient,
                        "action": action,
                        "status": status_code,
                        "reason": reason,
                        "message_ids": sorted(message_ids),
                    }
                )
            elif recipient:
                evidence.append(
                    {
                        "email": recipient,
                        "reason": reason,
                        "message_ids": sorted(message_ids),
                        "verified_failure": False,
                        "action": action,
                        "status": status_code,
                        "bounce_class": "unverified",
                    }
                )
            else:
                unverified.append(
                    {
                        "uid": str(uid),
                        "email": "",
                        "reason": reason,
                        "expected_message_id": "",
                        "observed_message_ids": sorted(message_ids),
                    }
                )

    if not saw_structured_dsn:
        body = "\n".join(_decoded_text_parts(message))
        recipient_match = re.search(
            r"(?:Final-Recipient|Original-Recipient):\s*rfc822;\s*([^\s<>]+@[^\s<>]+)",
            body,
            re.I,
        ) or re.search(r"(?:To|收件人)[\s:]+([^\s<>]+@[^\s<>]+\.[^\s<>]+)", body)
        recipient = _dsn_recipient(recipient_match.group(1)) if recipient_match else ""
        diagnostic = re.search(r"Diagnostic-Code:\s*(.+)", body, re.I) or re.search(
            r"\b(5\d\d[ \-]?\d?\.?\d?\.?\d?.{0,80})", body
        )
        reason = (
            re.sub(r"\s+", " ", diagnostic.group(1).strip())[:160]
            if diagnostic
            else "non-standard delivery notification requires manual review"
        )
        if recipient:
            evidence.append(
                {
                    "email": recipient,
                    "reason": reason,
                    "message_ids": sorted(message_ids),
                    "verified_failure": False,
                    "action": "",
                    "status": "",
                    "bounce_class": "unverified",
                }
            )
        else:
            unverified.append(
                {
                    "uid": str(uid),
                    "email": "",
                    "reason": reason,
                    "expected_message_id": "",
                    "observed_message_ids": sorted(message_ids),
                }
            )

    return {
        "evidence": evidence,
        "non_permanent": non_permanent,
        "unverified_candidates": unverified,
    }


def _extract_bounces(cfg, lookback, since_time=""):
    if str(cfg.get("imap_host", "")).strip().lower() in MICROSOFT_OAUTH_IMAP_HOSTS:
        raise LedgerIntegrityError(
            "Exchange Online no longer accepts password-based IMAP. MailPilot v0.2 "
            "does not implement IMAP OAuth or event import; use a compatible bounce mailbox "
            "before production or the audited archive cannot be completed."
        )
    ctx = _ssl_context()
    M = imaplib.IMAP4_SSL(
        cfg["imap_host"], cfg["imap_port"], ssl_context=ctx, timeout=30
    )
    try:
        M.login(cfg["imap_user"], cfg["imap_password"])
        select_status, _select_data = M.select("INBOX", readonly=True)
        if str(select_status).upper() != "OK":
            raise LedgerIntegrityError("IMAP could not select INBOX safely.")
        since_date = _imap_since_date(since_time)
        criteria = ("SINCE", since_date) if since_date else ("ALL",)
        status, search_data = M.uid("search", None, *criteria)
        if str(status).upper() != "OK":
            raise LedgerIntegrityError("IMAP could not search the selected mailbox safely.")
        ids = search_data[0].split() if search_data and search_data[0] else []
        recent = ids[-lookback:] if lookback else ids
        bounced = {}
        fetch_errors = []
        unverified_candidates = []
        non_permanent = []
        for uid in reversed(recent):
            header_status, header_data = M.uid(
                "fetch",
                uid,
                "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT CONTENT-TYPE)])",
            )
            header_bytes = _imap_fetch_bytes(header_data)
            if str(header_status).upper() != "OK" or not header_bytes:
                fetch_errors.append(
                    {"uid": uid.decode("ascii", "ignore"), "stage": "header"}
                )
                continue
            message = BytesParser(policy=policy.default).parsebytes(
                header_bytes, headersonly=True
            )
            blob = (
                _decode_hdr(message.get("From", "")).lower()
                + " "
                + _decode_hdr(message.get("Subject", "")).lower()
            )
            report_type = str(message.get_param("report-type") or "").lower()
            if report_type != "delivery-status" and not any(
                hint in blob for hint in _BOUNCE_HINTS
            ):
                continue
            body_status, body_data = M.uid("fetch", uid, "(BODY.PEEK[])")
            raw_message = _imap_fetch_bytes(body_data)
            if str(body_status).upper() != "OK" or not raw_message:
                fetch_errors.append(
                    {"uid": uid.decode("ascii", "ignore"), "stage": "message"}
                )
                continue
            try:
                parsed = _parse_bounce_candidate(
                    raw_message, uid.decode("ascii", "ignore")
                )
            except Exception as e:
                unverified_candidates.append(
                    {
                        "uid": uid.decode("ascii", "ignore"),
                        "email": "",
                        "reason": f"candidate DSN could not be parsed: {e}"[:160],
                        "expected_message_id": "",
                        "observed_message_ids": [],
                    }
                )
                continue
            unverified_candidates.extend(parsed["unverified_candidates"])
            non_permanent.extend(parsed["non_permanent"])
            for candidate in parsed["evidence"]:
                addr = candidate["email"]
                entry = bounced.setdefault(
                    addr,
                    {
                        "reasons": [],
                        "message_ids": set(),
                        "evidence": [],
                    },
                )
                entry["reasons"].append(candidate["reason"])
                entry["message_ids"].update(candidate["message_ids"])
                entry["evidence"].append(
                    {
                        "reason": candidate["reason"],
                        "message_ids": set(candidate["message_ids"]),
                        "verified_failure": candidate.get("verified_failure") is True,
                        "action": candidate.get("action", ""),
                        "status": candidate.get("status", ""),
                        "bounce_class": candidate.get("bounce_class", "unverified"),
                    }
                )
        uid_response = M.response("UIDVALIDITY")
        uidvalidity = ""
        if uid_response and len(uid_response) > 1 and uid_response[1]:
            raw_uidvalidity = uid_response[1][0]
            uidvalidity = (
                raw_uidvalidity.decode("ascii", "ignore")
                if isinstance(raw_uidvalidity, bytes)
                else str(raw_uidvalidity)
            )
        return {
            "bounces": bounced,
            "unverified_candidates": unverified_candidates,
            "non_permanent": non_permanent,
            "coverage": {
                "mailbox": "INBOX",
                "uidvalidity": uidvalidity,
                "query_since": since_date,
                "first_uid": ids[0].decode("ascii", "ignore") if ids else "",
                "last_uid": ids[-1].decode("ascii", "ignore") if ids else "",
                "matching_messages": len(ids),
                "scanned_messages": len(recent),
                "fetch_errors": fetch_errors,
                "complete": (not lookback or len(ids) <= lookback) and not fetch_errors,
            },
        }
    finally:
        try:
            M.logout()
        except Exception:
            pass


def _cmd_bounces_locked(args):
    df = pd.read_csv(args.input, dtype=object).fillna("")
    _validate_reviewed_batch(df)
    if args.apply and _lifecycle_phase(args.input) == "ARCHIVED":
        raise LedgerIntegrityError(
            "Archived batches are immutable; late bounce evidence requires a new audit workflow."
        )
    ledger_path = args.ledger or _default_ledger_path(args.input)
    trusted_production_ledger = os.path.exists(_ledger_anchor_path(ledger_path))
    applied = _apply_ledger(df, ledger_path)
    if applied:
        print(f"Recovered {applied} status update(s) from ledger: {ledger_path}")
    config_data = getattr(args, "config_data", None)
    cfg = normalize_config(config_data) if config_data is not None else load_config(args.config)
    ledger_events = list(_iter_ledger(ledger_path) or [])
    event_times = [
        str(event.get("send_time", ""))
        for event in ledger_events
        if str(event.get("send_time", "")).strip()
    ]
    since_time = min(event_times) if event_times else ""
    print(f"Connecting to IMAP {cfg['imap_host']}:{cfg['imap_port']} to read bounce notifications…")
    extracted = _extract_bounces(cfg, args.lookback, since_time)
    bounced = extracted["bounces"]
    coverage = extracted["coverage"]
    print(f"Found {len(bounced)} bounced address(es) in your mailbox.")
    if not coverage.get("complete"):
        print(
            "⚠️  Bounce scan coverage is incomplete. Increase the lookback before archiving."
        )
    for address, evidence in bounced.items():
        reasons = evidence.get("reasons", [])
        reason = reasons[0] if reasons else "delivery failed (bounced)"
        print(f"  ✗ {address} : {reason}")
    latest_ledger_status = {}
    for event in ledger_events:
        if event.get("event") == "batch_started":
            continue
        latest_ledger_status[int(event["row"])] = str(event.get("status", "")).strip()
    matched = []
    unverified = list(extracted.get("unverified_candidates", []))
    for idx, r in df.iterrows():
        to = str(r.get("email", "")).strip().lower()
        st = str(r.get("status", "")).strip().lower()
        if not to or to not in bounced or st in {"bounced", "soft_bounced"}:
            continue
        expected_message_id = _send_message_id(
            str(r.get("record_id", "")).strip(),
            cfg["from_addr"],
            str(r.get("activity_id", "")).strip(),
        )
        evidence = bounced[to]
        proven_sent = (
            trusted_production_ledger
            and latest_ledger_status.get(int(idx)) == "sent"
            and st == "sent"
        )
        atomic_evidence = evidence.get("evidence")
        if not isinstance(atomic_evidence, list):
            legacy = dict(evidence)
            reasons = evidence.get("reasons", [])
            legacy.setdefault(
                "reason", reasons[0] if reasons else "delivery evidence unverified"
            )
            atomic_evidence = [legacy]
        verified_match = next(
            (
                item
                for item in atomic_evidence
                if isinstance(item, dict)
                and item.get("verified_failure") is True
                and expected_message_id in set(item.get("message_ids", set()))
            ),
            None,
        )
        observed_message_ids = sorted(
            {
                message_id
                for item in atomic_evidence
                if isinstance(item, dict)
                for message_id in set(item.get("message_ids", set()))
            }
        )
        if proven_sent and verified_match:
            matched.append(
                {
                    "row": int(idx),
                    "batch_id": str(r.get("batch_id", "")),
                    "record_id": str(r.get("record_id", "")),
                    "activity_id": str(r.get("activity_id", "")),
                    "email": to,
                    "message_id": expected_message_id,
                    "reason": str(verified_match.get("reason", "delivery failed")),
                    "bounce_action": str(verified_match.get("action", "")),
                    "bounce_status": str(verified_match.get("status", "")),
                    "bounce_class": str(verified_match.get("bounce_class", "")),
                }
            )
        else:
            related = next(
                (
                    item
                    for item in atomic_evidence
                    if isinstance(item, dict)
                    and expected_message_id in set(item.get("message_ids", set()))
                ),
                atomic_evidence[0] if atomic_evidence else {},
            )
            unverified.append(
                {
                    "row": int(idx),
                    "email": to,
                    "reason": str(related.get("reason", "delivery evidence unverified")),
                    "expected_message_id": expected_message_id,
                    "observed_message_ids": observed_message_ids,
                    "bounce_action": str(related.get("action", "")),
                    "bounce_status": str(related.get("status", "")),
                    "bounce_class": str(related.get("bounce_class", "unverified")),
                }
            )
    terminal_message_ids = {
        message_id
        for item in extracted.get("non_permanent", [])
        if str(item.get("action", "")) in {"delivered", "relayed", "expanded"}
        for message_id in item.get("message_ids", [])
    }
    terminal_message_ids.update(candidate["message_id"] for candidate in matched)
    delayed_items = [
        item
        for item in extracted.get("non_permanent", [])
        if str(item.get("action", "")) == "delayed"
    ]
    for idx, r in df.iterrows():
        to = str(r.get("email", "")).strip().lower()
        if str(r.get("status", "")).strip().lower() != "sent":
            continue
        if latest_ledger_status.get(int(idx)) != "sent":
            continue
        expected_message_id = _send_message_id(
            str(r.get("record_id", "")).strip(),
            cfg["from_addr"],
            str(r.get("activity_id", "")).strip(),
        )
        if expected_message_id in terminal_message_ids:
            continue
        delayed_match = next(
            (
                item
                for item in delayed_items
                if to == str(item.get("email", "")).strip().lower()
                and expected_message_id in set(item.get("message_ids", []))
            ),
            None,
        )
        if delayed_match and not any(
            candidate.get("row") == int(idx)
            and candidate.get("bounce_action") == "delayed"
            for candidate in unverified
        ):
            unverified.append(
                {
                    "row": int(idx),
                    "uid": str(delayed_match.get("uid", "")),
                    "email": to,
                    "reason": str(delayed_match.get("reason", "delivery delayed")),
                    "expected_message_id": expected_message_id,
                    "observed_message_ids": sorted(
                        set(delayed_match.get("message_ids", []))
                    ),
                    "bounce_action": "delayed",
                    "bounce_status": str(delayed_match.get("status", "")),
                    "bounce_class": "pending",
                }
            )
    print(f"\nMatched against {args.input}: {len(matched)} row(s).")
    for candidate in matched:
        print(f"  row {candidate['row']} {candidate['email']} -> bounced")
    if unverified:
        print(
            f"Unverified address-only candidate(s): {len(unverified)}. "
            "These are NOT changed automatically because the current batch/Message-ID "
            "could not be proven."
        )
        for candidate in unverified:
            identity = candidate.get("email") or (
                f"IMAP UID {candidate.get('uid')}" if candidate.get("uid") else "unknown DSN"
            )
            row = candidate.get("row")
            prefix = f"row {row} " if row is not None else ""
            print(f"  {prefix}{identity} -> manual review required")
    result = {
        "found": len(bounced),
        "matched": len(matched),
        "unverified": len(unverified),
        "applied": 0,
        "candidates": matched,
        "unverified_candidates": unverified,
        "non_permanent": extracted.get("non_permanent", []),
        "from_addr": cfg["from_addr"],
        "coverage": coverage,
    }
    if not matched:
        print("(No rows to mark. Note: emails sent with --redirect-to can't be matched by recipient.)")
        return result
    if not args.apply:
        print("\n[preview] Not written back. Add --apply to mark these rows as bounced.")
        return result
    result["applied"] = _apply_bounce_candidates(
        df, args.input, ledger_path, matched, cfg["from_addr"]
    )
    print(f"\n✅ Marked {result['applied']} row(s) as bounced. Ledger: {ledger_path}. Wrote reasons to {args.input}")
    return result


def _apply_bounce_candidates(df, input_path, ledger_path, candidates, from_addr):
    _validate_reviewed_batch(df)
    batch_id = str(df["batch_id"].iloc[0])
    _ensure_ledger_anchor(ledger_path, batch_id)
    applied = 0
    for candidate in candidates:
        idx = int(candidate["row"])
        if idx not in df.index:
            raise LedgerIntegrityError(f"Bounce candidate row {idx} no longer exists.")
        to = str(df.at[idx, "email"]).strip().lower()
        record_id = str(df.at[idx, "record_id"]).strip()
        current_status = str(df.at[idx, "status"]).strip().lower()
        if (
            to != str(candidate.get("email", "")).strip().lower()
            or record_id != str(candidate.get("record_id", "")).strip()
            or batch_id != str(candidate.get("batch_id", "")).strip()
            or current_status != "sent"
        ):
            raise LedgerIntegrityError(
                f"Bounce candidate row {idx} changed after scan; rescan before applying."
            )
        activity_id = str(df.at[idx, "activity_id"]).strip() if "activity_id" in df else ""
        if activity_id != str(candidate.get("activity_id", "")).strip():
            raise LedgerIntegrityError(
                f"Bounce candidate row {idx} belongs to a different activity."
            )
        expected_message_id = _send_message_id(record_id, from_addr, activity_id)
        if not hmac.compare_digest(
            expected_message_id, str(candidate.get("message_id", ""))
        ):
            raise LedgerIntegrityError(
                f"Bounce candidate row {idx} is not bound to this MailPilot message."
            )
        reason = str(candidate.get("reason", "delivery failed (bounced)"))[:160]
        bounce_class = str(candidate.get("bounce_class", "hard")).strip().lower()
        result_status = "soft_bounced" if bounce_class == "soft" else "bounced"
        _append_ledger(ledger_path, {
            "event": "bounce_result",
            "row": int(idx),
            "batch_id": batch_id,
            "record_id": record_id,
            "email": to,
            "status": result_status,
            "send_error": reason,
            "send_time": str(df.at[idx, "send_time"]) if "send_time" in df.columns else "",
            "bounce_action": str(candidate.get("bounce_action", "")),
            "bounce_status": str(candidate.get("bounce_status", "")),
            "bounce_class": bounce_class,
        })
        df.at[idx, "status"] = result_status
        df.at[idx, "send_error"] = reason
        applied += 1
    _atomic_write_csv(df, input_path)
    return applied


def cmd_bounces(args):
    if args.lookback < 0:
        sys.exit("❌ --lookback must be zero (all matching messages) or a positive number.")
    try:
        with BatchLock(f"{args.input}.lock"):
            return _cmd_bounces_locked(args)
    except BatchAlreadyLocked:
        sys.exit("❌ This batch is currently sending. Bounce reconciliation must wait.")


# ---------------- doctor ----------------

def cmd_doctor(args):
    ok = True
    print("== Dependencies ==")
    for m in ("pandas", "yaml", "jinja2", "certifi", "openpyxl"):
        try:
            __import__(m)
            print(f"  ✓ {m}")
        except Exception as e:
            ok = False
            print(f"  ✗ {m} not installed — run: pip install {m if m != 'yaml' else 'pyyaml'} ({e})")
    print("== Config ==")
    if not args.config or not os.path.exists(args.config):
        print(f"  ✗ Config file not found: {args.config}")
        print("     → Copy config.example.yaml to config.yaml and fill in your app password (see references/)")
        print("\nResult: not ready to send. Fix the ✗ items above.")
        return
    try:
        cfg = load_config(args.config)
        print("  ✓ Config complete; password is not a placeholder")
    except SystemExit as e:
        print(f"  ✗ {str(e).lstrip('❌ ').strip()}")
        print("\nResult: not ready to send.")
        return
    smtp_host = str(cfg.get("host", "")).strip().lower()
    if smtp_host == "smtp.gmail.com":
        print(
            "  ⚠ MailPilot cannot see this account's messages sent elsewhere. Personal Gmail "
            "may stop after 500 messages/day; confirm the remaining account/tenant quota."
        )
    if smtp_host in MICROSOFT_PASSWORD_SMTP_HOSTS:
        ok = False
        print(
            "  ⚠ Microsoft 365 password SMTP AUTH remains available through Dec 2026, "
            "but tenant policy may disable it and Microsoft will default-disable it afterward. "
            "Plan OAuth or a supported relay."
        )
        print(
            "  ✗ The v0.2 web lifecycle cannot archive Exchange Online sends because IMAP OAuth "
            "is unsupported. Web production is blocked; redirected testing remains available."
        )
    print("== SMTP connection ==")
    try:
        s = connect_smtp(cfg)
        s.quit()
        print(f"  ✓ Logged in to {cfg['host']}:{cfg['port']} ({cfg['user']})")
    except Exception as e:
        ok = False
        print(f"  ✗ Connection/login failed: {e}")
        print("     → Check host/port, and that password is an app password (not your login password)")
    print("\nResult:", "✅ All set — ready to send." if ok else "⚠️  Fix the ✗ items above before sending.")


# ---------------- shared: config / SMTP ----------------

def normalize_config(cfg):
    if not isinstance(cfg, dict):
        sys.exit("❌ SMTP configuration must be a mapping.")
    smtp = cfg.get("smtp", {})
    imap = cfg.get("imap", {})
    smtp_host = smtp.get("host")
    # IMAP host defaults to the SMTP host with smtp->imap (override in config if your provider differs).
    imap_host = imap.get("host") or (smtp_host.replace("smtp", "imap", 1) if smtp_host else None)
    merged = {
        "host": smtp_host,
        "port": int(smtp.get("port", 465)),
        "use_ssl": smtp.get("use_ssl", True),
        "user": smtp.get("user"),
        "password": smtp.get("password"),
        "from_addr": cfg.get("from_addr") or smtp.get("user"),
        "from_name": cfg.get("from_name", ""),
        "max_messages_per_connection": int(
            smtp.get("max_messages_per_connection", 50)
        ),
        "imap_host": imap_host,
        "imap_port": int(imap.get("port", 993)),
        "imap_user": imap.get("user") or smtp.get("user"),
        "imap_password": imap.get("password") or smtp.get("password"),
        "unsubscribe": cfg.get("unsubscribe") or cfg.get("from_addr") or smtp.get("user"),
    }
    missing = [k for k in ("host", "user", "password", "from_addr") if not merged.get(k)]
    if missing:
        sys.exit(f"❌ Missing config: {missing}. 'password' must be an app password, not your login password.")
    pw = str(merged["password"])
    if pw in ("YOUR_APP_PASSWORD_HERE", "YOUR_AUTH_CODE_HERE") \
            or ("YOUR_" in pw.upper() and "HERE" in pw.upper()):
        sys.exit("❌ smtp.password is still the placeholder — set it to a real app password (not your login password). See references/email_provider_setup.md.")
    imap_pw = str(merged.get("imap_password", ""))
    if "YOUR_" in imap_pw.upper() and "HERE" in imap_pw.upper():
        sys.exit(
            "❌ imap.password is still the placeholder — set a real IMAP app password "
            "or remove the optional imap section until bounce reconciliation."
        )
    if not 1 <= merged["port"] <= 65535:
        sys.exit("❌ smtp.port must be between 1 and 65535.")
    if not 1 <= merged["imap_port"] <= 65535:
        sys.exit("❌ imap.port must be between 1 and 65535.")
    if not 1 <= merged["max_messages_per_connection"] <= 200:
        sys.exit("❌ smtp.max_messages_per_connection must be between 1 and 200.")
    normalized_from = _valid_email(merged["from_addr"])
    if not normalized_from:
        sys.exit("❌ from_addr must be a valid email address supported by MailPilot.")
    merged["from_addr"] = normalized_from
    from_name = str(merged.get("from_name", ""))
    if any(char in from_name for char in "\r\n") or len(from_name.encode("utf-8")) > 120:
        sys.exit("❌ from_name must be one line and at most 120 UTF-8 bytes.")
    unsubscribe = str(merged.get("unsubscribe", "")).strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in unsubscribe):
        sys.exit("❌ unsubscribe must be a single email address or http(s)/mailto URL.")
    if unsubscribe.startswith(("http://", "https://")):
        try:
            parsed_unsubscribe = urlsplit(unsubscribe)
            unsubscribe_valid = bool(parsed_unsubscribe.netloc)
        except ValueError:
            unsubscribe_valid = False
    elif unsubscribe.startswith("mailto:"):
        unsubscribe_valid = bool(_valid_email(unsubscribe[7:].split("?", 1)[0]))
    else:
        unsubscribe_valid = bool(_valid_email(unsubscribe))
    if unsubscribe and not unsubscribe_valid:
        sys.exit("❌ unsubscribe must be a valid email address or http(s)/mailto URL.")
    return merged


def load_config(path):
    if not path or not os.path.exists(path):
        sys.exit(f"❌ Config file not found: {path}\n   Copy config.example.yaml to config.yaml and fill in your app password.")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return normalize_config(cfg)


def _ssl_context():
    # Prefer certifi's CA bundle to avoid SSL verification failures on macOS system Python.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def connect_smtp(cfg):
    ctx = _ssl_context()
    if cfg["use_ssl"]:
        server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ctx, timeout=30)
    else:
        server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
        server.starttls(context=ctx)
    server.login(cfg["user"], cfg["password"])
    return server


def _unsubscribe_header(value):
    if not value:
        return None
    v = str(value).strip()
    if v.startswith(("http://", "https://", "mailto:")):
        return f"<{v}>"
    return f"<mailto:{v}?subject=unsubscribe>"


# ---------------- CLI ----------------

def main():
    p = argparse.ArgumentParser(
        prog="mailpilot-agent",
        description="Crash-safe mail merge for AI agents, with delivery tracking",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("preview", help="Render templates and show a preview for review (no sending)")
    pp.add_argument("file")
    pp.add_argument("--template", default="default", help="Template for simple mode (templates/<name>.txt)")
    pp.add_argument("--out", default="sendable.csv")
    pp.add_argument("--email-col", default=None, help="Override the email column name")
    pp.add_argument("--name-col", default=None, help="Override the name column name")
    pp.add_argument("--group-col", default=None, help="Grouped mode: the group column (auto-detected if omitted)")
    pp.add_argument(
        "--sent-col",
        default=None,
        help="Column that proves a row was already sent; use __none__ only after explicit review",
    )
    pp.add_argument(
        "--activity-id",
        default="",
        help="Campaign namespace; use a new value only for an explicitly new activity",
    )
    pp.set_defaults(func=cmd_preview)

    ps = sub.add_parser("send", help="Send in batches (idempotent; schedule / throttle / test-redirect)")
    ps.add_argument("--input", default="sendable.csv")
    ps.add_argument("--config", required=True)
    ps.add_argument("--only", default=None, help="Send only this template/group")
    ps.add_argument("--redirect-to", default=None, help="Testing: send everything to this address instead")
    ps.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Required positive attempt cap for SMTP sends; production maximum is 200",
    )
    ps.add_argument(
        "--sleep",
        type=float,
        default=1,
        help="Pause N seconds between emails (production minimum is 1)",
    )
    ps.add_argument("--at", default=None, help='Schedule the send, e.g. "2026-07-02 09:00"')
    ps.add_argument("--ledger", default=None, help="Append-only checkpoint log (default: <input>.sendlog.jsonl)")
    ps.add_argument("--dry-run", action="store_true")
    ps.set_defaults(func=cmd_send)

    pb = sub.add_parser("bounces", help="Read bounce notifications and mark undelivered rows")
    pb.add_argument("--input", default="sendable.csv")
    pb.add_argument("--config", required=True)
    pb.add_argument("--lookback", type=int, default=50, help="Scan the most recent N inbox messages")
    pb.add_argument("--ledger", default=None, help="Append-only checkpoint log (default: <input>.sendlog.jsonl)")
    pb.add_argument("--apply", action="store_true", help="Write results back (omit to only preview)")
    pb.set_defaults(func=cmd_bounces)

    pd_ = sub.add_parser("doctor", help="Self-check: dependencies / config / SMTP connectivity")
    pd_.add_argument("--config", default="config.yaml")
    pd_.set_defaults(func=cmd_doctor)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
