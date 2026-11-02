"""资源路径解析：兼容源码运行、PyInstaller 冻结、zipapp 便携包三种形态。

zipapp（.pyz）内的数据文件不能直接用文件系统路径读取，
因此启动时把 data/ 与 assets/ 释放到用户数据目录，再以普通目录方式使用——
顺带让地图编辑器在便携版里也能正常保存想定。

用法：
    from core import paths
    ROOT = paths.data_root()
"""
from __future__ import annotations

import hashlib
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


def _file_hash(path: str) -> str:
    """计算文件内容的 MD5（用于判断数据文件是否被玩家改动）。"""
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _extract_from_archive(archive: str, dest: str) -> bool:
    """把归档内的 data/ 与 assets/ 释放到 dest。

    采用**按内容哈希的增量提取**。`.extracted` 清单记录上次释放时每个文件的
    内容哈希，据此区分两种情况：

    * 目标文件与上次释放的哈希一致 → 玩家没改过 → 可安全地用新版本覆盖
      （归档升级后同名数据文件的修复能生效）；
    * 目标文件哈希与上次释放的不一致 → 玩家改过 → **保留玩家版本**；
    * 清单里没有的新文件 → 直接释放（升级便携版后新增的想定能正常出现）。

    因此：升级后新增数据能补齐，旧数据能更新，玩家本地修改不会被覆盖。
    """
    marker = os.path.join(dest, ".extracted")
    os.makedirs(dest, exist_ok=True)

    try:
        with zipfile.ZipFile(archive, "r") as z:
            members = [n for n in z.namelist()
                       if n.startswith(("data/", "assets/")) and not n.endswith("/")]

            # 上次释放的清单：{相对路径: 内容哈希}
            old: dict[str, str] = {}
            if os.path.isfile(marker):
                try:
                    with open(marker, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line or " " not in line:
                                continue
                            name, digest = line.rsplit(" ", 1)
                            old[name] = digest
                except (OSError, ValueError):
                    old = {}

            new: dict[str, str] = {}
            for name in members:
                data = z.read(name)
                digest = hashlib.md5(data).hexdigest()
                new[name] = digest
                target = os.path.join(dest, name.replace("/", os.sep))

                if os.path.isfile(target):
                    prev = old.get(name)
                    if prev is not None:
                        if _file_hash(target) != prev:
                            # 玩家改过这个文件 → 保留玩家版本
                            continue
                    elif _file_hash(target) == digest:
                        # 清单缺失但内容与归档一致 → 无需重写
                        continue

                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as dst:
                    dst.write(data)

            # 记录本次清单
            with open(marker, "w", encoding="utf-8") as f:
                for name in sorted(new):
                    f.write(f"{name} {new[name]}\n")
            return True
    except (OSError, zipfile.BadZipFile, KeyError):
        # 提取失败时保留已有文件，由调用方决定是否回退到项目根目录
        return False


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


def lang_dir() -> str:
    """语言包目录（`data/lang/<code>.json`）。"""
    return os.path.join(data_root(), "data", "lang")


def user_lang_dir() -> str:
    """用户自定义语言包目录（可放社区翻译，不影响安装目录）。"""
    d = os.path.join(user_writable_dir(), "lang")
    os.makedirs(d, exist_ok=True)
    return d


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
