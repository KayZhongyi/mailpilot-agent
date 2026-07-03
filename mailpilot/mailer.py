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
import email
import imaplib
import json
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header, decode_header
from email.utils import formataddr

import pandas as pd
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
PACKAGE_TEMPLATES_DIR = os.path.join(HERE, "templates")
PROJECT_TEMPLATES_DIR = os.path.join(PROJECT_ROOT, "templates")
TEMPLATES_DIR = PROJECT_TEMPLATES_DIR if os.path.isdir(PROJECT_TEMPLATES_DIR) else PACKAGE_TEMPLATES_DIR

# Column-name aliases for auto-detection. Chinese entries are intentional — they let the tool read
# spreadsheets with Chinese headers out of the box.
EMAIL_ALIASES = ["email", "e-mail", "mail", "邮箱", "邮件地址", "电子邮箱", "收件邮箱", "收件人邮箱", "收件人"]
NAME_ALIASES = ["name", "customer", "姓名", "名称", "客户", "客户名称", "经销商", "公司", "公司名称", "联系人"]
GROUP_ALIASES = ["group", "type", "category", "template", "组别", "分组", "类别", "类型", "组", "邮件类型", "模板"]
SENT_ALIASES = ["sent", "sent_flag", "已发", "已发送", "是否已发", "是否已发送", "是否发送", "发送状态", "已发状态"]


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
        na = _norm(a)
        for nc, orig in norm.items():
            if nc == na or na in nc:
                return orig
    return None


def _valid_email(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    if "@" in s and "." in s.split("@")[-1] and " " not in s:
        return s
    return None


def _truthy_sent(v):
    # Recognizes both English and Chinese "already sent" markers.
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return False
    s = _norm(v)
    return s in ("y", "yes", "true", "1", "sent", "ok", "是", "已", "已发", "已发送") or "已发" in s


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
    if path.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(path, dtype=object)
    return pd.read_csv(path, dtype=object)


def _default_ledger_path(input_path):
    return f"{input_path}.sendlog.jsonl"


def _atomic_write_csv(df, path):
    tmp = f"{path}.tmp"
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)


def _append_ledger(path, event):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _iter_ledger(path):
    if not path or not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                print(f"⚠️  Ignoring malformed ledger line {line_no} in {path}: {e}")


def _apply_ledger(df, path):
    applied = 0
    for ev in _iter_ledger(path) or []:
        try:
            idx = int(ev["row"])
        except Exception:
            continue
        if idx not in df.index:
            continue
        status = str(ev.get("status", "")).strip()
        if status not in ("sent", "error", "bounced"):
            continue
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
    email_col = args.email_col or _find_col(df, EMAIL_ALIASES)
    name_col = args.name_col or _find_col(df, NAME_ALIASES)
    group_col = args.group_col or _find_col(df, GROUP_ALIASES)
    sent_col = _find_col(df, SENT_ALIASES)
    if not email_col:
        sys.exit("❌ No email column found. Use --email-col to specify it, or make sure the file has an 'email' column.")

    env = get_env()
    rows = []
    per = {}
    unsendable = []
    present_cnt = 0
    for _, r in df.iterrows():
        email_v = _valid_email(r.get(email_col))
        name = ""
        if name_col:
            nv = r.get(name_col)
            name = "" if (nv is None or (isinstance(nv, float) and pd.isna(nv))) else str(nv).strip()
        tpl = args.template
        if group_col:
            gv = r.get(group_col)
            gv = "" if (gv is None or (isinstance(gv, float) and pd.isna(gv))) else str(gv).strip()
            if gv:
                tpl = gv

        subject = body = ""
        sendable = True
        reason = ""
        if not email_v:
            sendable, reason = False, "no valid email address"
        elif not _template_exists(tpl):
            sendable, reason = False, f"template not found: templates/{tpl}.txt"
        else:
            try:
                subject, body = render(env, tpl, {"name": name or "there"})
            except Exception as e:
                sendable, reason = False, f"template render failed: {e}"

        status = ""
        if sent_col and _truthy_sent(r.get(sent_col)):
            status = "sent"
            present_cnt += 1

        rows.append({
            "name": name, "email": email_v or "", "template": tpl,
            "subject": subject, "body": body, "status": status,
            "sendable": "yes" if sendable else "no", "reason": reason,
            "send_time": "", "send_error": "",
        })
        if sendable and status != "sent":
            per.setdefault(tpl, []).append(len(rows) - 1)
        if not sendable:
            unsendable.append((email_v or "(no email)", reason))

    out = pd.DataFrame(rows)
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


def cmd_send(args):
    df = pd.read_csv(args.input, dtype=object).fillna("")
    ledger_path = args.ledger or _default_ledger_path(args.input)
    applied = _apply_ledger(df, ledger_path)
    if applied:
        print(f"Recovered {applied} status update(s) from ledger: {ledger_path}")
    targets = []
    for idx, r in df.iterrows():
        if str(r.get("sendable", "")).strip().lower() not in ("yes", "true", "1"):
            continue
        if str(r.get("status", "")).strip().lower() == "sent":
            continue
        tpl = str(r.get("template", "")).strip()
        if args.only and tpl != args.only:
            continue
        to = str(r.get("email", "")).strip()
        if not to:
            continue
        targets.append((idx, r, tpl, to))

    print(f"To send: {len(targets)} email(s)" + (f" (only {args.only})" if args.only else ""))
    print(f"Checkpoint ledger: {ledger_path}")
    if args.redirect_to:
        print(f"⚠️  TEST redirect: all emails actually go to {args.redirect_to}")
    if args.dry_run:
        if args.at:
            print(f"(scheduled --at {args.at}; dry-run does not wait or send)")
        print("[dry-run] Not sending. Would send:")
        for idx, r, tpl, to in targets[: args.limit or len(targets)]:
            print(f"  row {idx} [{tpl}] -> {args.redirect_to or to} | {r.get('subject','')}")
        return

    if args.at:
        _wait_until(args.at)

    cfg = load_config(args.config)
    server = connect_smtp(cfg)
    unsub = _unsubscribe_header(cfg.get("unsubscribe"))
    sent = 0
    attempted = 0
    try:
        for idx, r, tpl, to in targets:
            if args.limit and sent >= args.limit:
                break
            actual = args.redirect_to or to
            msg = MIMEMultipart()
            msg["From"] = formataddr((str(Header(cfg.get("from_name", ""), "utf-8")), cfg["from_addr"]))
            msg["To"] = actual
            msg["Subject"] = Header(str(r.get("subject", "")), "utf-8")
            if unsub:
                msg["List-Unsubscribe"] = unsub
            msg.attach(MIMEText(str(r.get("body", "")), "plain", "utf-8"))
            try:
                server.sendmail(cfg["from_addr"], [actual], msg.as_string())
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                _append_ledger(ledger_path, {
                    "event": "send_result",
                    "row": int(idx),
                    "email": to,
                    "actual_recipient": actual,
                    "template": tpl,
                    "status": "sent",
                    "send_time": now,
                    "send_error": "",
                })
                df.at[idx, "status"] = "sent"
                df.at[idx, "send_time"] = now
                df.at[idx, "send_error"] = ""
                sent += 1
                attempted += 1
                print(f"  ✓ row {idx} [{tpl}] -> {actual}")
            except Exception as e:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                _append_ledger(ledger_path, {
                    "event": "send_result",
                    "row": int(idx),
                    "email": to,
                    "actual_recipient": actual,
                    "template": tpl,
                    "status": "error",
                    "send_time": now,
                    "send_error": str(e),
                })
                df.at[idx, "status"] = "error"
                df.at[idx, "send_time"] = now
                df.at[idx, "send_error"] = str(e)
                attempted += 1
                print(f"  ✗ row {idx} [{tpl}] -> {actual}: {e}")
            if args.sleep and args.sleep > 0:
                time.sleep(args.sleep)
    except KeyboardInterrupt:
        print("\n⚠️  Interrupted. Completed sends are safe in the ledger; resume by running the same command again.")
        raise
    finally:
        if attempted or applied:
            _atomic_write_csv(df, args.input)
        try:
            server.quit()
        except Exception:
            pass
    print(f"\nDone: {sent} sent. Ledger: {ledger_path}. Status written back to {args.input}")


# ---------------- bounces ----------------

# From/Subject keywords that flag a bounce notification (English + Chinese).
_BOUNCE_HINTS = ["postmaster", "mailer-daemon", "returned", "delivery", "undeliver", "failure",
                 "退信", "无法投递", "投递失败"]


def _extract_bounces(cfg, lookback):
    ctx = _ssl_context()
    M = imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"], ssl_context=ctx)
    M.login(cfg["user"], cfg["password"])
    M.select("INBOX")
    ids = M.search(None, "ALL")[1][0].split()
    recent = ids[-lookback:] if lookback else ids
    bounced = {}
    for i in reversed(recent):
        hdr = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")[1][0][1].decode("utf-8", "ignore")
        m = email.message_from_string(hdr)
        blob = _decode_hdr(m.get("From", "")).lower() + " " + _decode_hdr(m.get("Subject", "")).lower()
        if not any(h in blob for h in _BOUNCE_HINTS):
            continue
        body = M.fetch(i, "(BODY.PEEK[TEXT])")[1][0][1].decode("utf-8", "ignore")
        mrec = re.search(r"Final-Recipient:\s*rfc822;\s*([^\s<>]+@[^\s<>]+)", body, re.I) \
            or re.search(r"(?:To|收件人)[\s:]+([^\s<>]+@[^\s<>]+\.[^\s<>]+)", body)
        if not mrec:
            continue
        addr = mrec.group(1).strip().strip(">").lower()
        mr = re.search(r"Diagnostic-Code:\s*(.+)", body) or re.search(r"(5\d\d[ \-]?\d?\.?\d?\.?\d?.{0,80})", body)
        reason = re.sub(r"\s+", " ", mr.group(1).strip())[:160] if mr else "delivery failed (bounced)"
        bounced.setdefault(addr, reason)
    M.logout()
    return bounced


def cmd_bounces(args):
    cfg = load_config(args.config)
    df = pd.read_csv(args.input, dtype=object).fillna("")
    ledger_path = args.ledger or _default_ledger_path(args.input)
    applied = _apply_ledger(df, ledger_path)
    if applied:
        print(f"Recovered {applied} status update(s) from ledger: {ledger_path}")
    print(f"Connecting to IMAP {cfg['imap_host']}:{cfg['imap_port']} to read bounce notifications…")
    bounced = _extract_bounces(cfg, args.lookback)
    print(f"Found {len(bounced)} bounced address(es) in your mailbox.")
    for a, rn in bounced.items():
        print(f"  ✗ {a} : {rn}")
    matched = []
    for idx, r in df.iterrows():
        to = str(r.get("email", "")).strip().lower()
        st = str(r.get("status", "")).strip().lower()
        if to and to in bounced and st in ("sent", "error", ""):
            matched.append((idx, to))
    print(f"\nMatched against {args.input}: {len(matched)} row(s).")
    for idx, to in matched:
        print(f"  row {idx} {to} -> bounced")
    if not matched:
        print("(No rows to mark. Note: emails sent with --redirect-to can't be matched by recipient.)")
        return
    if not args.apply:
        print("\n[preview] Not written back. Add --apply to mark these rows as bounced.")
        return
    for idx, to in matched:
        _append_ledger(ledger_path, {
            "event": "bounce_result",
            "row": int(idx),
            "email": to,
            "status": "bounced",
            "send_error": bounced[to],
            "send_time": str(df.at[idx, "send_time"]) if "send_time" in df.columns else "",
        })
        df.at[idx, "status"] = "bounced"
        df.at[idx, "send_error"] = bounced[to]
    _atomic_write_csv(df, args.input)
    print(f"\n✅ Marked {len(matched)} row(s) as bounced. Ledger: {ledger_path}. Wrote reasons to {args.input}")


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

def load_config(path):
    if not path or not os.path.exists(path):
        sys.exit(f"❌ Config file not found: {path}\n   Copy config.example.yaml to config.yaml and fill in your app password.")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
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
        "imap_host": imap_host,
        "imap_port": int(imap.get("port", 993)),
        "unsubscribe": cfg.get("unsubscribe") or cfg.get("from_addr") or smtp.get("user"),
    }
    missing = [k for k in ("host", "user", "password", "from_addr") if not merged.get(k)]
    if missing:
        sys.exit(f"❌ Missing config: {missing}. 'password' must be an app password, not your login password.")
    pw = str(merged["password"])
    if pw in ("YOUR_APP_PASSWORD_HERE", "YOUR_AUTH_CODE_HERE") \
            or ("YOUR_" in pw.upper() and "HERE" in pw.upper()):
        sys.exit("❌ smtp.password is still the placeholder — set it to a real app password (not your login password). See references/email_provider_setup.md.")
    return merged


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
    pp.set_defaults(func=cmd_preview)

    ps = sub.add_parser("send", help="Send in batches (idempotent; schedule / throttle / test-redirect)")
    ps.add_argument("--input", default="sendable.csv")
    ps.add_argument("--config", required=True)
    ps.add_argument("--only", default=None, help="Send only this template/group")
    ps.add_argument("--redirect-to", default=None, help="Testing: send everything to this address instead")
    ps.add_argument("--limit", type=int, default=None, help="Send at most N emails")
    ps.add_argument("--sleep", type=float, default=0, help="Pause N seconds between emails (throttle)")
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
