#!/bin/sh
# Build "PR Reviewer.app". Pass --install to also copy it into /Applications.
set -eu

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
BUILD="$ROOT/build"
APP="$BUILD/PR Reviewer.app"
BUNDLE_ID="com.danielfan.pr-reviewer"
VERSION="0.1.0"

INSTALL=0
[ "${1:-}" = "--install" ] && INSTALL=1

command -v swiftc >/dev/null 2>&1 || { echo "error: swiftc not found (install Xcode Command Line Tools)" >&2; exit 1; }

echo "==> cleaning"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "==> icon"
python3 packaging/make_icon.py "$BUILD"
cp "$BUILD/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

echo "==> payload"
mkdir -p "$APP/Contents/Resources/app"
cp -R src static pyproject.toml uv.lock "$APP/Contents/Resources/app/"
# strip caches that would otherwise be signed into the bundle
find "$APP/Contents/Resources/app" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
find "$APP/Contents/Resources/app" -name '*.pyc' -delete 2>/dev/null || true

echo "==> compiling launcher"
swiftc -O -swift-version 5 \
    -target arm64-apple-macos13.0 \
    -o "$APP/Contents/MacOS/PRReviewer" \
    packaging/main.swift \
    -framework Cocoa -framework WebKit

echo "==> Info.plist"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>PR Reviewer</string>
    <key>CFBundleDisplayName</key><string>PR Reviewer</string>
    <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
    <key>CFBundleExecutable</key><string>PRReviewer</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundleVersion</key><string>$VERSION</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSHumanReadableCopyright</key><string>Local tool — runs entirely on this Mac.</string>
    <key>NSAppTransportSecurity</key>
    <dict>
        <key>NSAllowsLocalNetworking</key><true/>
    </dict>
</dict>
</plist>
PLIST

echo "==> signing (ad-hoc)"
codesign --force --deep --sign - "$APP" 2>/dev/null \
    || echo "   warning: ad-hoc signing failed; app may need a Gatekeeper override on first open"

echo "==> built: $APP"
du -sh "$APP" | awk '{print "    size: " $1}'

if [ "$INSTALL" -eq 1 ]; then
    echo "==> installing to /Applications"
    rm -rf "/Applications/PR Reviewer.app"
    cp -R "$APP" /Applications/
    echo "    installed: /Applications/PR Reviewer.app"
fi
