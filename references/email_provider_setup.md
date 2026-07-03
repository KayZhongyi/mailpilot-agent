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

### Outlook / Microsoft 365
1. Enable 2-Step Verification.
2. Microsoft account → Security → Advanced security → **App passwords** → create one.
3. Note: many M365 orgs disable SMTP AUTH by default — an admin may need to enable
   "Authenticated SMTP" for the mailbox. Ask your IT.
4. Use `port: 587`, `use_ssl: false` (STARTTLS). For `bounces`, set IMAP host to
   `outlook.office365.com`.

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
DirectMail) with your domain's SPF/DKIM/DMARC configured. Switching only changes `config.yaml`
host/port/user/password; the tool is unchanged. Regardless of provider, run `bounces` after each
batch to confirm what actually got through.
