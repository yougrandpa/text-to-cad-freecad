# UI refactor preview

Branch: `feat/ui_refactor`, based on local `main` at `884495e`.

The workspace pairs warm paper controls with a graphite model canvas and terracotta accents. Screenshots show the actual app served with `--no-worker` and an isolated temporary data directory; no saved credentials or production sessions were used.

- `desktop.jpg`: 1600 × 900, four-panel workspace.
- `mobile.jpg`: 390 px wide, scrollable conversation with the composer reachable in the first screen.
- Also inspected the 1440 px layout, which places the inspector below the model canvas.

Interaction changes: editable example drafts, IME-safe Enter handling, preservation of drafts when submission is blocked, sidebar expanded state, and keyboard focus cycling/restoration in settings.

Validation: 52 frontend tests, 60 UI server tests, and `git diff --check` passed. Browser checks covered example selection, responsive layout, and settings Shift+Tab/Escape handling. Geometry generation and real-provider requests were not run; CAD contracts and backend code are unchanged.

Merge review: integrated current main at `78b403d`, retained its rounded scrollbar behavior with panel-specific colors, constrained long session titles/IDs, and kept the unbounded-budget indicator visible on narrow screens. The integrated revision passed 52 frontend tests and 1331 unit tests (1 skipped), plus `git diff --check`. No backend files differ from current main.
