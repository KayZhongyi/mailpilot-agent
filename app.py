#!/usr/bin/env python3
"""Local web UI for MailPilot Agent.

This is a lightweight Flask app, not a SaaS service. It runs on localhost and
reuses the same preview/send engine as the CLI and Agent Skill.
"""

from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
import hashlib
import io
import os
import secrets
import tempfile
import webbrowser

import pandas as pd
import yaml
from flask import Flask, redirect, render_template_string, request, url_for

from mailpilot import mailer


ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT / "templates"
RUNS_DIR = ROOT / ".mailpilot_runs"
RUNS_DIR.mkdir(exist_ok=True)

app = Flask(__name__)

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
                  <input name="redirect_to" placeholder="you@example.com">
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
    suppressed = status.isin(mailer.SUPPRESSED_STATUSES)
    return {
        "ready": int((sendable & ~suppressed).sum()),
        "skipped": int((sendable & suppressed).sum()),
        "blocked": int((~sendable).sum()),
    }


def _table_html(df):
    if df.empty:
        return ""
    columns = [c for c in ["name", "email", "template", "subject", "status", "sendable", "reason", "send_time", "send_error"] if c in df.columns]
    safe = df[columns].head(200).copy()
    return f'<div class="table-wrap">{safe.to_html(index=False, escape=True)}</div>'


def _content_hash(df):
    columns = [
        c
        for c in ("record_id", "email", "template", "subject", "body", "sendable", "reason")
        if c in df.columns
    ]
    payload = df[columns].fillna("").astype(str).to_csv(index=False, lineterminator="\n")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _template_names():
    names = sorted(p.stem for p in TEMPLATES_DIR.glob("*.txt"))
    return names or ["default"]


def _template_files():
    return [
        {"name": p.name, "text": p.read_text(encoding="utf-8")}
        for p in sorted(TEMPLATES_DIR.glob("*.txt"))
    ]


def _write_config(run_dir, form):
    config_path = Path(run_dir) / f"smtp-{secrets.token_hex(8)}.yaml"
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
    config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.chmod(config_path, 0o600)
    return config_path


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
        "metrics": {"ready": 0, "skipped": 0, "blocked": 0},
        "table_html": "",
    }
    defaults.update(kwargs)
    return render_template_string(PAGE, **defaults)


@app.get("/")
def index():
    return _render()


@app.post("/preview")
def preview():
    uploaded = request.files.get("recipients")
    if not uploaded or not uploaded.filename:
        return _render(message="Please upload a CSV/XLSX file.", message_kind="error")

    suffix = Path(uploaded.filename).suffix.lower() or ".csv"
    if suffix not in (".csv", ".xlsx"):
        return _render(message="Only CSV and XLSX files are accepted.", message_kind="error")
    run_dir = Path(tempfile.mkdtemp(prefix="mailpilot_", dir=RUNS_DIR))
    source_path = run_dir / f"recipients{suffix}"
    uploaded.save(source_path)
    out_path = run_dir / "sendable.csv"

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
    action_token = _new_action_token(run_dir) if ok else ""
    return _render(
        message="Preview completed. Nothing has been sent." if ok else "Preview failed.",
        message_kind="success" if ok else "error",
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
        marker = run_dir / "redirect-test-passed.txt"
        if not marker.exists() or marker.read_text(encoding="utf-8").strip() != _content_hash(df):
            return render_error("Run a successful redirected test for this exact preview before real sending.")

    try:
        sleep_seconds = float(request.form.get("sleep", "2") or 2)
    except ValueError:
        return render_error("Seconds between emails must be a number.")
    if action == "send" and sleep_seconds < 1:
        return render_error("Real sending requires at least 1 second between emails.")

    config_path = None
    if action != "dry":
        try:
            config_path = _write_config(run_dir, request.form)
        except (OSError, TypeError, ValueError) as e:
            return render_error(f"SMTP settings are invalid: {e}")
    args = Namespace(
        input=str(sendable_path),
        config=str(config_path) if config_path else "",
        only=request.form.get("only") or None,
        redirect_to=redirect_to if action == "test" else None,
        limit=limit,
        sleep=sleep_seconds,
        at=None,
        dry_run=action == "dry",
        ledger=None,
    )
    try:
        test_ledger = Path(mailer._default_test_ledger_path(str(sendable_path)))
        before_test_events = list(mailer._iter_ledger(str(test_ledger)) or [])
        ok, output = _capture(mailer.cmd_send, args)
        if action == "test" and ok:
            after_test_events = list(mailer._iter_ledger(str(test_ledger)) or [])
            new_events = after_test_events[len(before_test_events):]
            if any(event.get("status") == "test_sent" for event in new_events):
                (run_dir / "redirect-test-passed.txt").write_text(
                    _content_hash(df), encoding="utf-8"
                )
            else:
                ok = False
                output = f"{output}\nNo redirected test was accepted by SMTP.".strip()
    finally:
        if config_path:
            config_path.unlink(missing_ok=True)

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
