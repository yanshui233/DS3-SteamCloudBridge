#!/usr/bin/env sh
# ============================================================
#  SteamCloudBridge - one-click machine probe (read-only)
#  Runs the portable probe and writes reports into reports/
#  Requires: python3 (3.9+)
# ============================================================
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
ENTRY="$ROOT/tools/machine_probe.py"

# 单文件发行版优先 (不需要 clone 整个仓库)
if [ -f "$ROOT/dist/SteamCloudBridgeProbe.py" ]; then
  ENTRY="$ROOT/dist/SteamCloudBridgeProbe.py"
fi

if [ ! -f "$ENTRY" ]; then
  echo "[ERROR] probe entry not found: $ENTRY" >&2
  exit 2
fi

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "[ERROR] python3 not found on PATH." >&2
  echo "Install Python 3.9+ first." >&2
  exit 2
fi

set +e
"$PY" "$ENTRY" "$@"
RC=$?
set -e

echo
if [ "$RC" -ne 0 ]; then
  echo "[exit $RC] something went wrong, see messages above." >&2
  exit "$RC"
fi

echo "Done. Reports are in: $ROOT/reports"
echo "  machine_profile.json        -- full, keep local only"
echo "  machine_profile.share.json  -- redacted, safe to share"
echo "  machine_profile.md          -- human readable summary"
