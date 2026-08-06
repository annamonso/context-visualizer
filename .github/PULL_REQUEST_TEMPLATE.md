## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## Why

<!-- The reasoning that is not obvious from the diff. -->

## How it was verified

<!-- Offline mode? Against a live cluster? How many nodes? -->

## Checklist

- [ ] `ruff check .` passes
- [ ] `pytest tests -q` passes
- [ ] `cd frontend && npx tsc --noEmit && npm run build` passes (if the UI changed)
- [ ] Docs updated (`README.md` / `docs/`) if behaviour or setup changed
- [ ] No hostnames, IPs, usernames or local paths hardcoded
