"""资源路径解析：兼容源码运行、PyInstaller 冻结、zipapp 便携包三种形态。

zipapp（.pyz）内的数据文件不能直接用文件系统路径读取，
因此启动时把 data/ 与 assets/ 释放到用户数据目录，再以普通目录方式使用——
顺带让地图编辑器在便携版里也能正常保存想定。

用法：
    from core import paths
    ROOT = paths.data_root()
"""
from __future__ import annotations

import os
import sys
import zipfile

# 源码布局下的项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _user_data_dir() -> str:
    """按平台返回用户数据目录。"""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "BlockWargame")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library",
                            "Application Support", "BlockWargame")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "blockwargame")


def _archive_path() -> str | None:
    """检测当前是否运行在 zipapp / zip 内，返回归档文件路径。"""
    loader = globals().get("__loader__")
    archive = getattr(loader, "archive", None)
    if archive and os.path.exists(archive):
        return archive
    return None


def _extract_from_archive(archive: str, dest: str) -> None:
    """把归档内的 data/ 与 assets/ 释放到 dest（已存在则跳过）。"""
    marker = os.path.join(dest, ".extracted")
    if os.path.exists(marker):
        return
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(archive, "r") as z:
        for name in z.namelist():
            if name.startswith(("data/", "assets/")) and not name.endswith("/"):
                target = os.path.join(dest, name.replace("/", os.sep))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(name) as src, open(target, "wb") as dst:
                    dst.write(src.read())
    with open(marker, "w", encoding="utf-8") as f:
        f.write("ok\n")


def data_root() -> str:
    """返回包含 data/ 与 assets/ 的可写根目录。"""
    if getattr(sys, "frozen", False):
        # PyInstaller：资源在 _MEIPASS（只读），想定另存到用户目录
        return getattr(sys, "_MEIPASS", PROJECT_ROOT)

    archive = _archive_path()
    if archive:
        dest = _user_data_dir()
        try:
            _extract_from_archive(archive, dest)
            return dest
        except Exception:
            return PROJECT_ROOT

    return PROJECT_ROOT


def user_writable_dir() -> str:
    """用户可写目录（存想定、存档、截图）。"""
    d = _user_data_dir()
    os.makedirs(d, exist_ok=True)
    return d


def maps_dir() -> str:
    return os.path.join(data_root(), "data", "maps")


def armory_path() -> str:
    return os.path.join(data_root(), "data", "units", "armory.json")


def icon_dir() -> str:
    return os.path.join(data_root(), "assets", "icon")


def saves_dir() -> str:
    d = os.path.join(user_writable_dir(), "saves")
    os.makedirs(d, exist_ok=True)
    return d


def user_config_path() -> str:
    """用户配置文件（键位自定义与界面偏好）的存放位置。"""
    return os.path.join(user_writable_dir(), "user_config.json")


def reports_dir() -> str:
    """战报导出目录。"""
    d = os.path.join(user_writable_dir(), "reports")
    os.makedirs(d, exist_ok=True)
    return d
