---
name: mailpilot-agent
description: "Crash-safe mail merge for AI agents. Give it a recipient list (optionally grouped by a column) and templates; it previews for review, sends in batches, is idempotent/resumable with an append-only ledger, and reconciles bounces. Great for status notifications, customer updates, and pre-sales follow-ups."
version: 0.1.0
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Email, Automation, SMTP, Mail-Merge, Agent-Skill]
prerequisites:
  commands: [python3]
  python: [pandas, openpyxl, jinja2, pyyaml, certifi]
---

# MailPilot Agent

Crash-safe mail merge for AI agents. Send a batch of personalized emails from a spreadsheet and
track whether each one was sent and delivered. **Two modes:**

- **Simple mode** — one recipient list + one template → the same email to everyone (with a personalized greeting).
- **Grouped mode** — the list has a `group` column → each group gets its own template.

## Core principles (important)

- **The tool only faithfully sends the list you already grouped. It does not decide who belongs in which group.** Deciding the grouping is the user's job (optionally with the agent's help in chat).
- **Rows with no valid email, or whose template can't be found, are surfaced for review and are NEVER sent automatically.**
- Sending is **idempotent and crash-safe**: `send` writes an append-only JSONL checkpoint after each SMTP result, then atomically syncs the CSV. Re-run after an interruption and it only sends what's left; rows marked "sent" in the list are skipped.
- **`send` marking a row `sent` only means "the mail server accepted it" — NOT that it was delivered.** Always run `bounces` afterward to reconcile.

## Workflow the agent should follow

1. **Self-check first**: run `doctor` to verify deps / config / SMTP. Fix what it flags — including that obtaining the email **app password** is a step the **user must do themselves** in their mail provider's settings (the agent can't log into their webmail / pass 2FA). Guide them using `references/email_provider_setup.md`, then write it into `config.yaml`.
2. **Preview**: user provides the list → run `preview` → read the per-group counts + one sample per group + the flagged un-sendable rows **out loud in the chat** for the user to review (do NOT email a report).
3. **Wait for confirmation**: only proceed after the user says "send" (they may give a time). Send nothing before that.
4. **Test first**: `send --redirect-to <user's test inbox> --limit 3` so they see the real emails.
5. **Send for real**: `send` (optionally `--at` to schedule, `--sleep` to throttle, `--only` per group). Report success/failure counts back.
6. **Reconcile bounces**: run `bounces` to mark undelivered rows `bounced`, and report that list to the user (those need manual follow-up).

## Command reference

Prereq: copy `config.example.yaml` to `config.yaml` and fill in your **app password** (NOT your login password — see `references/email_provider_setup.md`).

```bash
pip install pandas openpyxl jinja2 pyyaml certifi          # first-time deps

python scripts/mailer.py doctor --config config.yaml        # check deps / config / SMTP

# Preview (review in chat). Simple mode picks a template; grouped mode auto-detects the group column.
python scripts/mailer.py preview list.xlsx --template default --out sendable.csv
python scripts/mailer.py preview list.xlsx --out sendable.csv        # auto-groups if a group column exists
python scripts/mailer.py preview samples/messy_registrations.csv --out sendable.csv    # safety demo

# Test send (redirect everything to a test inbox, a few at a time)
python scripts/mailer.py send --input sendable.csv --config config.yaml --redirect-to test@example.com --limit 3

# Real send (idempotent; schedule / throttle / send one group)
python scripts/mailer.py send --input sendable.csv --config config.yaml
#   checkpoint ledger defaults to sendable.csv.sendlog.jsonl
#   --at "2026-07-02 09:00"   send at a scheduled time (process must stay running)
#   --sleep 2                 pause 2s between emails (throttle for large batches)
#   --only confirmed          send only one template/group

# Reconcile bounces after sending
python scripts/mailer.py bounces --input sendable.csv --config config.yaml            # preview
python scripts/mailer.py bounces --input sendable.csv --config config.yaml --apply     # mark bounced rows
```

If installed with `python -m pip install -e .`, the same commands are available via `mailpilot`:

```bash
mailpilot preview list.xlsx --out sendable.csv
mailpilot send --input sendable.csv --config config.yaml --dry-run
```

## List format

Needs at least an **email column** (headers containing "email"/"邮箱" are auto-detected). Optional:

- **name column** — for the personalized greeting `{{ name }}`;
- **group column** (headers containing "group"/"type"/"组别" etc.) — presence switches on grouped mode; the value = which template to use;
- **sent column** (headers containing "sent"/"已发") — rows with a truthy value (`yes`/`1`/`是`/`sent`) are skipped, so you don't re-send ones already sent manually.

## Templates

Plain text + Jinja2 variables, in `templates/`:

- Simple mode uses `templates/default.txt` (override with `--template <name>`).
- Grouped mode: group value `confirmed` → `templates/confirmed.txt`, and so on.
- The first line `Subject: ...` is the subject; the rest is the body. `{{ name }}` is replaced with the recipient's name.

## Steps only the user can do (agent: give guidance, then wait)

1. **Get the mail app password / authorization code** — requires logging into the provider and passing 2FA; the agent can't do this. Guide the user per `references/email_provider_setup.md`, then put it in `config.yaml`.
2. **Provide the real list and email content.**
3. **Confirm sending** — for outbound bulk mail, the final go/no-go and timing are the user's call.

## Config & docs

- `config.example.yaml` — SMTP config template (copy to `config.yaml`). Includes `unsubscribe` (adds a `List-Unsubscribe` header to improve bulk deliverability) and optional `imap` (for `bounces`).
- `references/email_provider_setup.md` — how to get an app password for Gmail / Outlook / NetEase, plus deliverability notes.
- `samples/` — `event_registrations.csv` (grouped mode: confirmed / waitlist), `attendees.csv` (simple mode), and `messy_registrations.csv` (safety demo for invalid emails, missing templates, and already-sent rows).

## Known limits

- **Must run on a local-execution AI** (Claude Code / Cowork / Codex CLI / Hermes). Plain web chat sandboxes can't reach mail servers, so they can't actually send.
- **Deliverability**: sending bulk to external/foreign inboxes from an ordinary mailbox may hit spam or rate limits. For one-off blasts of ~1000+, a transactional email service (Amazon SES / SendGrid / Mailgun / Aliyun DirectMail) delivers more reliably — switching only changes `config.yaml`. Always run `bounces` afterward.
