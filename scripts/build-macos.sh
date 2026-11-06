#!/bin/bash
# Build SlideBridge for the CPU of this Mac (Apple Silicon -> arm64, Intel -> x64) and make a .dmg.
# PyInstaller and the Swift text renderer cannot cross-compile, so each architecture is built on its own Mac
# (the GitHub workflow uses one runner per architecture).
#   bash scripts/build-macos.sh        ->  dist/SlideBridge-<version>-<arch>.dmg
# Needs: python3, node + npm, the Xcode command line tools (swiftc, hdiutil, codesign).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
case "$(uname -m)" in
  arm64) ARCH=arm64 ;;
  x86_64) ARCH=x64 ;;
  *) echo "Unsupported CPU: $(uname -m)" >&2; exit 1 ;;
esac
VERSION="$(node -p "require('$ROOT/app/package.json').version")"
BUILD="$ROOT/build"
DIST="$ROOT/dist"
mkdir -p "$BUILD" "$DIST"
echo "== SlideBridge $VERSION for $ARCH"

echo "== 1/5 Python environment"
python3 -m venv "$BUILD/venv"
"$BUILD/venv/bin/python" -m pip install --quiet --upgrade pip
"$BUILD/venv/bin/python" -m pip install --quiet -r "$ROOT/engine/requirements.txt" "pyinstaller>=6.10"

echo "== 2/5 Text renderer (Swift, compiled once so the app needs no Xcode tools)"
"$BUILD/venv/bin/python" -c "import sys; sys.path.insert(0, '$ROOT/engine'); import pp2mp4; open('$BUILD/rtf2png.swift', 'w').write(pp2mp4.SWIFT_SRC)"
swiftc -O "$BUILD/rtf2png.swift" -o "$BUILD/rtf2png"

echo "== 3/5 Freeze the engine (Python, numpy, Pillow, ffmpeg inside)"
"$BUILD/venv/bin/pyinstaller" --noconfirm --onedir --name pp2mp4-engine \
  --distpath "$BUILD/engine-dist" --workpath "$BUILD/pyi-work" --specpath "$BUILD" \
  --collect-all imageio_ffmpeg --exclude-module tkinter "$ROOT/engine/pp2mp4.py"

echo "== 4/5 Electron app"
cd "$ROOT/app"
npm ci
./node_modules/.bin/electron-packager . SlideBridge --platform=darwin --arch="$ARCH" \
  --out "$BUILD/app-out" --overwrite \
  --icon assets/icon.icns --app-bundle-id io.github.tchandrap.slidebridge --app-version "$VERSION" \
  --extra-resource="$BUILD/engine-dist/pp2mp4-engine" --extra-resource="$BUILD/rtf2png"
APP="$BUILD/app-out/SlideBridge-darwin-$ARCH/SlideBridge.app"
codesign --force --deep --sign - "$APP"        # ad-hoc signature only: not notarized (no Apple developer account)

echo "== 5/5 .dmg"
STAGE="$(mktemp -d "$BUILD/dmg-stage.XXXXXX")"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
DMG="$DIST/SlideBridge-$VERSION-$ARCH.dmg"
hdiutil create -volname "SlideBridge $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
echo "== Done: $DMG"
echo "$APP" > "$BUILD/last-app-path.txt"
