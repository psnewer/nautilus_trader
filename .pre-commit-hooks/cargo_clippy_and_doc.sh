#!/usr/bin/env bash
set -euo pipefail

# Combined clippy and doc check to minimize rebuild overhead.
#
# Why combine these checks:
#
# 1. Running sequentially in one script reduces cargo process overhead:
#    - Single lock acquisition on target/ directory,
#    - One dependency graph resolution instead of two,
#    - Less filesystem thrashing between separate hook invocations.
#
# 2. Doc can immediately reuse clippy's check artifacts (metadata files and compiled
#    dependencies) without cargo having to revalidate freshness between processes.

FEATURES="high-precision,ffi,python,extension-module"

echo "Running cargo clippy..."
cargo clippy \
  --workspace \
  --all-targets \
  --no-default-features \
  --features "$FEATURES" \
  -- \
  -D warnings

echo "Running cargo doc..."
RUSTDOCFLAGS="-D warnings" cargo doc \
  --no-default-features \
  --features "$FEATURES" \
  --no-deps \
  --workspace \
  --quiet
