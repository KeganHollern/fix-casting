#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv"
BIN_DIR="${INSTALL_DIR:-$HOME/.local/bin}"

python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -e "$ROOT"
"$VENV/bin/playwright" install chromium

mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/cast" <<EOF
#!/usr/bin/env bash
exec "$VENV/bin/cast" "\$@"
EOF
chmod +x "$BIN_DIR/cast"

echo "Installed cast -> $BIN_DIR/cast"
echo "Make sure $BIN_DIR is in your PATH."