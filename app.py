#!/usr/bin/env python3
"""Local web UI for MailPilot Agent.

This is a lightweight Flask app, not a SaaS service. It runs on localhost and
reuses the same preview/send engine as the CLI and Agent Skill.
"""

from argparse import Namespace
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
import hashlib
import io
import json
import os
import secrets
import shutil
import tempfile
import threading
import webbrowser

import pandas as pd
from flask import Flask, redirect, render_template_string, request, url_for

from mailpilot import mailer


ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT / "templates"
RUNS_DIR = ROOT / ".mailpilot_runs"
RUNS_DIR.mkdir(exist_ok=True)
mailer._fsync_parent(RUNS_DIR)
for stale_pattern in ("mailpilot_*/smtp-*.yaml", "mailpilot_*/config.yaml"):
    for stale_config in RUNS_DIR.glob(stale_pattern):
        stale_config.unlink(missing_ok=True)

app = Flask(__name__)
CAPTURE_LOCK = threading.Lock()

CSS = """
:root {
  --bg: #f6f7f9;
  --surface: #ffffff;
  --border: #e5e7eb;
  --text: #15171a;
  --muted: #697386;
  --accent: #4568dc;
  --accent-dark: #2447b8;
  --danger: #b42318;
  --success: #027a48;
  --warn: #8a5a00;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
  color: var(--text);
  background: linear-gradient(180deg, #fbfcfd 0%, var(--bg) 260px), var(--bg);
  letter-spacing: 0;
}
.shell {
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr);
  min-height: 100vh;
}
aside {
  border-right: 1px solid var(--border);
  background: rgba(255,255,255,0.72);
  padding: 28px 22px;
}
main {
  padding: 32px;
  max-width: 1180px;
  width: 100%;
}
.brand {
  font-size: 15px;
  font-weight: 760;
  margin-bottom: 4px;
}
.muted, .help, label span {
  color: var(--muted);
}
.help {
  font-size: 13px;
  line-height: 1.55;
}
.runbook {
  margin-top: 28px;
  padding-top: 24px;
  border-top: 1px solid var(--border);
}
.runbook div {
  color: #333946;
  font-size: 13px;
  padding: 7px 0;
}
.hero {
  border: 1px solid var(--border);
  background: rgba(255,255,255,0.88);
  border-radius: 9px;
  padding: 26px 28px;
  box-shadow: 0 18px 44px rgba(16, 24, 40, 0.06);
}
.eyebrow {
  color: var(--accent);
  font-size: 12px;
  font-weight: 760;
  text-transform: uppercase;
  margin-bottom: 8px;
}
h1 {
  font-size: 34px;
  line-height: 1.12;
  margin: 0;
}
h2 {
  font-size: 20px;
  margin: 0 0 16px;
}
h3 {
  font-size: 15px;
  margin: 0 0 12px;
}
.subtitle {
  max-width: 760px;
  color: var(--muted);
  font-size: 15px;
  line-height: 1.65;
  margin: 10px 0 0;
}
.pills {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 18px;
}
.pill {
  border: 1px solid var(--border);
  border-radius: 999px;
  background: #fafafa;
  color: #343946;
  font-size: 12px;
  padding: 6px 10px;
}
.grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px;
  margin-top: 22px;
}
.card {
  border: 1px solid var(--border);
  background: var(--surface);
  border-radius: 9px;
  padding: 20px;
  box-shadow: 0 8px 24px rgba(16, 24, 40, 0.04);
}
.full { grid-column: 1 / -1; }
form { margin: 0; }
label {
  display: block;
  font-size: 13px;
  font-weight: 650;
  margin: 12px 0 6px;
}
input, select, textarea {
  width: 100%;
  border: 1px solid #cfd5df;
  border-radius: 7px;
  background: #fff;
  color: var(--text);
  font: inherit;
  padding: 9px 10px;
}
textarea {
  min-height: 170px;
  resize: vertical;
}
input[type="checkbox"] {
  width: auto;
  margin-right: 8px;
}
.row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  margin-top: 16px;
}
button, .button {
  appearance: none;
  border: 1px solid #cfd5df;
  border-radius: 7px;
  background: #fff;
  color: var(--text);
  cursor: pointer;
  font-weight: 700;
  padding: 10px 13px;
  text-decoration: none;
  box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05);
}
button.primary {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}
button.primary:hover { background: var(--accent-dark); }
button.danger {
  background: #fff5f5;
  border-color: #f7b5b0;
  color: var(--danger);
}
.metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin: 16px 0;
}
.metric {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
  background: #fff;
}
.metric .label {
  color: var(--muted);
  font-size: 12px;
  font-weight: 650;
}
.metric .value {
  font-size: 26px;
  font-weight: 760;
  margin-top: 4px;
}
.notice {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
  background: #fbfbfc;
  color: #313846;
  font-size: 13px;
  line-height: 1.55;
}
.notice.success { border-color: #abefc6; background: #f6fef9; color: var(--success); }
.notice.error { border-color: #fecdca; background: #fff7f7; color: var(--danger); }
.notice.warn { border-color: #fedf89; background: #fffbeb; color: var(--warn); }
pre {
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #0d1117;
  color: #e6edf3;
  overflow: auto;
  padding: 14px;
  font-size: 12px;
  line-height: 1.5;
}
table {
  border-collapse: collapse;
  width: 100%;
  font-size: 13px;
}
th, td {
  border-bottom: 1px solid var(--border);
  padding: 9px 10px;
  text-align: left;
  vertical-align: top;
}
th {
  color: #343946;
  background: #fafafa;
  font-size: 12px;
}
.table-wrap {
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: auto;
  max-height: 420px;
}
.tabs {
  display: flex;
  gap: 8px;
  margin: 20px 0 0;
}
.tabs a {
  border: 1px solid var(--border);
  border-radius: 999px;
  color: #343946;
  font-size: 13px;
  font-weight: 700;
  padding: 8px 12px;
  text-decoration: none;
  background: #fff;
}
@media (max-width: 900px) {
  .shell { grid-template-columns: 1fr; }
  aside { border-right: 0; border-bottom: 1px solid var(--border); }
  main { padding: 20px; }
  .grid, .row, .metrics { grid-template-columns: 1fr; }
}
"""

PAGE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MailPilot Agent</title>
  <style>{{ css }}</style>
</head>
<body>
  <div class="shell">
    <aside>
      <div class="brand">MailPilot Agent</div>
      <div class="help">Local mail merge workbench. No AI software required.</div>
      <div class="runbook">
        <div>1. Upload recipient list</div>
        <div>2. Preview grouped templates</div>
        <div>3. Review blocked rows</div>
        <div>4. Test with redirect or dry-run</div>
        <div>5. Confirm real send</div>
      </div>
      <div class="runbook help">
        Runs only on this computer. Preview sends nothing.
      </div>
    </aside>
    <main>
      <section class="hero">
        <div class="eyebrow">Local Mail Merge Workbench</div>
        <h1>Preview-first sending for careful teams.</h1>
        <p class="subtitle">
          Upload a CSV/XLSX list, map groups to templates, block unsafe rows, and send with a
          crash-safe checkpoint ledger.
        </p>
        <div class="pills">
          <span class="pill">Preview-first</span>
          <span class="pill">Crash-safe ledger</span>
          <span class="pill">Grouped templates</span>
          <span class="pill">Local-only</span>
        </div>
      </section>

      <div class="tabs">
        <a href="#preview">Preview</a>
        <a href="#send">Send</a>
        <a href="#templates">Templates</a>
      </div>

      {% if message %}
        <div class="notice {{ message_kind }}">{{ message }}</div>
      {% endif %}

      <div class="grid">
        {% if runs %}
          <section class="card full">
            <h2>Resume an existing batch</h2>
            <p class="help">Always reopen the original batch after interruption. Do not upload the same list again.</p>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Batch</th><th>Updated</th><th>Ready</th><th>Accepted / suppressed</th><th>Blocked</th><th></th></tr></thead>
                <tbody>
                  {% for run in runs %}
                    <tr>
                      <td><code>{{ run.name }}</code></td>
                      <td>{{ run.updated }}</td>
                      <td>{{ run.metrics.ready }}</td>
                      <td>{{ run.metrics.skipped }}</td>
                      <td>{{ run.metrics.blocked }}</td>
                      <td><a class="button" href="{{ run.url }}">Resume safely</a></td>
                    </tr>
                  {% endfor %}
                </tbody>
              </table>
            </div>
          </section>
        {% endif %}

        <section class="card" id="preview">
          <h2>Upload and preview</h2>
          <p class="help">Nothing is sent here. MailPilot renders templates and flags unsafe rows.</p>
          <form action="/preview" method="post" enctype="multipart/form-data">
            <label>Recipient CSV/XLSX</label>
            <input type="file" name="recipients" accept=".csv,.xlsx,.xls" required>

            <div class="row">
              <div>
                <label>Mode</label>
                <select name="mode">
                  <option value="grouped">Grouped (choose the business decision column)</option>
                  <option value="simple">Simple one-template</option>
                </select>
              </div>
              <div>
                <label>Simple-mode template</label>
                <select name="template">
                  {% for tpl in templates %}
                    <option value="{{ tpl }}">{{ tpl }}</option>
                  {% endfor %}
                </select>
              </div>
            </div>

            <div class="row">
              <div>
                <label>Email column <span>optional</span></label>
                <input name="email_col" placeholder="email">
              </div>
              <div>
                <label>Name column <span>optional</span></label>
                <input name="name_col" placeholder="name">
              </div>
            </div>
            <label>Group/template column <span>required for grouped mode</span></label>
            <input name="group_col" placeholder="Example: 邮件类型 (MailPilot will not guess)">
            <label>Trusted already-sent column <span>required if anyone may have sent manually</span></label>
            <input name="sent_col" placeholder="Example: 已发送">
            <label><input type="checkbox" name="confirm_no_history"> I confirm nobody has previously sent any row in this list.</label>
            <div class="actions">
              <button class="primary" type="submit">Preview list</button>
            </div>
          </form>
        </section>

        <section class="card" id="send">
          <h2>Test or send</h2>
          {% if not sendable_path %}
            <div class="notice warn">Preview a list first.</div>
          {% else %}
            <p class="help">Using <code>{{ sendable_path }}</code></p>
            <form action="/send" method="post">
              <input type="hidden" name="sendable_path" value="{{ sendable_path }}">
              <input type="hidden" name="run_dir" value="{{ run_dir }}">
              <input type="hidden" name="action_token" value="{{ action_token }}">

              <div class="row">
                <div>
                  <label>SMTP host</label>
                  <input name="host" value="smtp.gmail.com" required>
                </div>
                <div>
                  <label>SMTP port</label>
                  <input name="port" value="465" required>
                </div>
              </div>
              <label><input type="checkbox" name="use_ssl" checked> Use SSL</label>
              <label>SMTP user / email</label>
              <input name="user" required>
              <label>App password</label>
              <input name="password" type="password" required>
              <div class="row">
                <div>
                  <label>From address</label>
                  <input name="from_addr">
                </div>
                <div>
                  <label>From name</label>
                  <input name="from_name">
                </div>
              </div>
              <label>Unsubscribe email/URL</label>
              <input name="unsubscribe">

              <div class="row">
                <div>
                  <label>Redirect all emails to test inbox</label>
                  <input name="redirect_to" placeholder="Must match SMTP user or From address">
                  <p class="help">For safety, this must be your sender test inbox and must not appear in the customer list.</p>
                </div>
                <div>
                  <label>Only group/template</label>
                  <input name="only" placeholder="confirmed">
                </div>
              </div>
              <div class="row">
                <div>
                  <label>Maximum attempts this batch (1–100)</label>
                  <input name="limit" type="number" min="1" max="100" value="3" required>
                </div>
                <div>
                  <label>Seconds between emails</label>
                  <input name="sleep" type="number" min="1" step="0.5" value="2" required>
                </div>
              </div>
              <label><input type="checkbox" name="confirm_real_send"> I reviewed the preview and understand real sending can email recipients.</label>
              <label><input type="checkbox" name="confirm_test_received"> I received the redirected test and checked every template.</label>
              <label>For real sending, type <code>发送 N 封</code> using the limit above</label>
              <input name="confirm_phrase" placeholder="Example: 发送 3 封">
              <div class="actions">
                <button type="submit" name="action" value="dry" formnovalidate>Dry-run (never connects to SMTP)</button>
                <button type="submit" name="action" value="test">Send redirected test only</button>
                <button class="danger" type="submit" name="action" value="send">Confirm real send</button>
              </div>
            </form>
          {% endif %}
        </section>

        {% if preview_output %}
          <section class="card full">
            <h2>Preview result</h2>
            <div class="metrics">
              <div class="metric"><div class="label">Ready to send</div><div class="value">{{ metrics.ready }}</div></div>
              <div class="metric"><div class="label">Skipped</div><div class="value">{{ metrics.skipped }}</div></div>
              <div class="metric"><div class="label">Blocked</div><div class="value">{{ metrics.blocked }}</div></div>
            </div>
            <pre>{{ preview_output }}</pre>
            {{ table_html|safe }}
          </section>
        {% endif %}

        {% if send_output %}
          <section class="card full">
            <h2>Send output</h2>
            <pre>{{ send_output }}</pre>
            {{ table_html|safe }}
          </section>
        {% endif %}

        <section class="card full" id="templates">
          <h2>Templates</h2>
          <p class="help">Grouped mode maps group values directly to template files.</p>
          <div class="grid">
            {% for tpl in template_files %}
              <div class="card">
                <h3>{{ tpl.name }}</h3>
                <pre>{{ tpl.text }}</pre>
              </div>
            {% endfor %}
          </div>
        </section>
      </div>
    </main>
  </div>
</body>
</html>
"""


def _capture(func, args):
    buf = io.StringIO()
    try:
        # redirect_stdout mutates process-global state, so captured commands must not overlap.
        with CAPTURE_LOCK:
            with redirect_stdout(buf):
                func(args)
        return True, buf.getvalue()
    except SystemExit as e:
        return False, f"{buf.getvalue()}\n{e}".strip()
    except Exception as e:
        return False, f"{buf.getvalue()}\n{type(e).__name__}: {e}".strip()


def _read_sendable(path):
    if not path or not Path(path).exists():
        return pd.DataFrame()
    return pd.read_csv(path, dtype=object).fillna("")


def _metric_counts(df):
    if df.empty:
        return {"ready": 0, "skipped": 0, "blocked": 0}
    sendable = df["sendable"].astype(str).str.lower().isin(["yes", "true", "1"])
    status = df["status"].astype(str).str.lower() if "status" in df else pd.Series("", index=df.index)
    initial_status = (
        df["initial_status"].astype(str).str.lower()
        if "initial_status" in df
        else pd.Series("", index=df.index)
    )
    suppressed = status.isin(mailer.SUPPRESSED_STATUSES) | initial_status.isin(
        mailer.SUPPRESSED_STATUSES
    )
    return {
        "ready": int((sendable & ~suppressed).sum()),
        "skipped": int((sendable & suppressed).sum()),
        "blocked": int((~sendable).sum()),
    }


def _pending_templates(df):
    sendable = df["sendable"].astype(str).str.lower().isin(["yes", "true", "1"])
    status = df["status"].astype(str).str.lower()
    initial_status = df["initial_status"].astype(str).str.lower()
    pending = df[
        sendable
        & ~status.isin(mailer.SUPPRESSED_STATUSES)
        & ~initial_status.isin(mailer.SUPPRESSED_STATUSES)
    ]
    return sorted(set(pending["template"].astype(str)))


def _table_html(df):
    if df.empty:
        return ""
    columns = [c for c in ["name", "email", "template", "subject", "status", "sendable", "reason", "send_time", "send_error"] if c in df.columns]
    safe = df[columns].head(200).copy()
    return f'<div class="table-wrap">{safe.to_html(index=False, escape=True)}</div>'


def _content_hash(df):
    columns = [
        c
        for c in (
            "record_id",
            "email",
            "template",
            "subject",
            "body",
            "initial_status",
            "sendable",
            "reason",
        )
        if c in df.columns
    ]
    payload = df[columns].fillna("").astype(str).to_csv(index=False, lineterminator="\n")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _batch_fingerprint(df):
    fields = (
        "email",
        "template",
        "subject",
        "body",
        "status",
        "initial_status",
        "sendable",
        "reason",
    )
    rows = []
    for _, row in df.iterrows():
        normalized = []
        for field in fields:
            value = str(row.get(field, "")).replace("\r\n", "\n").replace("\r", "\n").strip()
            normalized.append(value.lower() if field == "email" else value)
        rows.append(normalized)
    payload = json.dumps(sorted(rows), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _recipient_set_hash(df):
    recipients = sorted(str(value).strip().lower() for value in df["email"] if str(value).strip())
    payload = json.dumps(recipients, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _recipient_hashes(df):
    return sorted(
        {
            hashlib.sha256(str(value).strip().lower().encode("utf-8")).hexdigest()
            for value in df["email"]
            if str(value).strip()
        }
    )


def _template_names():
    names = sorted(p.stem for p in TEMPLATES_DIR.glob("*.txt"))
    return names or ["default"]


def _template_files():
    return [
        {"name": p.name, "text": p.read_text(encoding="utf-8")}
        for p in sorted(TEMPLATES_DIR.glob("*.txt"))
    ]


def _config_from_form(form):
    user = form.get("user", "").strip()
    from_addr = form.get("from_addr", "").strip() or user
    config = {
        "smtp": {
            "host": form.get("host", "").strip(),
            "port": int(form.get("port", "465") or 465),
            "use_ssl": bool(form.get("use_ssl")),
            "user": user,
            "password": form.get("password", ""),
        },
        "from_addr": from_addr,
        "from_name": form.get("from_name", "").strip(),
        "unsubscribe": form.get("unsubscribe", "").strip() or from_addr,
    }
    mailer.normalize_config(config)
    return config


def _smtp_identity(form):
    user = form.get("user", "").strip().lower()
    return {
        "host": form.get("host", "").strip().lower(),
        "port": int(form.get("port", "465") or 465),
        "use_ssl": bool(form.get("use_ssl")),
        "user": user,
        "from_addr": (form.get("from_addr", "").strip() or user).lower(),
        "from_name": form.get("from_name", "").strip(),
        "unsubscribe": form.get("unsubscribe", "").strip(),
    }


def _write_test_marker(run_dir, df, templates, form):
    payload = {
        "content_hash": _content_hash(df),
        "templates": sorted(templates),
        "smtp_identity": _smtp_identity(form),
        "tested_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    (Path(run_dir) / "redirect-test-passed.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _read_test_marker(run_dir):
    path = Path(run_dir) / "redirect-test-passed.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _safe_paths(sendable_path, run_dir):
    try:
        run = Path(run_dir).resolve(strict=True)
        sendable = Path(sendable_path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None, None
    runs_root = RUNS_DIR.resolve()
    if run.parent != runs_root or not run.name.startswith("mailpilot_"):
        return None, None
    if sendable.parent != run or sendable.name != "sendable.csv":
        return None, None
    return sendable, run


def _new_action_token(run_dir):
    token = secrets.token_urlsafe(32)
    (Path(run_dir) / "action.token").write_text(token, encoding="utf-8")
    return token


def _consume_action_token(run_dir, submitted):
    token_path = Path(run_dir) / "action.token"
    try:
        with mailer.BatchLock(str(Path(run_dir) / "action.lock")):
            expected = token_path.read_text(encoding="utf-8").strip()
            if not submitted or not secrets.compare_digest(expected, submitted):
                return None
            return _new_action_token(run_dir)
    except (OSError, mailer.BatchAlreadyLocked):
        return None


def _sha256_path(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest(run_dir, **values):
    path = Path(run_dir) / "manifest.json"
    current = {}
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}
    current.update(values)
    tmp = path.with_suffix(f".{secrets.token_hex(6)}.tmp")
    with tmp.open("x", encoding="utf-8") as f:
        f.write(json.dumps(current, ensure_ascii=False, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    mailer._fsync_parent(path)


def _write_hash_sidecar(run_dir, name, value):
    path = Path(run_dir) / name
    with path.open("x", encoding="ascii") as f:
        f.write(value)
        f.flush()
        os.fsync(f.fileno())
    mailer._fsync_parent(path)


def _write_recipient_index(run_dir, df):
    path = Path(run_dir) / "recipient-index.json"
    with path.open("x", encoding="utf-8") as f:
        json.dump(_recipient_hashes(df), f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    mailer._fsync_parent(path)


def _ensure_production_marker(run_dir, df):
    path = Path(run_dir) / "production.started"
    if path.exists():
        return
    payload = json.dumps(
        {
            "content_hash": _content_hash(df),
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    try:
        with path.open("x", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
    except FileExistsError:
        return
    mailer._fsync_parent(path)


def _remove_run(run_dir):
    run_dir = Path(run_dir)
    shutil.rmtree(run_dir)
    mailer._fsync_parent(run_dir)


def _find_source_batches(source_hash, exclude=None):
    """Find every run made from the same bytes, even if its manifest is damaged."""
    RUNS_DIR.mkdir(exist_ok=True)
    matches = []
    for run in RUNS_DIR.glob("mailpilot_*"):
        if exclude and run == exclude:
            continue
        candidate_hashes = []
        sidecar = run / "source.sha256"
        if sidecar.exists():
            try:
                candidate_hashes.append(sidecar.read_text(encoding="ascii").strip())
            except OSError:
                pass
        for source in (run / "recipients.csv", run / "recipients.xlsx"):
            if source.exists():
                try:
                    candidate_hashes.append(_sha256_path(source))
                except OSError:
                    pass
        manifest = run / "manifest.json"
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                candidate_hashes.append(str(data.get("source_hash", "")))
            except (OSError, json.JSONDecodeError):
                pass
        if any(value and secrets.compare_digest(value, source_hash) for value in candidate_hashes):
            matches.append(run)
    return matches


def _find_recipient_batches(recipient_set_hash, exclude=None):
    RUNS_DIR.mkdir(exist_ok=True)
    matches = []
    for run in RUNS_DIR.glob("mailpilot_*"):
        if exclude and run == exclude:
            continue
        candidate = ""
        sidecar = run / "recipients.sha256"
        if sidecar.exists():
            try:
                candidate = sidecar.read_text(encoding="ascii").strip()
            except OSError:
                candidate = ""
        if not candidate:
            try:
                manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
                candidate = str(manifest.get("recipient_set_hash", ""))
            except (OSError, json.JSONDecodeError):
                candidate = ""
        if not candidate and (run / "sendable.csv").exists():
            try:
                candidate = _recipient_set_hash(_read_sendable(run / "sendable.csv"))
            except (OSError, KeyError):
                candidate = ""
        if candidate and secrets.compare_digest(candidate, recipient_set_hash):
            matches.append(run)
    return matches


def _run_recipient_hashes(run):
    run = Path(run)
    index = run / "recipient-index.json"
    if index.exists():
        try:
            values = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise mailer.LedgerIntegrityError(
                f"Recipient identity index is damaged for {run.name}."
            ) from e
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise mailer.LedgerIntegrityError(
                f"Recipient identity index is invalid for {run.name}."
            )
        return set(values)
    sendable = run / "sendable.csv"
    if sendable.exists():
        try:
            return set(_recipient_hashes(_read_sendable(sendable)))
        except (OSError, KeyError) as e:
            raise mailer.LedgerIntegrityError(
                f"Recipient identities cannot be read for {run.name}."
            ) from e
    raise mailer.LedgerIntegrityError(
        f"A started batch ({run.name}) has no recoverable recipient identity index."
    )


def _find_overlapping_started_batch(df, exclude=None):
    current = set(_recipient_hashes(df))
    for run in RUNS_DIR.glob("mailpilot_*"):
        if exclude and run == exclude:
            continue
        if _production_started(run) and current.intersection(_run_recipient_hashes(run)):
            return run
    return None


def _production_started(run):
    sendable = Path(run) / "sendable.csv"
    ledger = Path(mailer._default_ledger_path(str(sendable)))
    anchor = Path(mailer._ledger_anchor_path(str(ledger)))
    if ledger.exists() or anchor.exists() or (Path(run) / "production.started").exists():
        return True
    try:
        manifest = json.loads((Path(run) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return manifest.get("production_started") is True


def _find_existing_preview(batch_fingerprint, exclude=None):
    RUNS_DIR.mkdir(exist_ok=True)
    for run in RUNS_DIR.glob("mailpilot_*"):
        if exclude and run == exclude:
            continue
        manifest = run / "manifest.json"
        if not manifest.exists():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            candidate = str(data.get("batch_fingerprint", ""))
        except (OSError, json.JSONDecodeError):
            continue
        if candidate and secrets.compare_digest(candidate, batch_fingerprint):
            return run
    return None


def _run_from_name(name):
    if not name.startswith("mailpilot_") or Path(name).name != name:
        return None
    try:
        run = (RUNS_DIR / name).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return run if run.parent == RUNS_DIR.resolve() else None


def _run_summaries():
    RUNS_DIR.mkdir(exist_ok=True)
    summaries = []
    for run in sorted(RUNS_DIR.glob("mailpilot_*"), key=lambda p: p.stat().st_mtime, reverse=True):
        sendable = run / "sendable.csv"
        if not sendable.exists():
            continue
        try:
            df = _read_sendable(sendable)
            mailer._apply_ledger(df, mailer._default_ledger_path(str(sendable)))
            metrics = _metric_counts(df)
        except Exception:
            metrics = {"ready": 0, "skipped": 0, "blocked": "review required"}
        summaries.append(
            {
                "name": run.name,
                "updated": datetime.fromtimestamp(run.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                "metrics": metrics,
                "url": f"/runs/{run.name}",
            }
        )
    return summaries


def _render(**kwargs):
    defaults = {
        "css": CSS,
        "templates": _template_names(),
        "template_files": _template_files(),
        "message": "",
        "message_kind": "",
        "preview_output": "",
        "send_output": "",
        "sendable_path": "",
        "run_dir": "",
        "action_token": "",
        "runs": _run_summaries(),
        "metrics": {"ready": 0, "skipped": 0, "blocked": 0},
        "table_html": "",
    }
    defaults.update(kwargs)
    return render_template_string(PAGE, **defaults)


@app.get("/")
def index():
    return _render()


@app.get("/runs/<run_name>")
def resume_run(run_name):
    run_dir = _run_from_name(run_name)
    if not run_dir:
        return redirect(url_for("index"))
    sendable_path = run_dir / "sendable.csv"
    if not sendable_path.exists():
        return _render(
            message=(
                "Recovery blocked: this batch manifest exists but sendable.csv is missing. "
                "Do not create a replacement batch until the original sending history is reconciled."
            ),
            message_kind="error",
        )
    try:
        df = _read_sendable(sendable_path)
        mailer._apply_ledger(df, mailer._default_ledger_path(str(sendable_path)))
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            raise mailer.LedgerIntegrityError("Batch manifest is missing.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not secrets.compare_digest(
            str(manifest.get("content_hash", "")), _content_hash(df)
        ):
            raise mailer.LedgerIntegrityError(
                "The reviewed recipient/content manifest no longer matches this batch."
            )
    except (OSError, json.JSONDecodeError, mailer.LedgerIntegrityError) as e:
        return _render(
            message=f"Recovery blocked for safety: {e}",
            message_kind="error",
            preview_output=(run_dir / "preview.txt").read_text(encoding="utf-8")
            if (run_dir / "preview.txt").exists()
            else "",
            metrics={"ready": 0, "skipped": 0, "blocked": len(df) if "df" in locals() else 0},
            table_html=_table_html(df) if "df" in locals() else "",
        )

    return _render(
        message="Existing batch reopened with its original checkpoint ledger. Already accepted and suppressed rows will not be sent again.",
        message_kind="success",
        preview_output=(run_dir / "preview.txt").read_text(encoding="utf-8")
        if (run_dir / "preview.txt").exists()
        else "",
        sendable_path=str(sendable_path),
        run_dir=str(run_dir),
        action_token=_new_action_token(run_dir),
        metrics=_metric_counts(df),
        table_html=_table_html(df),
    )


@app.post("/preview")
def preview():
    uploaded = request.files.get("recipients")
    if not uploaded or not uploaded.filename:
        return _render(message="Please upload a CSV/XLSX file.", message_kind="error")

    suffix = Path(uploaded.filename).suffix.lower() or ".csv"
    if suffix not in (".csv", ".xlsx"):
        return _render(message="Only CSV and XLSX files are accepted.", message_kind="error")
    mode = request.form.get("mode")
    group_col = request.form.get("group_col", "").strip() or None
    if mode == "simple":
        group_col = "__none__"
    elif not group_col:
        return _render(
            message="Grouped mode requires you to choose the exact business decision column. MailPilot will not guess it.",
            message_kind="error",
        )

    sent_col = request.form.get("sent_col", "").strip()
    if not sent_col and not request.form.get("confirm_no_history"):
        return _render(
            message="Choose a trusted already-sent column, or explicitly confirm there is no previous manual sending.",
            message_kind="error",
        )

    RUNS_DIR.mkdir(exist_ok=True)
    mailer._fsync_parent(RUNS_DIR)
    run_dir = Path(tempfile.mkdtemp(prefix="mailpilot_", dir=RUNS_DIR))
    mailer._fsync_parent(run_dir)
    source_path = run_dir / f"recipients{suffix}"
    uploaded.save(source_path)
    source_hash = _sha256_path(source_path)
    _write_hash_sidecar(run_dir, "source.sha256", source_hash)
    out_path = run_dir / "sendable.csv"

    args = Namespace(
        file=str(source_path),
        template=request.form.get("template") or "default",
        out=str(out_path),
        email_col=request.form.get("email_col") or None,
        name_col=request.form.get("name_col") or None,
        group_col=group_col,
        sent_col=sent_col or "__none__",
    )
    ok, output = _capture(mailer.cmd_preview, args)
    df = _read_sendable(out_path)
    if not ok:
        _remove_run(run_dir)
        return _render(
            message="Preview failed. The uploaded recipient copy was deleted.",
            message_kind="error",
            preview_output=output,
        )

    batch_fingerprint = _batch_fingerprint(df)
    recipient_set_hash = _recipient_set_hash(df)
    try:
        with mailer.BatchLock(str(RUNS_DIR / ".preview-dedupe.lock"), blocking=True):
            existing = _find_existing_preview(batch_fingerprint, exclude=run_dir)
            if existing:
                _remove_run(run_dir)
                return redirect(url_for("resume_run", run_name=existing.name))
            same_sources = _find_source_batches(source_hash, exclude=run_dir)
            equivalent_recipients = _find_recipient_batches(
                recipient_set_hash, exclude=run_dir
            )
            overlapping_started = _find_overlapping_started_batch(df, exclude=run_dir)
            if overlapping_started:
                _remove_run(run_dir)
                return redirect(
                    url_for("resume_run", run_name=overlapping_started.name)
                )
            for prior_run in set(same_sources + equivalent_recipients):
                if _production_started(prior_run):
                    _remove_run(run_dir)
                    return redirect(url_for("resume_run", run_name=prior_run.name))
            for same_source in same_sources:
                try:
                    old_manifest = json.loads(
                        (same_source / "manifest.json").read_text(encoding="utf-8")
                    )
                    old_fingerprint = str(old_manifest.get("batch_fingerprint", ""))
                except (OSError, json.JSONDecodeError):
                    old_fingerprint = ""
                if not old_fingerprint or not (same_source / "sendable.csv").exists():
                    _remove_run(run_dir)
                    return redirect(url_for("resume_run", run_name=same_source.name))
            for same_source in same_sources:
                _remove_run(same_source)
            _write_hash_sidecar(run_dir, "recipients.sha256", recipient_set_hash)
            _write_recipient_index(run_dir, df)
            (run_dir / "preview.txt").write_text(output, encoding="utf-8")
            _write_manifest(
                run_dir,
                original_filename=uploaded.filename,
                source_hash=source_hash,
                content_hash=_content_hash(df),
                batch_fingerprint=batch_fingerprint,
                recipient_set_hash=recipient_set_hash,
                created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            )
    except mailer.BatchAlreadyLocked:
        _remove_run(run_dir)
        return _render(
            message="Another preview is being finalized. Please retry after it finishes.",
            message_kind="error",
        )
    except (OSError, mailer.LedgerIntegrityError) as e:
        _remove_run(run_dir)
        return _render(
            message=f"Preview finalization was blocked for safety: {e}",
            message_kind="error",
        )
    action_token = _new_action_token(run_dir)
    return _render(
        message="Preview completed. Nothing has been sent.",
        message_kind="success",
        preview_output=output,
        sendable_path=str(out_path),
        run_dir=str(run_dir),
        action_token=action_token,
        metrics=_metric_counts(df),
        table_html=_table_html(df),
    )


@app.post("/send")
def send():
    sendable_path, run_dir = _safe_paths(
        request.form.get("sendable_path", ""), request.form.get("run_dir", "")
    )
    if not sendable_path or not run_dir:
        return redirect(url_for("index"))

    next_action_token = _consume_action_token(run_dir, request.form.get("action_token", ""))
    if not next_action_token:
        return _render(
            message="This action was already used or the batch is busy. Refresh the preview before trying again.",
            message_kind="error",
        )

    action = request.form.get("action")
    df = _read_sendable(sendable_path)
    metrics = _metric_counts(df)

    def render_error(message):
        return _render(
            message=message,
            message_kind="error",
            sendable_path=str(sendable_path),
            run_dir=str(run_dir),
            action_token=next_action_token,
            metrics=metrics,
            table_html=_table_html(df),
        )

    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return render_error("Batch manifest is missing or damaged. Real/test sending is blocked.")
    if not secrets.compare_digest(str(manifest.get("content_hash", "")), _content_hash(df)):
        return render_error("The reviewed recipients or message content changed. Run preview again.")

    if action not in ("dry", "test", "send"):
        return render_error("Unknown action. Nothing was sent.")
    try:
        limit = int(request.form.get("limit", ""))
    except (TypeError, ValueError):
        return render_error("Limit must be a whole number from 1 to 100.")
    if not 1 <= limit <= 100:
        return render_error("Limit must be between 1 and 100. Zero never means unlimited.")

    redirect_to = request.form.get("redirect_to", "").strip() or None
    if action == "test" and not redirect_to:
        return render_error("A redirected test inbox is required. Test mode can never use customer addresses.")
    test_templates = []
    if action == "test":
        normalized_redirect = (mailer._valid_email(redirect_to) or "").lower()
        allowed_test_inboxes = {
            str(request.form.get("user", "")).strip().lower(),
            str(request.form.get("from_addr", "")).strip().lower(),
        }
        customer_inboxes = set(df["email"].astype(str).str.strip().str.lower())
        if not normalized_redirect or normalized_redirect not in allowed_test_inboxes:
            return render_error(
                "For safety, the test inbox must exactly match the SMTP account or From address."
            )
        if normalized_redirect in customer_inboxes:
            return render_error(
                "The test inbox is also present in the customer list. Remove it from the list or use a separate sender test inbox."
            )
        test_templates = _pending_templates(df)
        if not test_templates:
            return render_error("There are no pending templates to test.")
        if len(test_templates) > 20:
            return render_error("More than 20 templates require review. Split or simplify this batch first.")
    if action == "send":
        if metrics["blocked"]:
            return render_error(
                f"Real sending is blocked until all {metrics['blocked']} unsafe rows are resolved."
            )
        if not request.form.get("confirm_real_send"):
            return render_error("Real sending requires explicit confirmation.")
        if not request.form.get("confirm_test_received"):
            return render_error("Confirm that you received and reviewed the redirected test first.")
        expected_phrase = f"发送 {limit} 封"
        if request.form.get("confirm_phrase", "").strip() != expected_phrase:
            return render_error(f"Type exactly “{expected_phrase}” to authorize this real batch.")
        marker = _read_test_marker(run_dir)
        pending_templates = set(_pending_templates(df))
        try:
            marker_matches = (
                marker
                and secrets.compare_digest(
                    str(marker.get("content_hash", "")), _content_hash(df)
                )
                and pending_templates.issubset(set(marker.get("templates", [])))
                and marker.get("smtp_identity") == _smtp_identity(request.form)
            )
        except (TypeError, ValueError):
            marker_matches = False
        if not marker_matches:
            return render_error("Run a successful redirected test for this exact preview before real sending.")

    try:
        sleep_seconds = float(request.form.get("sleep", "2") or 2)
    except ValueError:
        return render_error("Seconds between emails must be a number.")
    if action == "send" and sleep_seconds < 1:
        return render_error("Real sending requires at least 1 second between emails.")

    config_data = None
    if action != "dry":
        try:
            config_data = _config_from_form(request.form)
        except (OSError, TypeError, ValueError, SystemExit) as e:
            return render_error(f"SMTP settings are invalid: {e}")

    if action == "send":
        try:
            with mailer.BatchLock(
                str(RUNS_DIR / ".preview-dedupe.lock"), blocking=True
            ):
                if not run_dir.exists() or not sendable_path.exists():
                    return _render(
                        message=(
                            "This preview was replaced before sending began. Nothing was sent; "
                            "open the current batch and review it again."
                        ),
                        message_kind="error",
                    )
                locked_df = _read_sendable(sendable_path)
                mailer._validate_reviewed_batch(locked_df)
                locked_manifest = json.loads(
                    (run_dir / "manifest.json").read_text(encoding="utf-8")
                )
                if not secrets.compare_digest(
                    str(locked_manifest.get("content_hash", "")),
                    _content_hash(locked_df),
                ):
                    raise mailer.LedgerIntegrityError(
                        "The reviewed batch changed before production could start."
                    )
                overlapping = _find_overlapping_started_batch(
                    locked_df, exclude=run_dir
                )
                if overlapping:
                    return render_error(
                        "Real sending is blocked because recipients overlap with already-started "
                        f"batch {overlapping.name}. Reopen and reconcile that batch first."
                    )
                _ensure_production_marker(run_dir, locked_df)
                _write_manifest(
                    run_dir,
                    production_started=True,
                    production_started_at=datetime.now().astimezone().isoformat(
                        timespec="seconds"
                    ),
                )
                df = locked_df
        except (
            OSError,
            json.JSONDecodeError,
            mailer.BatchAlreadyLocked,
            mailer.LedgerIntegrityError,
        ) as e:
            return render_error(f"Production start was blocked for safety: {e}")

    try:
        test_ledger = Path(mailer._default_test_ledger_path(str(sendable_path)))
        before_test_events = list(mailer._iter_ledger(str(test_ledger)) or [])
        if action == "test":
            outputs = []
            ok = True
            for template_name in test_templates:
                args = Namespace(
                    input=str(sendable_path),
                    config="",
                    config_data=config_data,
                    only=template_name,
                    redirect_to=redirect_to,
                    limit=1,
                    sleep=0,
                    at=None,
                    dry_run=False,
                    ledger=None,
                )
                run_ok, run_output = _capture(mailer.cmd_send, args)
                outputs.append(f"=== Template: {template_name} ===\n{run_output}")
                ok = ok and run_ok
            output = "\n\n".join(outputs)
            after_test_events = list(mailer._iter_ledger(str(test_ledger)) or [])
            new_events = after_test_events[len(before_test_events):]
            accepted_templates = {
                str(event.get("template", ""))
                for event in new_events
                if event.get("status") == "test_sent"
            }
            if ok and accepted_templates == set(test_templates):
                _write_test_marker(run_dir, df, test_templates, request.form)
            else:
                ok = False
                missing = sorted(set(test_templates).difference(accepted_templates))
                output = f"{output}\nTemplates without an accepted redirected test: {missing}".strip()
        else:
            args = Namespace(
                input=str(sendable_path),
                config="",
                config_data=config_data,
                only=request.form.get("only") or None,
                redirect_to=None,
                limit=limit,
                sleep=sleep_seconds,
                at=None,
                dry_run=action == "dry",
                ledger=None,
            )
            ok, output = _capture(mailer.cmd_send, args)
    except mailer.LedgerIntegrityError as e:
        ok, output = False, f"Test ledger integrity error: {e}"

    df = _read_sendable(str(sendable_path))
    command_has_errors = "unknown" in output.lower() or "✗" in output
    succeeded = ok and not command_has_errors
    return _render(
        message="Command completed." if succeeded else "Command stopped or needs review.",
        message_kind="success" if succeeded else "error",
        send_output=output,
        sendable_path=str(sendable_path),
        run_dir=str(run_dir),
        action_token=next_action_token,
        metrics=_metric_counts(df),
        table_html=_table_html(df),
    )


def main():
    url = "http://127.0.0.1:8501"
    print(f"MailPilot Agent is running at {url}")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    app.run(host="127.0.0.1", port=8501, debug=False)


if __name__ == "__main__":
    main()
