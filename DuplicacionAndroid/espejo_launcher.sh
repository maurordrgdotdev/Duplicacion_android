#!/usr/bin/env bash
set -euo pipefail
# Lanza el binario scrcpy con rutas del .app principal (ADB, servidor, icono, dylibs).
DIR="$(cd -P "$(dirname "$0")" && pwd)"
MAIN_CONTENTS="$(cd -P "$DIR/../../../.." && pwd)"
export ADB="$MAIN_CONTENTS/Resources/bundled/adb"
export PATH="$(dirname "$ADB"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"
export SCRCPY_SERVER_PATH="$MAIN_CONTENTS/Resources/bundled/scrcpy-server"
export DYLD_LIBRARY_PATH="/opt/homebrew/lib:/usr/local/lib:${DYLD_LIBRARY_PATH:-}"
ICON="$MAIN_CONTENTS/Resources/DuplicacionAndroidIcon.png"
if [[ -f "$ICON" ]]; then
	export SCRCPY_ICON_PATH="$ICON"
fi
exec "$DIR/DuplicacionAndroidMirror" "$@"
