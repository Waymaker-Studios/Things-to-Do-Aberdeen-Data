# Things-to-Do-Aberdeen-Data

`events.json` is the remote event feed used by Community Notice.

## Maintenance

The maintenance script uses the Europe/London date, preserves ongoing recurrence
rules, validates the feed before and after cleanup, and refuses unusually large
removals unless explicitly approved.

```bash
python3 scripts/maintain_events.py --validate-only
python3 scripts/maintain_events.py --dry-run
python3 scripts/maintain_events.py --apply
```

Running it without a mode is also a safe dry run. Use `--date YYYY-MM-DD` for
expiry tests without changing the system clock. Use `--allow-large-removal`
only after reviewing a dry-run report.
