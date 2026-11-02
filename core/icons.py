"""单位图标包：可替换的兵种图标资源。

设计目标
--------
兵种图标原先只有内置矢量图形，玩家想换风格只能改代码。v2.1 把它变成
**可替换的资源包**：一个图标包是一个目录，里面放若干 PNG 与一份清单。

目录结构::

    icon_packs/
      my_pack/
        pack.json          清单：兵种 → 文件名 的映射
        mbt.png
        fighter.png
        ...

清单格式::

    {
      "id": "my_pack",
      "name": "我的图标包",
      "author": "someone",
      "version": 1,
      "icons": {"mbt": "mbt.png", "fighter": "fighter.png"}
    }

* `icons` 的键是 `UnitClass` 的取值（mbt / fighter / sam …）；
* 缺失的兵种自动回退到内置矢量图形——图标包不必覆盖全部 24 个兵种；
* 图标以**白色**绘制，运行时按阵营着色，这样一套图标能同时用于红蓝双方。

查找顺序
--------
1. 用户目录 `icon_packs/<id>/`（玩家自己装的包）
2. 安装目录 `data/icon_packs/<id>/`（随游戏分发的包）

用户目录优先，与语言包的处理方式一致：社区包无需改动安装目录。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .enums import UnitClass


class IconPackError(Exception):
    """图标包损坏或缺失。"""


@dataclass
class IconPack:
    """一个图标包。"""

    id: str = ""
    name: str = ""
    author: str = ""
    version: int = 1
    icons: dict[str, str] = field(default_factory=dict)   # UnitClass 值 → 文件名
    path: str = ""

    def icon_path(self, cls: UnitClass) -> str | None:
        """取该兵种的图标文件路径；没有则返回 None。"""
        fn = self.icons.get(cls.value)
        if not fn:
            return None
        p = os.path.join(self.path, fn)
        return p if os.path.isfile(p) else None


# --------------------------------------------------------------------------- #
# 目录扫描
# --------------------------------------------------------------------------- #
def _packs_root() -> str:
    from . import paths
    return os.path.join(paths.data_root(), "data", "icon_packs")


def _user_packs_root() -> str:
    from . import paths
    d = os.path.join(paths.user_writable_dir(), "icon_packs")
    os.makedirs(d, exist_ok=True)
    return d


def _load_pack_from(dir_path: str) -> IconPack:
    """从目录读取一个图标包。"""
    manifest = os.path.join(dir_path, "pack.json")
    if not os.path.isfile(manifest):
        raise IconPackError(f"图标包缺少 pack.json：{dir_path}")
    try:
        with open(manifest, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise IconPackError(f"图标包清单读取失败：{exc}") from None
    if not isinstance(raw, dict):
        raise IconPackError("图标包清单格式错误：根节点必须是对象")
    icons_raw = raw.get("icons", {})
    if not isinstance(icons_raw, dict):
        raise IconPackError("图标包清单的 icons 字段必须是对象")
    icons: dict[str, str] = {}
    for k, v in icons_raw.items():
        ks = str(k)
        if ks not in {c.value for c in UnitClass}:
            continue        # 未知兵种：忽略（包可能来自更高版本的游戏）
        if not isinstance(v, str):
            continue
        icons[ks] = v
    return IconPack(
        id=str(raw.get("id", os.path.basename(dir_path))),
        name=str(raw.get("name", os.path.basename(dir_path))),
        author=str(raw.get("author", "")),
        version=int(raw.get("version", 1) or 1),
        icons=icons,
        path=dir_path,
    )


def list_packs() -> list[IconPack]:
    """列出全部可用图标包（用户目录优先）。"""
    seen: dict[str, IconPack] = {}
    for root in (_user_packs_root(), _packs_root()):
        if not os.path.isdir(root):
            continue
        for fn in sorted(os.listdir(root)):
            d = os.path.join(root, fn)
            if not os.path.isdir(d):
                continue
            try:
                pack = _load_pack_from(d)
            except IconPackError:
                continue        # 损坏的包直接跳过，不影响游戏启动
            if pack.id not in seen:
                seen[pack.id] = pack
    return list(seen.values())


def load_pack(pack_id: str) -> IconPack:
    """按 id 加载图标包。"""
    for root in (_user_packs_root(), _packs_root()):
        d = os.path.join(root, pack_id)
        if os.path.isdir(d):
            return _load_pack_from(d)
    raise IconPackError(f"图标包不存在：{pack_id}")


def find_pack(pack_id: str) -> IconPack | None:
    """按 id 找图标包；找不到返回 None（不抛异常）。"""
    try:
        return load_pack(pack_id)
    except IconPackError:
        return None


# --------------------------------------------------------------------------- #
# 位图缓存
# --------------------------------------------------------------------------- #
# 缓存键：(pack_id, 兵种, 颜色, 尺寸)
_bitmap_cache: dict[tuple, Any] = {}
_bitmap_limit = 600

# 当前启用的图标包 ID（空 = 使用内置矢量图形）。
# 由 UI 层在启动与切换时设置；core 不读用户配置，避免 core→config→core 的循环依赖。
_current_pack_id: str = ""


def set_current_pack(pack_id: str) -> None:
    """设置当前图标包；空字符串表示使用内置矢量图形。"""
    global _current_pack_id
    pid = (pack_id or "").strip()
    if pid == _current_pack_id:
        return
    _current_pack_id = pid
    clear_cache()


def current_pack_id() -> str:
    return _current_pack_id


def _load_bitmap(path: str, size: int, color: tuple[int, int, int]):
    """读入 PNG 并按目标颜色着色。

    图标以白色绘制，用 `BLEND_RGB_MULT` 着色——这样一套素材能同时
    用于红蓝双方，换肤也不用重新导出。
    """
    import pygame

    try:
        img = pygame.image.load(path)
    except Exception:
        return None
    try:
        img = img.convert_alpha()
    except Exception:
        pass
    if size > 0 and max(img.get_width(), img.get_height()) != size:
        try:
            img = pygame.transform.smoothscale(img, (size, size))
        except Exception:
            pass
    try:
        tinted = img.copy()
        tinted.fill((*color, 255), special_flags=pygame.BLEND_RGB_MULT)
        return tinted
    except Exception:
        return img


def icon_bitmap(cls: UnitClass, color: tuple[int, int, int], size: int):
    """取某兵种在某颜色下的图标位图。

    没有可用图标包（或该包缺这个兵种）时返回 None，调用方回退到矢量图形。
    """
    pack_id = _current_pack_id
    if not pack_id:
        return None
    key = (pack_id, cls.value, tuple(color), size)
    if key in _bitmap_cache:
        return _bitmap_cache[key]
    pack = find_pack(pack_id)
    if pack is None:
        _bitmap_cache[key] = None
        return None
    path = pack.icon_path(cls)
    if path is None:
        _bitmap_cache[key] = None
        return None
    bmp = _load_bitmap(path, size, color)
    if len(_bitmap_cache) > _bitmap_limit:
        _bitmap_cache.clear()
    _bitmap_cache[key] = bmp
    return bmp


def clear_cache() -> None:
    """清空位图缓存（切换图标包或换肤后调用）。"""
    _bitmap_cache.clear()


def pack_summary(pack: IconPack) -> str:
    """一行式描述，用于设置面板。"""
    covered = len(pack.icons)
    total = len(list(UnitClass))
    who = f" · {pack.author}" if pack.author else ""
    return f"{pack.name}（{covered}/{total} 兵种{who}）"


__all__ = [
    "IconPackError", "IconPack", "list_packs", "load_pack", "find_pack",
    "icon_bitmap", "clear_cache", "pack_summary",
    "set_current_pack", "current_pack_id",
]
