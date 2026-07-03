# Security Policy

## Credentials

Do not commit `config.yaml`, app passwords, SMTP passwords, exported inboxes, or real recipient
lists. Prefer environment-variable substitution in your own deployment wrapper when possible.

## Responsible Use

Use this project only for recipients who consented to receive the mail. Keep a suppression list for
unsubscribed or bounced addresses, and include a working unsubscribe contact for bulk messages.

## Reporting Issues

Please open a private security advisory or contact the maintainers before publishing credential
leaks, injection issues, or deliverability bypasses.
