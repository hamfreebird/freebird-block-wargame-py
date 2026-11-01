#!/usr/bin/env bash
# Linux 打包脚本：生成免安装 tar.gz（可选 AppImage）与便携包。
#
# 用法（项目根目录）：
#     bash packaging/build_linux.sh
#
# 产物：packaging/dist/fangkuai-bingqi-linux-x86_64.tar.gz
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DIST="$ROOT/packaging/dist"
BUILD="$ROOT/packaging/build"
NAME="fangkuai-bingqi"

echo "==> 1/5 check dependencies"
python3 --version
python3 -m pip install --upgrade pip >/dev/null
python3 -m pip install -r requirements.txt
python3 -m pip install pyinstaller

echo "==> 2/5 generate icons"
python3 tools/make_icon.py >/dev/null

echo "==> 3/5 build with PyInstaller (onefile)"
# 显式指定输出目录：PyInstaller 默认写到 CWD 下的 dist/，与打包目录不一致
python3 -m PyInstaller packaging/wargame.spec --noconfirm --clean \
        --distpath "$DIST" --workpath "$BUILD"

BIN="$DIST/BlockWargame"
if [ ! -f "$BIN" ]; then
  echo "packaged output not found: $BIN" >&2
  exit 1
fi
chmod +x "$BIN"

echo "==> 4/5 verify packaged executable"
# 打包后的程序必须能读到内置数据（验证 data/ 是否正确打入）
if ! SDL_VIDEODRIVER=dummy "$BIN" --check >/dev/null 2>&1; then
  echo "packaged self-check failed" >&2
  exit 1
fi
echo "    packaged self-check passed"

echo "==> 5/5 collect artifacts"
rm -rf "$DIST/stage"
STAGE="$DIST/stage"
mkdir -p "$STAGE/$NAME"
cp -a "$BIN" "$STAGE/$NAME/"
cp -f README.md GAME_GUIDE.md LICENSE "$STAGE/$NAME/" 2>/dev/null || true

cat > "$STAGE/$NAME/run.sh" <<'LAUNCH'
#!/usr/bin/env bash
# 方块兵棋 启动脚本
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
chmod +x BlockWargame 2>/dev/null || true
if [ -x "$DIR/BlockWargame" ]; then
  exec "$DIR/BlockWargame" "$@"
fi
echo "Packaged executable not found, falling back to source mode (needs pygame-ce)"
exec python3 "$DIR/main.py" "$@"
LAUNCH
chmod +x "$STAGE/$NAME/run.sh"

# 桌面快捷方式与图标
mkdir -p "$STAGE/$NAME/.desktop"
cp "$ROOT/assets/icon/icon_256.png" "$STAGE/$NAME/.desktop/icon.png"
cat > "$STAGE/$NAME/$NAME.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Block Wargame
Comment=Block Wargame
Exec=sh -c 'cd "$(dirname "%k")" && ./BlockWargame'
Icon=icon.png
Terminal=false
Categories=Game;StrategyGame;
DESKTOP

TARBALL="$DIST/$NAME-linux-x86_64.tar.gz"
rm -f "$TARBALL"
tar -czf "$TARBALL" -C "$STAGE" "$NAME"
rm -rf "$STAGE"

# 可选：生成 AppImage（需要 appimagetool）
if command -v appimagetool >/dev/null 2>&1; then
  echo "==> extra: AppImage"
  APPDIR="$DIST/AppDir"
  rm -rf "$APPDIR"; mkdir -p "$APPDIR"
  cp -a "$BIN" "$APPDIR/"
  cp "$ROOT/assets/icon/icon_256.png" "$APPDIR/icon.png"
  cp "$ROOT/assets/icon/icon.svg" "$APPDIR/icon.svg"
  cat > "$APPDIR/AppRun" <<'APPRUN'
#!/usr/bin/env bash
HERE="$(dirname "$(readlink -f "${0}")")"
exec "$HERE/BlockWargame" "$@"
APPRUN
  chmod +x "$APPDIR/AppRun"
  cat > "$APPDIR/$NAME.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Block Wargame
Exec=AppRun
Icon=icon
Terminal=false
Categories=Game;
DESKTOP
  appimagetool "$APPDIR" "$DIST/$NAME-linux-x86_64.AppImage" || echo "  AppImage generation skipped"
  rm -rf "$APPDIR"
fi

echo "==> extra: portable build and source archive"
python3 packaging/make_portable.py

echo
echo "Build finished:"
ls -lh "$DIST" | tail -n +2 | awk '{printf "  %-44s %s\n", $9, $5}'
