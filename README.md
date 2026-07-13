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
- **Safe testing**: redirected tests can only go to the SMTP/From address, never to an address in the customer list.
- **Duplicate-batch guard in the local web UI**: once production starts, an overlapping upload reopens the original batch instead of creating a second send ledger.
- **Schedule and throttle**: send later with `--at`, or slow down batches with `--sleep`.
- **Bounce reconciliation**: bind IMAP to the recorded production sender, scan from the activity start date, record UID coverage, and automatically apply only DSNs with the exact activity Message-ID.
- **Explicit web activity lifecycle**: production open → resolve uncertainty → close outbound → scan/apply bounces → archive.
- **Auditable web campaigns**: overlapping recipients stay blocked until the old activity is archived and a one-time new-activity authorization is created.
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
- read its headers locally and suggest email/name/group/history column mappings for confirmation
- name the activity so operators can distinguish campaigns
- reopen an interrupted batch with its original checkpoint ledger
- preview grouped or simple templates
- inspect blocked rows before sending
- inspect the local templates used by each group
- run dry-runs or redirected test sends
- require explicit confirmation before real sending
- resolve `unknown` / definite failures with evidence notes
- close outbound permanently, scan IMAP bounces, download full status/audit reports, and archive the activity

No AI software is required for the web UI. Everything runs locally on your computer.

After a restart, use **Resume an existing batch**. Once production has started, uploading the same
source—or any new list that overlaps its recipients—opens the original batch instead of creating a
second send ledger. Reconcile the old batch before beginning a separate campaign. Never delete or rename the hidden run directory, ledger, or
`.started` marker while a batch is active.

For a 1000+ recipient job, do not authorize all rows at once. Verify the trusted historical sent
column, preview the whole file, test every template to the sender inbox, then send in web batches of
at most 100 (CLI: 200) with at least one second between attempts. Reopen the same batch after every
interruption.

If any attempt becomes `unknown`, MailPilot pauses the entire production batch. Check the provider
logs, then record either “confirmed accepted” or “permanently do not retry” with an evidence note.
Do not guess. After the last outbound decision, close outbound, scan bounces, apply only DSNs whose
Message-ID matches this activity, acknowledge any unverified candidates, and archive the activity.
The scan report shows every candidate and records the IMAP UID range. If the configured scan cap did
not cover every message since the activity began, archiving is blocked.

Do not archive immediately after the last send. Wait for the bounce window recommended by your mail
provider (commonly at least 24–72 hours), scan again, then archive. An archive is immutable; the
current v0.2 web UI does not append late DSNs received after archival.

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
python scripts/mailer.py preview samples/event_registrations.csv --activity-id event-2026-july --out sendable.csv
```

If `--activity-id` is omitted, preview generates a fresh random ID so a later campaign cannot reuse
the same Message-ID namespace accidentally.

Preview a simple one-template list:

```bash
python scripts/mailer.py preview samples/attendees.csv --template default --out sendable.csv
```

Test before sending. The redirect must equal the SMTP user or From address and must not also appear
in the customer list:

```bash
python scripts/mailer.py send --input sendable.csv --config config.yaml --redirect-to sender@example.com --limit 3
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

The CLI is a low-level single-batch interface. It has the same fail-closed SMTP recovery and
Message-ID bounce matching, but v0.2 does not provide CLI commands for web-style close/archive or
cross-folder duplicate-campaign authorization. Use the local web UI for the complete audited
1000+ workflow.

## Installable CLI

You can also install it as a local CLI:

```bash
python -m pip install -e .
```

Then use:

```bash
mailpilot preview samples/event_registrations.csv --activity-id event-2026-july --out sendable.csv
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

The production ledger is paired with a durable start marker and integrity digest. MailPilot refuses
to resume if a complete JSONL event, the whole ledger, or either marker disappears. A non-recipient
`batch_started` event makes ordinary SMTP login/network failures safely retryable before the first
message attempt. If power is lost after exactly one complete tail event is fsynced but before its
digest/head update, MailPilot verifies the previous digest and repairs only that one valid tail;
partial or multiple unexplained changes remain blocked.

If the process stops halfway through, run the same command again. MailPilot replays the ledger first
and skips rows already accepted by SMTP. If a process stopped after an attempt began but before a
definite SMTP result was recorded, that row becomes `unknown` and requires manual reconciliation;
MailPilot will not risk sending it twice automatically.

The status imported during preview is also sealed as immutable history. Clearing a current `status`
cell later cannot turn a historically sent, bounced, suppressed, or uncertain row back into a
sendable row.

### Finishing an activity safely

The local web app uses these persistent phases:

```text
PREVIEW -> OUTBOUND_OPEN -> OUTBOUND_CLOSED -> ARCHIVED
```

- `OUTBOUND_OPEN`: bounded sending and evidence-backed exception handling are allowed.
- `OUTBOUND_CLOSED`: the batch can never send again; scan and apply bounces next.
- `ARCHIVED`: manifest, production ledger, lifecycle ledger, bounce report, and counts are sealed in
  `archive-receipt.json`.

Re-uploading overlapping recipients still opens the archived activity by default. To start a truly
new campaign, open the archive, choose **Start a new activity**, type the exact confirmation phrase,
then upload the new list within 30 minutes. A new Activity ID gives the campaign a separate
Message-ID namespace. Addresses previously marked `bounced` or `unsubscribed` remain blocked.

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
python scripts/mailer.py send --input sendable.csv --config config.yaml --redirect-to sender@example.com --limit 3
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

Run the complete offline safety acceptance gate (all tests forbid real socket connections):

```bash
python scripts/safety_acceptance.py
```

Non-technical macOS/Windows users can double-click `run_safety_acceptance.command` or
`run_safety_acceptance.bat` after the current app launcher has completed its one-time environment
setup. That setup installs the test tools too. The acceptance launcher itself never installs or
downloads anything; the suite uses Fake SMTP and Fake IMAP only and never logs into a mailbox.

## Double-click startup troubleshooting

- The launchers create a project-local `.venv`; they do not install packages into the global Python.
- First launch needs internet to install dependencies. Later launches reuse the local environment.
- Keep the terminal window open while using MailPilot.
- The app binds `127.0.0.1:8501` before opening the browser, preventing the former
  `ERR_CONNECTION_REFUSED` startup race.
- If port 8501 is occupied, close the older MailPilot terminal and retry.
- On macOS, if Gatekeeper blocks the launcher, right-click it and choose **Open** once.
- On Windows, install Python 3 with **Add Python to PATH** enabled. Failures remain visible in the
  terminal; press a key only after copying the error for the maintainer.
- The repository CI is configured for Windows, macOS, and Linux. A real Windows double-click still
  needs to be recorded on a Windows computer before calling that launcher field-tested.

## Roadmap

- HTML email templates
- Attachments
- SQLite checkpoint ledger option
- SendGrid / SES / Mailgun adapters
- Better DSN bounce parsing
- Richer preview reports

## License

MIT — see [LICENSE](LICENSE).
