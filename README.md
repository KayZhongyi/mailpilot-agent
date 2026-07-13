# MailPilot Agent

**Crash-safe mail merge for teams and AI agents.**

MailPilot Agent helps teams and AI coding agents preview, send, resume, and reconcile personalized email batches from CSV/XLSX.

It is designed for careful workflows like pre-sales follow-ups, event notifications, customer updates, and local-first automation.

> Most email scripts just send. MailPilot previews first, skips unsafe rows, checkpoints every SMTP result, and resumes safely after interruption.

## Features

- **Reads Excel/CSV** and auto-detects email, name, group, and sent-status columns.
- **Simple and grouped modes**: send one template to everyone, or let a grouping column choose templates automatically (`group=confirmed` -> `templates/confirmed.txt`).
- **Preview before sending**: see per-group counts, rendered samples, and unsafe rows before any email goes out.
- **Fail-closed resume**: writes `attempting` before SMTP and the result afterward. An interrupted,
  uncertain attempt becomes `unknown` and is never retried automatically.
- **Safe testing**: use `--dry-run`, `--redirect-to`, and `--limit` before real sending.
- **Schedule and throttle**: send later with `--at`, or slow down batches with `--sleep`.
- **Bounce reconciliation**: scan bounce notifications through IMAP and mark undelivered rows.
- **Local-first**: no SaaS account, no telemetry, no database required.
- **Local web UI**: teammates can use a browser-based interface without any AI software.

## Local Web UI

For non-technical teammates, MailPilot includes a local browser UI.

On macOS, double-click:

```text
run_mailpilot_app.command
```

On Windows, double-click:

```text
run_mailpilot_app.bat
```

Or run manually:

```bash
pip install -r requirements.txt
python app.py
```

Then open the local URL shown in the terminal, usually:

```text
http://localhost:8501
```

The UI lets you:

- upload a CSV/XLSX list
- preview grouped or simple templates
- inspect blocked rows before sending
- inspect the local templates used by each group
- run dry-runs or redirected test sends
- require explicit confirmation before real sending

No AI software is required for the web UI. Everything runs locally on your computer.

## Safety Demo

Try the messy sample:

```bash
python scripts/mailer.py preview samples/messy_registrations.csv --out sendable.csv
```

The sample contains valid rows, empty emails, malformed emails, a missing template, and an already-sent row.

MailPilot Agent will show:

```text
Read 5 row(s) -> wrote sendable.csv

Will send (grouped by template, 1 total):
  [confirmed] 1 email(s)

Marked as already sent, will skip: 1 row(s)

Cannot send, needs your attention: 3 row(s) (will NOT be sent)
  - (no email): no valid email address
  - (no email): no valid email address
  - vip@example.com: template not found: templates/vip.txt
```

Nothing is sent during preview.

## Quickstart

Install dependencies for direct folder usage:

```bash
pip install pandas openpyxl jinja2 pyyaml certifi
```

Copy and edit the config:

```bash
cp config.example.yaml config.yaml
python scripts/mailer.py doctor --config config.yaml
```

Preview a grouped list:

```bash
python scripts/mailer.py preview samples/event_registrations.csv --out sendable.csv
```

Preview a simple one-template list:

```bash
python scripts/mailer.py preview samples/attendees.csv --template default --out sendable.csv
```

Test before sending:

```bash
python scripts/mailer.py send --input sendable.csv --config config.yaml --redirect-to you@example.com --limit 3
```

Redirected tests use a separate test ledger and never mark customer rows as sent.

Send for real in a reviewed batch (CLI maximum 200; local web UI maximum 100):

```bash
python scripts/mailer.py send --input sendable.csv --config config.yaml --limit 50 --sleep 2
```

Reconcile bounces:

```bash
python scripts/mailer.py bounces --input sendable.csv --config config.yaml --apply
```

## Installable CLI

You can also install it as a local CLI:

```bash
python -m pip install -e .
```

Then use:

```bash
mailpilot preview samples/event_registrations.csv --out sendable.csv
mailpilot send --input sendable.csv --config config.yaml --dry-run
```

Both commands are available:

```bash
mailpilot
mailpilot-agent
```

## How It Works

MailPilot uses a review-first workflow:

```text
CSV/XLSX -> preview -> confirm -> send -> checkpoint ledger -> bounce reconciliation
```

During sending, it sends one email at a time and appends the result to a JSONL ledger:

```text
sendable.csv.sendlog.jsonl
```

If the process stops halfway through, run the same command again. MailPilot replays the ledger first
and skips rows already accepted by SMTP. If a process stopped after an attempt began but before a
definite SMTP result was recorded, that row becomes `unknown` and requires manual reconciliation;
MailPilot will not risk sending it twice automatically.

The CSV is still updated for convenience, but it is written atomically instead of being rewritten after every single email.

## List Format

| column | required | notes |
| --- | --- | --- |
| `email` | yes | headers containing `email` / `邮箱` are auto-detected |
| `name` | optional | used for `{{ name }}` in templates |
| `group` | optional | enables grouped mode; value should match a template name |
| `sent` | optional | use explicit values such as `yes`/`no` or `已发送`/`未发送`; ambiguous or blank history is blocked |

MailPilot also auto-detects common grouping headers such as `type`, `category`, `template`, `status`, `stage`, `segment`, `组别`, `报名状态`, and `客户阶段`.

MailPilot does not guess business segmentation by itself. If your list is not grouped yet, ask your agent or spreadsheet rules to create a `group` / `template` column first, then preview before sending.

## Templates

Templates are plain text files with Jinja2 variables.

Example:

```text
Subject: You're confirmed
Hi {{ name }},

Your spot is confirmed.
```

Templates live in:

```text
templates/
```

Examples:

```text
templates/default.txt
templates/confirmed.txt
templates/waitlist.txt
```

In grouped mode, the value in the grouping column maps directly to a template file name:

```text
group=confirmed -> templates/confirmed.txt
group=waitlist  -> templates/waitlist.txt
group=vip       -> templates/vip.txt
```

If a matching template does not exist, MailPilot blocks that row during preview instead of sending it.

## Common Commands

Preview:

```bash
python scripts/mailer.py preview list.csv --out sendable.csv
```

Dry run:

```bash
python scripts/mailer.py send --input sendable.csv --config config.yaml --dry-run
```

Redirect all emails to yourself for testing:

```bash
python scripts/mailer.py send --input sendable.csv --config config.yaml --redirect-to you@example.com --limit 3
```

Send only one group:

```bash
python scripts/mailer.py send --input sendable.csv --config config.yaml --only confirmed --limit 50 --sleep 2
```

Throttle sending:

```bash
python scripts/mailer.py send --input sendable.csv --config config.yaml --limit 50 --sleep 2
```

Schedule sending:

```bash
python scripts/mailer.py send --input sendable.csv --config config.yaml --limit 50 --sleep 2 --at "2026-07-02 09:00"
```

Every SMTP send requires a positive `--limit`. Production sending also requires at least one second
between attempts. `0` never means unlimited.

## Configuration

Copy:

```bash
cp config.example.yaml config.yaml
```

Fill in SMTP settings:

```yaml
smtp:
  host: smtp.gmail.com
  port: 465
  use_ssl: true
  user: you@example.com
  password: YOUR_APP_PASSWORD_HERE

from_addr: you@example.com
from_name: Your Name
unsubscribe: you@example.com
```

Use an **app password**, not your normal email password. See:

```text
references/email_provider_setup.md
```

## Deliverability Notes

`send` marking a row as `sent` means the SMTP server accepted it. It does not guarantee inbox placement.

For larger or recurring batches, use a transactional provider such as Amazon SES, SendGrid, Mailgun, or Aliyun DirectMail with SPF/DKIM/DMARC configured.

MailPilot is designed for responsible workflows:

- send only to recipients who expect the message
- preview before sending
- throttle larger batches
- keep unsubscribed or bounced addresses out of future lists
- include a working unsubscribe contact

## Project Structure

```text
mailpilot-agent/
  mailpilot/              # installable Python package
  scripts/mailer.py       # direct folder entrypoint
  templates/              # editable templates
  samples/                # example CSV files
  tests/                  # pytest tests
  SKILL.md                # agent skill instructions
  config.example.yaml     # SMTP config template
```

## Development

Install dev dependencies:

```bash
python -m pip install -e ".[dev]"
```

Run checks:

```bash
python -m pytest
python -m ruff check .
```

## Roadmap

- HTML email templates
- Attachments
- SQLite checkpoint ledger option
- SendGrid / SES / Mailgun adapters
- Better DSN bounce parsing
- Richer preview reports

## License

MIT — see [LICENSE](LICENSE).
