# Contributing

Thanks for helping improve Grok Build Usage.

## Dev setup (macOS)

```bash
git clone https://github.com/vbusnita/grok-build-usage.git
cd grok-build-usage
python3.11 -m venv .venv   # 3.10+ works; 3.11/3.12 preferred
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

If you don’t have an editable extra yet:

```bash
pip install -e .
pip install pytest
pytest -q
```

## Conventions

- Keep the overlay **chrome-free** unless a change is discussed (floating type + bar).
- Never log or print auth tokens.
- Prefer small PRs: one idea per PR.
- Run `pytest` before opening a PR.

## Packaging notes

- `scripts/install-app.sh` builds a personal `~/Applications/*.app` that points at
  the clone’s `.venv`. That is intentional for a source-based open-source app.
- A future Homebrew cask / signed DMG can embed a Python runtime; keep install.sh
  working for contributors either way.
