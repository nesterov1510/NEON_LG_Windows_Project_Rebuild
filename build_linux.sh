#!/usr/bin/env bash
# Build NEON LG Control as a Linux AppImage (Ubuntu 20.04+/Debian).
# Requires: python3, pip, PyInstaller (installed by this script), appimagetool.
# Usage: ./build_linux.sh
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="NeonLGControl"
APP_DIR="dist/${APP_NAME}.AppDir"
APPIMAGE_OUT="dist/${APP_NAME}.AppImage"
ARCH="$(uname -m)"

echo "==> [1/6] Install Python dependencies"
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt pyinstaller

echo "==> [2/6] Build Linux bundle with PyInstaller"
python3 -m PyInstaller --noconfirm --clean --windowed --name "$APP_NAME" \
    --icon "assets/neon.png" \
    --add-data "ui.html:." \
    --add-data "change_region.sh:." \
    --add-data "assets:assets" \
    --exclude-module PyQt5 \
    --exclude-module PyQt5.QtWebEngineWidgets \
    --exclude-module PyQt5.QtWebEngineCore \
    --exclude-module tkinter \
    app_linux.py

echo "==> [3/6] Assemble AppDir"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/usr" "$APP_DIR/usr/share/applications" "$APP_DIR/usr/share/icons/hicolor/256x256/apps" "$APP_DIR/usr/bin"
cp -r "dist/$APP_NAME" "$APP_DIR/usr/bin/$APP_NAME"
cp "assets/neon.png" "$APP_DIR/usr/share/icons/hicolor/256x256/apps/${APP_NAME}.png"

# Desktop entry
cat > "$APP_DIR/${APP_NAME}.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=NEON / LG webOS Region Console
Comment=LG webOS region console, Samsung Tizen installer, Alem TV generator
Exec="$APP_DIR/usr/bin/$APP_NAME"
Icon=${APP_NAME}
Terminal=false
Categories=Utility;Network;
EOF

# AppRun launcher
cat > "$APP_DIR/AppRun" <<'EOF'
#!/usr/bin/env bash
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$HERE/usr/bin/NeonLGControl" "$@"
EOF
chmod +x "$APP_DIR/AppRun"

echo "==> [4/6] Download appimagetool"
AI_TOOL="$(mktemp -d)/appimagetool.AppImage"
if [ "$ARCH" = "x86_64" ]; then
    AI_URL="https://github.com/AppImage/AppImageKit/releases/download/13/obsolete-appimagetool-x86_64.AppImage"
else
    AI_URL="https://github.com/AppImage/AppImageKit/releases/download/13/obsolete-appimagetool-aarch64.AppImage"
fi
if ! curl -fsSL -o "$AI_TOOL" "$AI_URL"; then
    echo "ERROR: could not download appimagetool from $AI_URL" >&2
    exit 1
fi
chmod +x "$AI_TOOL"

echo "==> [5/6] Package AppImage"
# AppImages need FUSE to mount; on headless CI use --appimage-extract-and-run.
if ! "$AI_TOOL" --appimage-extract-and-run "$APP_DIR" -o "$APPIMAGE_OUT" 2>/dev/null; then
    "$AI_TOOL" "$APP_DIR" -o "$APPIMAGE_OUT"
fi
chmod +x "$APPIMAGE_OUT"

echo "==> [6/6] Done"
ls -lh "$APPIMAGE_OUT"
echo "Run with: ./$APPIMAGE_OUT"
