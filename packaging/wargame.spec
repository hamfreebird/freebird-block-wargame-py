# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：生成免安装可执行文件（Windows / Linux 通用）。

用法（在项目根目录）：
    python -m PyInstaller packaging/wargame.spec --noconfirm

产物：
    dist/BlockWargame/            Windows（onedir，含全部依赖）
    dist/BlockWargame-linux/      Linux（onedir）

注意：可执行文件名用 ASCII（BlockWargame），窗口标题仍是中文，
避免部分打包工具与 CI 制品路径对非 ASCII 名称处理不一致。
"""
import os
import sys

SPECPATH = os.path.abspath(SPECPATH)          # PyInstaller 注入：spec 所在目录
ROOT = os.path.dirname(SPECPATH)
IS_WIN = sys.platform.startswith("win")

# 图标：Windows 用 .ico，Linux 用 PNG
icon_path = os.path.join(ROOT, "assets", "icon",
                          "icon.ico" if IS_WIN else "icon_256.png")

datas = [
    (os.path.join(ROOT, "data"), "data"),            # 想定与武器库
    (os.path.join(ROOT, "assets"), "assets"),        # 图标资源
]

hiddenimports = [
    "pygame",
    "pygame.freetype",
    "pygame.gfxdraw",
    "pygame.image",
    "pygame.display",
    "pygame.font",
    "pygame.mixer",
]

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "PIL"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe_kwargs = dict(
    name="BlockWargame",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,               # GUI 程序，不弹控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path if os.path.exists(icon_path) else None,
)

if IS_WIN:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **exe_kwargs)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False,
                    name="BlockWargame")
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
              runtime_tmpdir=None,
              exclude_binaries=False,   # Linux 用 onefile，便于分发
              **exe_kwargs)
