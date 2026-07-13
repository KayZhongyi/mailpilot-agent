---
name: mailpilot-agent
description: "Crash-safe mail merge for AI agents. Give it a recipient list (optionally grouped by a column) and templates; it previews for review, sends in batches, is idempotent/resumable with an append-only ledger, and reconciles bounces. Great for status notifications, customer updates, and pre-sales follow-ups."
version: 0.2.0
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
- If the user's CSV is not grouped yet, ask for explicit grouping rules, create a `group` / `template` column in a working copy, then run `preview`. Do not infer recipient segments silently.
- Grouped mode maps each row's group value directly to a template file: `confirmed` -> `templates/confirmed.txt`, `waitlist` -> `templates/waitlist.txt`. Missing templates are preview errors and are never sent.
- **Rows with no valid email, or whose template can't be found, are surfaced for review and are NEVER sent automatically.**
- Sending is **fail-closed on interruption**: `send` writes `attempting` before SMTP, then appends the result and atomically syncs the CSV. A result that cannot be proven becomes `unknown` and is never retried automatically.
- Historical suppression is immutable after preview: clearing the current `status` does not make an originally `sent`, `bounced`, `unknown`, or suppressed row eligible again.
- Once production starts, never create a replacement batch whose recipients overlap it. Reopen the original run and its ledger; mapping changes or a separate campaign require explicit reconciliation first.
- If any row is `unknown`, stop the entire production batch. Require provider evidence, then record either confirmed acceptance or permanent suppression; never auto-retry an uncertain attempt.
- For 1000+ work, use the local web UI to finish each activity explicitly: close outbound permanently, scan bounces with complete UID coverage, apply only Message-ID-bound DSNs, acknowledge visible unverified candidates, and archive. Only an archived web activity may authorize a new overlapping campaign.
- Before 1000+ work, verify the provider's current daily/rate quota and count mail sent outside MailPilot. Personal Gmail cannot safely complete 1000 messages in one day; ordinary Microsoft 365 mailboxes also have rate limits. Prefer a specialist provider for external/marketing bulk mail.
- Structured bounce evidence must prove `Action=failed`, a valid enhanced status, and this activity's exact Message-ID in the same DSN. `delayed`, malformed, and address-only reports remain manual-review items. Only narrow address-invalid statuses (`5.1.1`/`5.1.2`/`5.1.3`) may become cross-activity hard suppression; policy and temporary failures are activity-local `soft_bounced`.
- The IMAP scanner reads `INBOX` only. Check Spam/Junk first. v0.2 has no IMAP OAuth and must block Exchange Online password IMAP. Provider event export cannot satisfy the audited archive gate because v0.2 has no event-import path; the web UI therefore blocks Microsoft 365 production while allowing redirected tests.
- Preserve the whole `.mailpilot_runs` directory when upgrading or moving the app. Never operate old and new copies of one activity concurrently.
- Never stage or commit recipient CSV/XLSX files, `config*.yaml`, `.env*`, ledgers, or `.mailpilot_runs`. Check `git status` before every commit; only the repository's public `samples/*.csv` fixtures are allowed.
- **`send` marking a row `sent` only means "the mail server accepted it" — NOT that it was delivered.** Always run `bounces` afterward to reconcile.

## Workflow the agent should follow

1. **Self-check first**: run `doctor` to verify deps / config / SMTP. Fix what it flags — including that obtaining the email **app password** is a step the **user must do themselves** in their mail provider's settings (the agent can't log into their webmail / pass 2FA). Guide them using `references/email_provider_setup.md`, then write it into `config.yaml`.
2. **Prepare grouping if needed**: if the list already has a grouping column (`group`, `template`, `status`, `stage`, `segment`, `组别`, `报名状态`, etc.), use it. If not, ask the user for explicit rules and create a working CSV with a `group` column. The sending tool must not guess business segmentation on its own.
3. **Preview**: user provides the list → run `preview` → read the per-group counts + one sample per group + the flagged un-sendable rows **out loud in the chat** for the user to review (do NOT email a report).
4. **Wait for confirmation**: only proceed after the user says "send" (they may give a time). Send nothing before that.
5. **Test first**: `send --redirect-to <sender inbox> --limit 3` so they see the real emails. The test inbox must exactly equal the SMTP user/From address and must not be present in the customer list. Redirected tests use a separate ledger and never mark customer rows as sent.
6. **Send for real in bounded batches**: confirm the provider quota first, always provide `--limit` (maximum 200) and `--sleep` (minimum 1 second for production), and keep `smtp.max_messages_per_connection` at or below the provider's documented cap. Start with a small batch and report accepted/error/unknown counts back. A 4xx, sender/account failure, ambiguous outcome, or repeated identical DATA rejection stops the batch for review.
7. **Close and reconcile in the web UI**: after ready/unknown/error are all zero, close outbound. Wait for the provider's bounce window, then scan using the recorded SMTP/From identity; only DSNs tied to this activity's deterministic Message-ID may be applied automatically. Address-only matches must be shown for manual review, and wrong mailbox identity or incomplete IMAP UID coverage blocks archive.
8. **Archive in the web UI**: after a valid post-close bounce scan and all candidates are handled, archive and export the audit report. Never delete the run directory to start a new campaign. v0.2 archives are immutable, so scan again before archiving if late DSNs may still arrive.

## Command reference

Prereq: copy `config.example.yaml` to `config.yaml` and fill in your **app password** (NOT your login password — see `references/email_provider_setup.md`).

```bash
pip install pandas openpyxl jinja2 pyyaml certifi          # first-time deps

python scripts/mailer.py doctor --config config.yaml        # check deps / config / SMTP

# Preview (review in chat). Simple mode picks a template; grouped mode auto-detects the group column.
python scripts/mailer.py preview list.xlsx --activity-id campaign-2026-07 --template default --out sendable.csv
python scripts/mailer.py preview list.xlsx --activity-id campaign-2026-07 --out sendable.csv
python scripts/mailer.py preview samples/messy_registrations.csv --out sendable.csv    # safety demo

# Test send (use the SMTP user/From inbox, never a customer address)
python scripts/mailer.py send --input sendable.csv --config config.yaml --redirect-to sender@example.com --limit 3

# Real send (idempotent; schedule / throttle / send one group)
python scripts/mailer.py send --input sendable.csv --config config.yaml --limit 50 --sleep 2
#   checkpoint ledger defaults to sendable.csv.sendlog.jsonl
#   --at "2026-07-02 09:00"   send at a scheduled time (process must stay running)
#   --sleep 2                 pause 2s between emails (throttle for large batches)
#   --only confirmed          send only one template/group

# Low-level CLI bounce reconciliation after sending. Use the web UI for audited close/archive.
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
- **group column** (headers containing "group"/"type"/"status"/"stage"/"segment"/"组别"/"报名状态" etc.) — presence switches on grouped mode; the value = which template to use;
- **sent column** (headers exactly matching "sent"/"已发" aliases) — use explicit sent/unsent values. Blank or ambiguous history is blocked rather than assumed unsent.

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

- `config.example.yaml` — SMTP config template (copy to `config.yaml`). Includes connection rotation, a basic `List-Unsubscribe` contact header, and optional IMAP credentials. The web workflow permits a different app password but requires the IMAP login identity to equal the production SMTP user or From address. The header is not RFC 8058 one-click unsubscribe and is not a complete compliance workflow.
- `references/email_provider_setup.md` — how to get an app password for Gmail / Outlook / NetEase, plus deliverability notes.
- `samples/` — `event_registrations.csv` (grouped mode: confirmed / waitlist), `attendees.csv` (simple mode), and `messy_registrations.csv` (safety demo for invalid emails, missing templates, and already-sent rows).

## Known limits

- **Agent-driven CLI commands require local execution** (Claude Code / Cowork / Codex CLI / Hermes). The Flask web UI needs no AI software and is the recommended path for non-technical teammates.
- **Deliverability/provider adapters**: sending bulk to external/foreign inboxes from an ordinary mailbox may hit spam or account limits. SMTP-capable transactional providers can be configured for sending, but v0.2 does not consume provider bounce/webhook exports or OAuth mailboxes automatically. Do not promise that changing only `config.yaml` provides end-to-end reconciliation.
- **Unsubscribe**: v0.2 has no hosted RFC 8058 one-click endpoint and no UI for recording late opt-outs into a durable global suppression list. Use a compliant provider/upstream consent list for marketing mail; do not claim bulk-sender compliance from the header alone.
- **Business decisions stay upstream**: never infer installation, parallel-operation, fraud, rebate eligibility, or rejection reasons. A business owner must provide a reviewed decision/template column and trustworthy historical send status before MailPilot may send.
