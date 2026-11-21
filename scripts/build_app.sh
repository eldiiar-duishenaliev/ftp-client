#!/bin/bash
# Build a double-clickable .app wrapper around this checkout.
#
# The bundle uses the project's virtualenv, so it is a launcher rather than a
# standalone distribution: moving this folder breaks the app. Build a signed
# standalone bundle with PyInstaller if you need to ship it elsewhere.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
APP="$ROOT/dist/FTP Client.app"
VERSION="1.0.0"

if [ ! -x "$PY" ]; then
    echo "No virtualenv found. Run ./run.sh once, then build again." >&2
    exit 1
fi

if ! "$PY" -c "import paramiko, keyring" >/dev/null 2>&1; then
    echo "Installing dependencies…"
    "$PY" -m pip install -r "$ROOT/requirements.txt"
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>FTP Client</string>
    <key>CFBundleDisplayName</key>
    <string>FTP Client</string>
    <key>CFBundleIdentifier</key>
    <string>local.ftpclient.app</string>
    <key>CFBundleVersion</key>
    <string>${VERSION}</string>
    <key>CFBundleShortVersionString</key>
    <string>${VERSION}</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleExecutable</key>
    <string>FTPClient</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>LSApplicationCategoryType</key>
    <string>public.app-category.utilities</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSPrincipalClass</key>
    <string>NSApplication</string>
</dict>
</plist>
PLIST

if "$PY" -c "import PIL" >/dev/null 2>&1; then
    ICON_TMP="$(mktemp -d)"
    ICNS="$("$PY" "$ROOT/scripts/make_icon.py" "$ICON_TMP" | tail -n 1)"
    if [ -f "$ICNS" ]; then
        cp "$ICNS" "$APP/Contents/Resources/AppIcon.icns"
    fi
    rm -rf "$ICON_TMP"
fi

cat > "$APP/Contents/MacOS/FTPClient" <<LAUNCHER
#!/bin/bash
cd "$ROOT" || exit 1
exec "$PY" -m ftpclient "\$@"
LAUNCHER
chmod +x "$APP/Contents/MacOS/FTPClient"

# Refresh LaunchServices so Finder picks up the icon and name immediately.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "$APP" >/dev/null 2>&1 || true

echo "Built: $APP"
echo "Open it with: open \"$APP\""
