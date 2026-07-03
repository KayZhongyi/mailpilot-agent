# MailPilot Agent

**Crash-safe mail merge for AI agents** (Claude Code, Cowork, Codex CLI, Hermes).

Point it at a recipient list and a template. It personalizes each email, shows you a preview to
approve, sends in batches, tracks exactly what was sent, and reconciles bounces so you know what
actually got through. It's a portable [Agent Skill](https://code.claude.com/docs/en/skills) — drop
the folder into your agent and talk to it in plain language, or run the CLI directly.

> **You decide who gets what; the tool just sends it reliably.** Rows with no valid email or an
> unknown template are flagged, never silently sent.

## Features

- 📋 **Reads Excel/CSV** and auto-detects the email / name / group columns (English or Chinese headers).
- ✍️ **Two modes** — *simple* (one template to everyone) or *grouped* (a `group` column picks a template per row).
- 👀 **Preview before sending** — per-group counts, a rendered sample of each, and a list of un-sendable rows.
- 🔁 **Crash-safe & resumable** — appends a per-email JSONL checkpoint before moving on; a re-run only sends what's left.
- ⏰ **Schedule & throttle** — `--at "2026-07-02 09:00"`, `--sleep 2`.
- 🧪 **Safe testing** — `--redirect-to you@example.com` sends the whole batch to yourself; `--dry-run` sends nothing.
- 📈 **Delivery tracking** — `bounces` reads bounce notifications and marks undelivered rows with the reason.
- 📨 **Better inbox rates** — adds a `List-Unsubscribe` header; works with any SMTP provider.

## Quickstart

```bash
# 1. Install deps for direct folder usage
pip install pandas openpyxl jinja2 pyyaml certifi

# 2. Configure. Fill in your APP PASSWORD (not your login password) — see references/
cp config.example.yaml config.yaml
python scripts/mailer.py doctor --config config.yaml     # verify deps / config / SMTP

# 3. Preview (review before sending)
python scripts/mailer.py preview samples/event_registrations.csv --out sendable.csv     # auto-groups (confirmed / waitlist)
#   simple mode (one email to all): preview samples/attendees.csv --template default
#   messy data safety demo: preview samples/messy_registrations.csv --out sendable.csv

# 4. Test, then send for real
python scripts/mailer.py send --input sendable.csv --config config.yaml --redirect-to you@example.com --limit 3
python scripts/mailer.py send --input sendable.csv --config config.yaml
python scripts/mailer.py bounces --input sendable.csv --config config.yaml --apply
```

Optional installable CLI:

```bash
python -m pip install -e .
mailpilot preview samples/event_registrations.csv --out sendable.csv
mailpilot send --input sendable.csv --config config.yaml --dry-run
```

Developer checks:

```bash
python -m pip install -e ".[dev]"
mailpilot --help
python -m ruff check .
python -m pytest
```

## Using it conversationally (recommended for non-technical users)

In Cowork or Claude Code, just talk to it:

> "Group this list by the `status` column, then show me how many go to each and a sample of each."
> "Looks good — send at 5pm and tell me the results."

The agent runs `doctor` → `preview` → waits for your OK → (optional test) → `send` → `bounces`, and
walks you through the one manual step (getting your mail app password). **Run it inside a
local-execution agent** — plain web chat can't reach mail servers.

## Crash-safe checkpointing

`send` is intentionally serial: it sends one message, records the result, then moves to the next.
The checkpoint is an append-only JSONL file beside your send list, for example
`sendable.csv.sendlog.jsonl`. If the process, terminal, or computer stops halfway through, run the
same command again; the tool replays the ledger first and skips addresses already accepted by SMTP.

The CSV is still updated for convenience, but it is written atomically instead of rewritten after
every single email. That keeps the recovery guarantee while avoiding huge write amplification on
large batches.

## List format

| column | required | notes |
|--------|----------|-------|
| email  | ✅ | headers containing `email` / `邮箱` are auto-detected |
| name   | optional | used for the `{{ name }}` greeting |
| group  | optional | presence enables grouped mode; value = template name |
| sent   | optional | truthy rows (`yes`/`1`/`是`) are skipped (already sent) |

Try `samples/messy_registrations.csv` to see the safety checks in action: empty emails, malformed
emails, missing templates, and already-sent rows are surfaced during preview instead of being sent.

## Templates

Plain text + [Jinja2](https://jinja.palletsprojects.com/) in `templates/`. First line `Subject: ...`
is the subject; the rest is the body. `{{ name }}` is filled per recipient.

- Simple mode → `templates/default.txt` (or `--template <name>`).
- Grouped mode → group value `confirmed` uses `templates/confirmed.txt`, etc.

## Providers & deliverability

Works with any SMTP provider — set `smtp.host/port/user/password` in `config.yaml`. **`password` must
be an app password / authorization code, not your login password** (see
[references/email_provider_setup.md](references/email_provider_setup.md)).

`send` marking a row `sent` means the server *accepted* it — not that it landed in the inbox. Spam
placement is invisible to senders; use `bounces` to catch hard failures. For large blasts to external
recipients, a transactional service (SES / SendGrid / Mailgun) delivers more reliably — only
`config.yaml` changes.

Only send to recipients who asked for or expect the email. Keep unsubscribed/bounced addresses out of
future lists, throttle large batches, and include a working unsubscribe contact.

## License

MIT — see [LICENSE](LICENSE).
