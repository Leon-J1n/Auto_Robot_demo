#!/bin/sh
# Apply OLS git hooks to this clone. Run once after cloning: sh hooks/install_hooks.sh
set -e
cd "$(dirname "$0")/.."
git config core.hooksPath hooks
chmod +x hooks/post-commit 2>/dev/null || true
echo "[ols-hooks] installed: core.hooksPath=hooks (post-commit version refresh)"