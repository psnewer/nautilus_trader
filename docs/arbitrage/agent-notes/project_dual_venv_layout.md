---
name: project_dual_venv_layout
description: This repo has two coexisting Python venvs (.venv and venv); .venv is the canonical one for current work, venv is a stale legacy environment that is incomplete and not safe to switch to silently.
type: project
originSessionId: 218d30e5-cb09-4c32-bdb4-6d3c7214a6f7
---
The repo contains **two Python virtualenvs**:

- `.venv/` — uv-managed, current canonical environment. Resolved by `which python3` (PATH order). Has the full dependency set including the framework Cython/Rust extensions. **Use this.**
- `venv/` — legacy Python 3.13 environment. Lacks numpy and other framework essentials, so cannot run the full pipeline. Still has some packages from before the upstream merge (notably `py-builder-relayer-client==0.0.1` from a manual install that was never reflected in pyproject.toml).

**Why this matters**:
- After the upstream merge into `refactor/adapter`, `py-builder-relayer-client` was missing from .venv (it was only ever in venv/). That caused `RiskService` to log `PolymarketContractService init failed, cleanup disabled`. Fix: added it to `pyproject.toml` polymarket extras + `uv lock` + `uv pip install` (2026-04-30). The fix is in code now, but the dual-venv setup is the reason the bug appeared after a branch switch.
- A `uv sync` against the project triggers an editable rebuild of nautilus-trader itself, which currently fails on macOS because Homebrew's cargo (1.81) shadows rustup's cargo and crates require `edition2024`. Workaround when sync is needed: `export PATH="$HOME/.cargo/bin:$PATH"` to put rustup's cargo first. Direct `uv pip install <pkg>` bypasses the rebuild and is the fast path for individual deps.

**How to apply**:
- When debugging "why is this package missing", check both venvs before assuming the install failed — packages may live in venv/ but not .venv/.
- Don't propose `python3 -m venv venv && pip install -e .` style setup; this project uses uv, and the editable build is brittle outside .venv. If you want a clean env, recommend `uv sync` (and prep PATH for the Rust toolchain first).
