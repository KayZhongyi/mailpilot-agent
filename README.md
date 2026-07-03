# MailPilot Agent

**Crash-safe mail merge for AI agents.**

MailPilot Agent helps AI coding agents preview, send, resume, and reconcile personalized email batches from CSV/XLSX.

It is designed for careful workflows like pre-sales follow-ups, event notifications, customer updates, and local-first automation.

> Most email scripts just send. MailPilot previews first, skips unsafe rows, checkpoints every SMTP result, and resumes safely after interruption.

## Features

- **Reads Excel/CSV** and auto-detects email, name, group, and sent-status columns.
- **Simple and grouped modes**: send one template to everyone, or choose templates per row with a `group` column.
- **Preview before sending**: see per-group counts, rendered samples, and unsafe rows before any email goes out.
- **Crash-safe resume**: writes an append-only JSONL checkpoint after every SMTP result.
- **Safe testing**: use `--dry-run`, `--redirect-to`, and `--limit` before real sending.
- **Schedule and throttle**: send later with `--at`, or slow down batches with `--sleep`.
- **Bounce reconciliation**: scan bounce notifications through IMAP and mark undelivered rows.
- **Local-first**: no SaaS account, no telemetry, no database required.

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

Send for real:

```bash
python scripts/mailer.py send --input sendable.csv --config config.yaml
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

If the process stops halfway through, run the same command again. MailPilot replays the ledger first and skips rows already accepted by SMTP.

The CSV is still updated for convenience, but it is written atomically instead of being rewritten after every single email.

## List Format

| column | required | notes |
| --- | --- | --- |
| `email` | yes | headers containing `email` / `邮箱` are auto-detected |
| `name` | optional | used for `{{ name }}` in templates |
| `group` | optional | enables grouped mode; value should match a template name |
| `sent` | optional | truthy rows such as `yes`, `1`, `是`, `已发送` are skipped |

Chinese headers are supported for common fields such as email, name, group, and sent status.

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

In grouped mode, a row with `group=confirmed` uses:

```text
templates/confirmed.txt
```

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
python scripts/mailer.py send --input sendable.csv --config config.yaml --only confirmed
```

Throttle sending:

```bash
python scripts/mailer.py send --input sendable.csv --config config.yaml --sleep 2
```

Schedule sending:

```bash
python scripts/mailer.py send --input sendable.csv --config config.yaml --at "2026-07-02 09:00"
```

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
