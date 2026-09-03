#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Mirror the repo-root frontend into backend/app/static.
#
# The FastAPI backend serves the UI at /app/ on the same origin, so the
# browser never makes a cross-site (third-party) request and never hits CORS.
# GitHub Pages still serves the repo root, so the root files remain the single
# source of truth; this script keeps the vendored copy used by the backend in
# sync. Run it after ANY change to index.html, map.html, styles.css,
# tailwind.css or anything in js/:
#
#     bash scripts/sync_frontend.sh
#
# A test in backend/tests/test_api.py fails if the two copies drift, so CI
# catches it as well.
# ---------------------------------------------------------------------------
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATIC_DIR="$REPO_ROOT/backend/app/static"

FILES=(
  index.html
  map.html
  styles.css
  tailwind.css
  js/api.js
  js/config.js
  js/details.js
  js/flyto.js
  js/globe.js
  js/map.js
  js/ui.js
  js/3d-viewer.js
)

mkdir -p "$STATIC_DIR/js"

for f in "${FILES[@]}"; do
  cp "$REPO_ROOT/$f" "$STATIC_DIR/$f"
done

# Remove any stale file from a previous layout, so the mirror is exact.
find "$STATIC_DIR" -type f ! -name '*.html' ! -name '*.css' ! -name '*.js' -delete

echo "Synced $((${#FILES[@]})) files -> $STATIC_DIR"
