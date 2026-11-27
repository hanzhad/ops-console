# Architecture & Domain Contract

## Domain Discovery (`BlockSpec` / `RouteSpec`)

`sw/domains/__init__.py` discovers every module in `sw/domains/` via `pkgutil.iter_modules` and collects its `BLOCKS: list[BlockSpec]` and `ROUTES: dict[path, RouteSpec]`. Adding a new panel requires only dropping a module into `sw/domains/` and a matching renderer into `static/`.

## SQLite Read Model & SSE Replay

- **`sw/store.py`:** maintains a disposable SQLite database (`~/.cache/ops-console/state.db`) with monotonic revision numbers (`rev`) across `blocks`, `commands`, and `events`.
- **`sw/core.py`:** pure canonical JSON comparison (`decide_put`) so unchanged poller ticks never emit redundant SSE frames.
- **`sw/http.py` & `sw/bus.py`:** serves `page.html`, `static/*.js`, `POST /api/*` command endpoints, and `GET /api/events` SSE with `Last-Event-ID` replay.

## Environment Variables & Service

| Variable | Purpose |
| --- | --- |
| `OPS_CONSOLE_PORT` | HTTP listen port (default `7631`). |
| `OPS_CONSOLE_DEMO` | Set to `1` to start in `--demo` fixture mode. |
| `OPS_CONSOLE_CI_RUNNER` | Path to `docker-ci-gate/ci-runner.sh`. |
| `OPS_CONSOLE_ENGINE_DIR` | Path to `agent-dev-slots` checkout. |
| `OPS_CONSOLE_AUTOREVIEW_REPO` | Path to `pr-auto-review` checkout. |
| `OPS_CONSOLE_WIN_HOME` | Windows user home path under WSL (`/mnt/c/Users/user`) for session probes. |

`ops-console.service` provides a sample user `systemd` unit expecting a checkout at `%h/ops-console/server.py`.
