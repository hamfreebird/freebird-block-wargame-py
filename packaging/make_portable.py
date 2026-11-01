#!/usr/bin/env python3
"""生成「零依赖便携包」与「源码包」。

只用标准库，任何环境都能跑（不需要 PyInstaller）：

    python packaging/make_portable.py

产物（输出到 packaging/dist/）：
    方块兵棋-portable.pyz     zipapp 单文件，双击/命令行均可运行
    方块兵棋-source.zip       源码包（含数据与图标）

zipapp 需要目标机器已安装 Python 3.10+ 与 pygame-ce；
免安装可执行文件请用 PyInstaller（见 packaging/build_windows.ps1 / build_linux.sh）。
"""
from __future__ import annotations

import os
import shutil
import sys
import zipapp
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "packaging", "dist")
NAME = "方块兵棋"
ENTRY = "main.py"

# 便携版需要打进去的包与资源
INCLUDE_DIRS = ("core", "ui")
INCLUDE_FILES = ("main.py",)
INCLUDE_DATA = (("data", "data"), ("assets", "assets"))


def build_pyapp() -> str:
    """生成 zipapp 单文件（.pyz）。"""
    out = os.path.join(DIST, f"{NAME}-portable.pyz")
    os.makedirs(DIST, exist_ok=True)
    if os.path.exists(out):
        os.remove(out)

    tmp = os.path.join(DIST, "_pyapp_build")
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)

    for d in INCLUDE_DIRS:
        shutil.copytree(os.path.join(ROOT, d), os.path.join(tmp, d),
                        ignore=shutil.ignore_patterns("__pycache__"))
    for f in INCLUDE_FILES:
        shutil.copy2(os.path.join(ROOT, f), os.path.join(tmp, f))
    for src, dst in INCLUDE_DATA:
        s = os.path.join(ROOT, src)
        if os.path.exists(s):
            shutil.copytree(s, os.path.join(tmp, dst),
                            ignore=shutil.ignore_patterns("__pycache__"))

    # 启动器：让 python main.py 也能识别为包入口
    with open(os.path.join(tmp, "__main__.py"), "w", encoding="utf-8") as f:
        f.write("import main\nmain.main()\n")

    zipapp.create_archive(tmp, target=out,
                          interpreter="/usr/bin/env python3",
                          compressed=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return out


def build_source_zip() -> str:
    """生成源码包（含数据、图标、文档与工具）。"""
    out = os.path.join(DIST, f"{NAME}-source.zip")
    os.makedirs(DIST, exist_ok=True)
    if os.path.exists(out):
        os.remove(out)

    skip_dirs = {"__pycache__", ".git", ".workbuddy", ".shots", "saves",
                 "build", "dist", ".venv", "venv", "__pycache__"}
    skip_files = {"*.pyc", "*.pyo", "*.bak", "*.log"}

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for base, dirs, files in os.walk(ROOT):
            rel_base = os.path.relpath(base, ROOT)
            if rel_base == ".":
                rel_base = ""
            parts = set(rel_base.split(os.sep)) if rel_base else set()
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for fn in files:
                if any(fn.endswith(ext.lstrip("*")) for ext in skip_files):
                    continue
                full = os.path.join(base, fn)
                rel = os.path.join(rel_base, fn) if rel_base else fn
                z.write(full, os.path.join(NAME, rel))
    return out


def main() -> int:
    pyz = build_pyapp()
    src = build_source_zip()
    for p in (pyz, src):
        print(f"  {os.path.relpath(p, ROOT)}  ({os.path.getsize(p) / 1024:.0f} KB)")
    print("提示：便携版运行需要目标机器已安装 Python 3.10+ 与 pygame-ce")
    return 0


if __name__ == "__main__":
    sys.exit(main())
