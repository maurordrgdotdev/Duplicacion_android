#!/usr/bin/env bash
set -euo pipefail

# Empaqueta ../DuplicacionAndroid.py → DuplicacionAndroid.app (PyInstaller, sin Terminal).

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
SRC="$REPO_ROOT/DuplicacionAndroid.py"
VENV="$HERE/.venv"
STAGING="$HERE/staging"
APP="$HERE/DuplicacionAndroid.app"
PI_APP="$STAGING/dist/DuplicacionAndroid.app"

if [[ ! -f "$SRC" ]]; then
	echo "No existe el script fuente: $SRC" >&2
	exit 1
fi

echo "→ Creando venv e instalando PyInstaller…"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q pyinstaller pillow

rm -rf "$STAGING" "$APP"
mkdir -p "$STAGING/work"

echo "→ Icono AppIcon.icns + PNG para scrcpy…"
MASTER="$STAGING/icon_master.png"
"$VENV/bin/python" "$HERE/make_icon.py" "$MASTER"
ICONSET="$STAGING/AppIcon.iconset"
mkdir "$ICONSET"
sips -z 16 16 "$MASTER" --out "$ICONSET/icon_16x16.png" >/dev/null
sips -z 32 32 "$MASTER" --out "$ICONSET/icon_16x16@2x.png" >/dev/null
sips -z 32 32 "$MASTER" --out "$ICONSET/icon_32x32.png" >/dev/null
sips -z 64 64 "$MASTER" --out "$ICONSET/icon_32x32@2x.png" >/dev/null
sips -z 128 128 "$MASTER" --out "$ICONSET/icon_128x128.png" >/dev/null
sips -z 256 256 "$MASTER" --out "$ICONSET/icon_128x128@2x.png" >/dev/null
sips -z 256 256 "$MASTER" --out "$ICONSET/icon_256x256.png" >/dev/null
sips -z 512 512 "$MASTER" --out "$ICONSET/icon_256x256@2x.png" >/dev/null
sips -z 512 512 "$MASTER" --out "$ICONSET/icon_512x512.png" >/dev/null
sips -z 1024 1024 "$MASTER" --out "$ICONSET/icon_512x512@2x.png" >/dev/null
(
	cd "$STAGING"
	iconutil -c icns AppIcon.iconset
)

echo "→ PyInstaller (app bundle, sin consola, tkinter)…"
(
	cd "$HERE"
	"$VENV/bin/pyinstaller" \
		--name DuplicacionAndroid \
		--windowed \
		--clean \
		--noconfirm \
		--osx-bundle-identifier com.munakdigitall.DuplicacionAndroid \
		--collect-all tkinter \
		--specpath "$STAGING" \
		--distpath "$STAGING/dist" \
		--workpath "$STAGING/work" \
		"$SRC"
)

if [[ ! -d "$PI_APP" ]]; then
	echo "PyInstaller no generó: $PI_APP" >&2
	exit 1
fi

cp -R "$PI_APP" "$APP"
cp "$HERE/Info.plist" "$APP/Contents/Info.plist"
cp "$STAGING/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cp "$MASTER" "$APP/Contents/Resources/DuplicacionAndroidIcon.png"
rm -f "$APP/Contents/Resources/icon-windowed.icns" 2>/dev/null || true

BUNDLED="$APP/Contents/Resources/bundled"
mkdir -p "$BUNDLED"

ADB_SRC=""
if [[ -n "${ADB_PATH:-}" && -f "${ADB_PATH}" ]]; then
	ADB_SRC="${ADB_PATH}"
elif command -v adb >/dev/null 2>&1; then
	ADB_SRC="$(command -v adb)"
elif [[ -n "${ANDROID_HOME:-}" && -f "${ANDROID_HOME}/platform-tools/adb" ]]; then
	ADB_SRC="${ANDROID_HOME}/platform-tools/adb"
elif [[ -f "${HOME}/Library/Android/sdk/platform-tools/adb" ]]; then
	ADB_SRC="${HOME}/Library/Android/sdk/platform-tools/adb"
fi

if [[ -n "$ADB_SRC" ]]; then
	cp "$ADB_SRC" "$BUNDLED/adb"
	chmod +x "$BUNDLED/adb"
	echo "→ Incluido adb: $ADB_SRC"
else
	echo "AVISO: no se encontró adb para empaquetar. Define ADB_PATH o instala Android platform-tools." >&2
fi

NEST_ROOT="$APP/Contents/Frameworks/DuplicacionAndroidEspejo.app/Contents"
if command -v brew >/dev/null 2>&1 && brew --prefix scrcpy >/dev/null 2>&1; then
	SCPF="$(brew --prefix scrcpy)"
	mkdir -p "$NEST_ROOT/MacOS"
	cp "$HERE/espejo_launcher.sh" "$NEST_ROOT/MacOS/DuplicacionEspejo"
	chmod +x "$NEST_ROOT/MacOS/DuplicacionEspejo"
	cp "$SCPF/bin/scrcpy" "$NEST_ROOT/MacOS/DuplicacionAndroidMirror"
	chmod +x "$NEST_ROOT/MacOS/DuplicacionAndroidMirror"
	cp "$HERE/MirrorBundle.Info.plist" "$NEST_ROOT/Info.plist"
	cp "$SCPF/share/scrcpy/scrcpy-server" "$BUNDLED/scrcpy-server"
	chmod +x "$BUNDLED/scrcpy-server"
	rm -f "$APP/Contents/MacOS/DuplicacionAndroidMirror" 2>/dev/null || true
	echo "→ Espejo en Frameworks/DuplicacionAndroidEspejo.app (LSUIElement) + scrcpy-server en bundled."
else
	echo "AVISO: sin «brew --prefix scrcpy» no se empaqueta el espejo." >&2
fi

echo "→ Listo: $APP"
echo "  Instancia única: puerto local 49819; espejo sin icono propio en Dock (LSUIElement)."
