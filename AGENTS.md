# Repository Guidelines

## Project Structure & Module Organization

`tcad/` contains the Python harness: `ir/` defines declarative CAD operations, `loop/` orchestrates model turns, `tools/` exposes validated operations, `worker/` compiles geometry through FreeCAD, and `verify/` checks results. Provider configuration lives in `config/` and `llm/`. `server/` hosts FastAPI and the static HTML/CSS/JavaScript interface in `server/ui/`.

`configs/` holds YAML configuration and policy profiles; `tools/` contains launchers, diagnostics, and probes. Tests are split into `tests/unit/`, `tests/contract/`, `tests/e2e/`, and `tests/frontend/`. Design notes live in `docs/`, with acceptance artifacts in `review/`. Runtime data in `data/` and upstream FreeCAD sources/builds in `free-cad/` are ignored.

## Build, Test, and Development Commands

- `python3 -m venv .venv` and `.venv/bin/pip install -e ".[server,dev]"`: install Python 3.11+ development dependencies.
- `export TCAD_FREECAD_CMD=/path/to/FreeCADCmd`: select the geometry worker executable.
- `.venv/bin/python tools/serve.py`: start the UI at `http://127.0.0.1:8000/ui/`; add `--no-worker` for configuration/UI work without FreeCAD.
- `.venv/bin/python tools/doctor.py`: check the supervisor, geometry kernel, and model service.
- `.venv/bin/python -m pytest tests/unit -q`: run unit tests.
- `node --test tests/frontend/*.test.mjs`: run frontend tests without a browser build step.
- `.venv/bin/python -m pytest tests -q`: run the Python suite, including contracts requiring real FreeCAD.

## Coding Style & Naming Conventions

Follow existing code: four-space Python indentation, type annotations, Pydantic models for structured contracts, and `snake_case` functions/modules. JavaScript uses two-space indentation, semicolons, and `camelCase`. No formatter or linter is configured in `pyproject.toml`; avoid unrelated formatting changes. Use `git diff --check` before submission.

## Testing Guidelines

Use pytest and pytest-asyncio for Python, and Node's built-in test runner for frontend behavior. Name Python tests `test_*.py` and frontend tests `*.test.mjs`. Add regression tests for changed behavior; mock provider HTTP requests in unit tests. Real-provider E2E tests require explicit credentials. No numeric coverage threshold is configured; report actual checks and skips.

## Commit & Pull Request Guidelines

History follows Conventional Commit-style prefixes: `feat`, `fix`, `docs`, and `chore`, optionally scoped, such as `fix(llm): normalize tool schemas`. Keep commits focused. PR descriptions should explain the problem, resulting behavior, validation, and limitations; link relevant issues and include screenshots for visible UI changes.

## Architecture & Configuration Safety

Preserve the declarative IR boundary: models use validated tools, while FreeCAD runs in its separate worker. Geometry compilation alone does not prove functional completion. Never commit credentials or print saved keys. Persisted `data/settings.json` overrides YAML/environment configuration; command-line model overrides are temporary.
