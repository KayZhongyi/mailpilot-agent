# Contributing

Thanks for improving MailPilot Agent.

## Local checks

```bash
python -m pip install -e ".[dev]"
ruff check .
pytest
```

## Design priorities

- Local-first: no SaaS dependency and no telemetry.
- Review-first: preview before sending, and make unsafe rows visible.
- Resumable: every attempted delivery must be recoverable after interruption.
- Boring formats: CSV, JSONL, YAML, plain-text templates.
- Responsible sending: consent, unsubscribe, throttle, and bounce handling matter.
