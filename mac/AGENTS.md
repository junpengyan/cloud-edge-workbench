# Repository Guidelines

## Project Structure & Module Organization

This is a Windows-oriented FastAPI workbench for device pools, test rounds, gateway generation, deployment, and acceptance verification.

- `server.py` serves the API and static UI; `workspace.py`, `rounds.py`, `deployment.py`, and `remote_validation.py` implement core workflows.
- `app/` contains models, services, orchestration, and the ThingsBoard client; `gateway_engine/` generates gateway projects.
- `capacity.py`, `microgrid.py`, and `acceptance.py` hold domain-specific calculations and checks.
- `static/` contains the plain HTML/CSS/JavaScript UI; `templates/` contains XML, CSV, and rule-chain inputs.
- `tests/` contains offline regression tests. Runtime state, credentials, logs, and generated artifacts belong in `data/` and must not be committed.

## Build, Test, and Development Commands

Run commands from `cloud-edge-workbench` in PowerShell:

```powershell
py -m venv .venv
.\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt
.\\.venv\\Scripts\\python.exe -m unittest discover -s tests -v
powershell -ExecutionPolicy Bypass -File .\\start.ps1 -Port 18768
powershell -ExecutionPolicy Bypass -File .\\stop.ps1
```

The test command runs the offline suite. The start/stop scripts manage the local server; browse to `http://127.0.0.1:18768/` or `/deployment`. For foreground debugging, run `python server.py`. There is no separate frontend build step.

## Coding Style & Naming Conventions

Use four-space Python indentation, `snake_case` functions, `PascalCase` classes, and uppercase constants. Preserve existing API/JSON field names. In JavaScript, follow the surrounding camelCase style. Keep user-facing UI text in Chinese, avoid unrelated reformatting, and run no formatter unless one is added to the project configuration.

## Testing Guidelines

Use `unittest`, `unittest.mock`, and FastAPI `TestClient`. Name files `test_*.py` and methods `test_<behavior>`. Use temporary `UNIFIED_DATA_DIR` storage and mock cloud, SSH, and MQTT calls. On non-Windows systems, run `python tests/run_portable.py`; no coverage threshold is configured.

## Commit & Pull Request Guidelines

The repository history is an initial source import, so no strict convention exists. Use concise imperative subjects, for example `Fix stale deployment validation`. PRs should describe behavior changes, link relevant issues, include test commands/results, and attach screenshots for UI changes. Clearly distinguish offline validation from real platform or gateway verification.

## Security & Configuration

Preserve loopback binding, request-token checks, confirmation gates for platform creation and remote deployment, and recovery safeguards. Never commit credentials or credential-bearing gateway archives. Windows DPAPI secrets are tied to the originating user; use the documented local setup scripts.
