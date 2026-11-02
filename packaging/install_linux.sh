#!/usr/bin/env bash
set -euo pipefail
TOOLKIT_PACKAGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLKIT_INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/docs-utilz"
TOOLKIT_DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
install -d "$TOOLKIT_INSTALL_DIR" "$TOOLKIT_DESKTOP_DIR"
install -m 755 "$TOOLKIT_PACKAGE_DIR/Docs-Utilz" "$TOOLKIT_INSTALL_DIR/Docs-Utilz"
install -m 644 "$TOOLKIT_PACKAGE_DIR/icon.png" "$TOOLKIT_INSTALL_DIR/icon.png"
cat > "$TOOLKIT_DESKTOP_DIR/docs-utilz.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Docs-Utilz
Comment=Clean, resize, convert, scan and print documents
Exec="$TOOLKIT_INSTALL_DIR/Docs-Utilz"
Icon=$TOOLKIT_INSTALL_DIR/icon.png
Terminal=false
Categories=Office;Graphics;
EOF
chmod 644 "$TOOLKIT_DESKTOP_DIR/docs-utilz.desktop"
printf 'Installed Docs-Utilz to %s\nOpen it from the applications menu. No Python installation is needed.\n' "$TOOLKIT_INSTALL_DIR"
