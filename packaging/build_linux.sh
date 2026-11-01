#!/usr/bin/env bash
# Linux 打包脚本：生成免安装 tar.gz（可选 AppImage）与便携包。
#
# 用法（项目根目录）：
#     bash packaging/build_linux.sh
#
# 产物：packaging/dist/方块兵棋-linux-x86_64.tar.gz
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DIST="$ROOT/packaging/dist"
VERSION="$(python -c "import tomllib,sys;print('1.0.0')" 2>/dev/null || echo 1.0.0)"

echo "==> 1/5 检查依赖"
python3 --version
python3 -m pip install --upgrade pip >/dev/null
python3 -m pip install -r requirements.txt
python3 -m pip install pyinstaller

echo "==> 2/5 生成图标"
python3 tools/make_icon.py >/dev/null

echo "==> 3/5 PyInstaller 打包（onefile）"
python3 -m PyInstaller packaging/wargame.spec --noconfirm --clean

echo "==> 4/5 收集产物"
mkdir -p "$DIST"
STAGE="$DIST/stage"
rm -rf "$STAGE"
mkdir -p "$STAGE/方块兵棋"
cp -r "$DIST/BlockWargame" "$STAGE/方块兵棋/" 2>/dev/null || cp "$DIST/BlockWargame" "$STAGE/方块兵棋/"
cp README.md GAME_GUIDE.md "$STAGE/方块兵棋/" 2>/dev/null || true

cat > "$STAGE/方块兵棋/run.sh" <<'LAUNCH'
#!/usr/bin/env bash
# 方块兵棋 启动脚本
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
chmod +x BlockWargame 2>/dev/null || true
if [ -x "$DIR/BlockWargame" ]; then
  exec "$DIR/BlockWargame" "$@"
fi
echo "未找到可执行文件，回退到源码方式运行（需已安装 pygame-ce）"
exec python3 "$DIR/main.py" "$@"
LAUNCH
chmod +x "$STAGE/方块兵棋/run.sh"

# 桌面快捷方式与图标
mkdir -p "$STAGE/方块兵棋/.desktop"
cp "$ROOT/assets/icon/icon_256.png" "$STAGE/方块兵棋/.desktop/icon.png"
cat > "$STAGE/方块兵棋/方块兵棋.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=方块兵棋
Comment=现代战争方块兵棋
Exec=sh -c 'cd "$(dirname "%k")" && ./BlockWargame'
Icon=icon.png
Terminal=false
Categories=Game;StrategyGame;
DESKTOP

echo "==> 5/5 压缩"
TARBALL="$DIST/方块兵棋-linux-x86_64.tar.gz"
rm -f "$TARBALL"
tar -czf "$TARBALL" -C "$STAGE" 方块兵棋
rm -rf "$STAGE"

# 可选：生成 AppImage（需要 appimagetool）
if command -v appimagetool >/dev/null 2>&1; then
  echo "==> 附加：AppImage"
  APPDIR="$DIST/AppDir"
  rm -rf "$APPDIR"; mkdir -p "$APPDIR"
  cp -r "$DIST/BlockWargame" "$APPDIR/" 2>/dev/null || cp "$DIST/BlockWargame" "$APPDIR/"
  cp "$ROOT/assets/icon/icon_256.png" "$APPDIR/icon.png"
  cp "$ROOT/assets/icon/icon.svg" "$APPDIR/icon.svg"
  cat > "$APPDIR/AppRun" <<'APPRUN'
#!/usr/bin/env bash
HERE="$(dirname "$(readlink -f "${0}")")"
exec "$HERE/BlockWargame" "$@"
APPRUN
  chmod +x "$APPDIR/AppRun"
  cat > "$APPDIR/方块兵棋.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=方块兵棋
Exec=AppRun
Icon=icon
Terminal=false
Categories=Game;
DESKTOP
  appimagetool "$APPDIR" "$DIST/方块兵棋-linux-x86_64.AppImage" || echo "  AppImage 生成失败（已跳过）"
  rm -rf "$APPDIR"
fi

echo "==> 附加：便携版与源码包"
python3 packaging/make_portable.py

echo
echo "打包完成："
ls -lh "$DIST" | tail -n +2 | awk '{printf "  %-44s %s\n", $9, $5}'
