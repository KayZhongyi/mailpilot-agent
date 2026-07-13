# Email provider setup (app passwords & SMTP)

## The #1 gotcha: use an APP PASSWORD, not your login password

Most providers block sending via SMTP with your normal login password. You must generate an
**app password** (a.k.a. authorization code / client password) and put *that* in `config.yaml`
under `smtp.password`. Using your login password will fail authentication.

Getting the app password requires logging into the provider and passing 2FA — **you have to do this
step yourself**; an AI agent cannot click through your mailbox's security pages.

## SMTP settings by provider

| Provider | host | port | use_ssl |
|----------|------|------|---------|
| Gmail / Google Workspace | `smtp.gmail.com` | 465 | true |
| Outlook / Microsoft 365 | `smtp.office365.com` | 587 | false (STARTTLS) |
| NetEase enterprise (@qiye.163.com) | `smtphz.qiye.163.com` | 465 | true |
| Yahoo | `smtp.mail.yahoo.com` | 465 | true |

### Gmail / Google Workspace
1. Enable 2-Step Verification on your Google account.
2. Go to Google Account → Security → **App passwords** (https://myaccount.google.com/apppasswords).
3. Create one for "Mail", copy the 16-character code → `config.yaml` `smtp.password`.
> Google Workspace admins may need to allow app passwords for the org.

Personal Gmail may reject sending after more than 500 messages in one day. Workspace quotas differ
and may be lower for trial accounts. MailPilot cannot see mail sent elsewhere from the same account,
so ask the administrator to confirm the remaining quota before every large run. Official limits:
https://support.google.com/mail/answer/22839 and
https://support.google.com/a/answer/166852.

### Outlook / Microsoft 365
1. Ask the Microsoft 365 administrator whether **Authenticated SMTP** is enabled for this mailbox
   and tenant. Do not assume an app password will work.
2. If the administrator authorizes password SMTP AUTH, use `port: 587`, `use_ssl: false`
   (STARTTLS), then run `doctor` before any test message.
3. Microsoft says existing-tenant SMTP AUTH Basic behavior remains unchanged through December
   2026; it then becomes disabled by default, with final removal timing to be announced in H2 2027.
   Tenant policy may already block it. Plan migration to OAuth or a supported relay:
   https://techcommunity.microsoft.com/blog/exchange/updated-exchange-online-smtp-auth-basic-authentication-deprecation-timeline/4489835
4. Exchange Online password IMAP is already unsupported. MailPilot v0.2 has no IMAP OAuth and
   intentionally blocks `outlook.office365.com` bounce scans. The web workflow requires the IMAP
   login identity to equal the production SMTP user or From address, so an unrelated mailbox is not
   a workaround; do not put an Outlook password into the IMAP section. A provider event export cannot
   satisfy the v0.2 audited archive gate because event import is not implemented. The web UI
   therefore allows Microsoft 365 redirected tests but blocks Microsoft 365 production sending.

Exchange Online documents 30 messages per minute and 10,000 recipients per day, while recommending
specialist third-party providers for legitimate bulk commercial email. Other activity on the
mailbox counts too: https://learn.microsoft.com/en-us/office365/servicedescriptions/exchange-online-service-description/exchange-online-limits.

### NetEase enterprise (@qiye.163.com and company domains hosted on it)
1. Log into the NetEase enterprise webmail (https://qiye.163.com).
2. Settings → Client / SMTP (or POP3/SMTP/IMAP) → enable **SMTP service**.
3. Create a **client authorization code** (客户端授权码), complete the SMS/scan verification.
4. Copy that code → `config.yaml` `smtp.password`.
> If `smtphz.qiye.163.com` doesn't connect, try `smtp.qiye.163.com`.

## Verify it works

```bash
python scripts/mailer.py doctor --config config.yaml
```
A green "All set" result means your settings + app password are correct.

## Deliverability at scale

Sending ~1000+ emails to external recipients from an ordinary mailbox can trip rate limits or land
in spam — even with correct authentication — because of sending reputation and burst volume. For
large or recurring blasts, use a transactional email service (Amazon SES, SendGrid, Mailgun, Aliyun
DirectMail) with your domain's SPF/DKIM/DMARC configured. An SMTP-capable service can often be used
for **sending** by changing `config.yaml`, but end-to-end bounce handling is not interchangeable:
these providers commonly expose API/webhook/event exports instead of an IMAP inbox. MailPilot v0.2
does not import those event APIs automatically. Use an INBOX-accessible password IMAP mailbox whose
login identity matches the production SMTP user/From address, or stop before production. An
API/event export alone cannot create a valid v0.2 archive; never claim a
completed bounce scan when the evidence is outside `INBOX`.

Gmail's sender rules also require authentication and, at higher marketing volumes, one-click and
visible unsubscribe facilities: https://support.google.com/mail/answer/81126. MailPilot v0.2 only
adds a basic `List-Unsubscribe` contact header; it does not host RFC 8058 one-click unsubscribe or
register late opt-outs into a global suppression list. Use a compliant provider and upstream consent
workflow for marketing mail.

For Amazon SES, quotas are region-specific and sandbox accounts start with much smaller limits.
Check the current SES quota before sending: https://docs.aws.amazon.com/ses/latest/dg/manage-sending-quotas.html.
