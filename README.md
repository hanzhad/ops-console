# ops-console

Local operations console (`server.py`, SSE `/api/events`, SQLite read model) for monitoring dev-container slots, CI gates, auto-review queues, and agent sessions.

## Layout

- `server.py` — HTTP & SSE server (`127.0.0.1:7631`)
- `page.html` + `static/` — single-page dashboard UI
- `sw/` — domain pollers, SQLite store, command handlers, and watchers
- `bin/` — helper probes and CLI utilities
- `scripts/ci/` — test runner (`run-tests.py`) and completeness checker (`check-test-legs.py`)

## Tests

```bash
python3 scripts/ci/run-tests.py --report test-report.json
git ls-files -- 'test_*.py' | python3 scripts/ci/check-test-legs.py test-report.json
python3 -m unittest discover -s tests -v
```
