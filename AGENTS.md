# Agent Rules (hanzhad/ops-console)

- **Workflow:** Work on a feature branch, open a pull request with full context, and ensure CI (`test` + `scan`) passes before merging.
- **Commits:** Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`).
- **Public safety:** Every change must pass `python3 scripts/scan.py --tree .` and `python3 scripts/scan.py --history`. Never commit tokens, credentials, personal emails, private repo references, binary files, or host-specific home paths.
- **Dependencies:** Python 3 standard library only for the server and backend; vanilla JS (classic `<script>` tags, no `type="module"`, no bundler) for the UI.
