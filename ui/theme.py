"""视觉主题：配色方案、字体缓存、文本渲染。

支持多套配色，可在运行时切换（设置项 → 界面主题）：

* `tactical` 深色战术终端（默认）——低饱和地形色 + 高对比阵营色；
* `light` 亮色简报风——适合白天、大屏投影；
* `deuteranopia` / `protanopia` 色盲友好——在战术主题基础上
  改用蓝黄配色区分敌我，避免红绿混淆。

**实现方式**：颜色不再用模块级常量绑定，而是通过模块级 `__getattr__`
动态解析到当前主题。这样 `T.TEXT` 之类的调用点无需改动，切换主题后
立即生效。

注意：函数默认参数在**定义时**求值，因此不要在默认参数里写 `T.TEXT`——
那样颜色会被固定。需要默认色时请用 `None`，在函数体内再取。
"""
from __future__ import annotations

import contextlib
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache

import pygame

from core.enums import Side, Terrain

RGB = tuple[int, int, int]


# --------------------------------------------------------------------------- #
# 主题定义
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Theme:
    """一套完整的配色方案。"""

    id: str
    name: str
    bg: RGB
    bg_map: RGB
    panel: RGB
    panel_hi: RGB
    panel_sunk: RGB
    border: RGB
    border_hi: RGB
    grid: RGB
    text: RGB
    text_dim: RGB
    text_faint: RGB
    cyan: RGB
    blue: RGB
    red: RGB
    amber: RGB
    green: RGB
    purple: RGB
    white: RGB
    #: 地图层专用：目标点名称（压在**地形**上，不是面板上）。
    #: 以前这两处写死在 mapview 里，浅色主题下对比度掉到 1.1:1——等于没画。
    map_label: RGB
    #: 地图层专用：中立设施符号（哨所/港口等的小图标）。
    map_symbol: RGB
    #: 侦测等级「已识别」的语义色（信息卡 / 面板背景上用）。
    detect_classified: RGB
    side_blue: dict = field(default_factory=dict)
    side_red: dict = field(default_factory=dict)
    side_neutral: dict = field(default_factory=dict)
    terrain: dict = field(default_factory=dict)
    # 环境色调的基准（昼夜叠加用）
    night_tint: RGB = (12, 20, 46)
    day_tint: RGB = (255, 244, 210)


def _side(fill: RGB, edge: RGB, text: RGB, glow: RGB) -> dict:
    return {"fill": fill, "edge": edge, "text": text, "glow": glow}


TACTICAL = Theme(
    id="tactical",
    name="深色战术终端",
    bg=(7, 10, 15),
    bg_map=(8, 12, 18),
    panel=(13, 18, 26),
    panel_hi=(18, 25, 35),
    panel_sunk=(9, 13, 19),
    border=(27, 37, 50),
    border_hi=(48, 66, 88),
    grid=(20, 27, 37),
    text=(210, 224, 238),
    text_dim=(126, 145, 166),
    text_faint=(76, 92, 112),
    cyan=(94, 231, 224),
    blue=(78, 156, 255),
    red=(255, 106, 106),
    amber=(255, 196, 92),
    green=(92, 222, 150),
    purple=(180, 142, 255),
    white=(244, 250, 255),
    map_label=(235, 228, 200),
    map_symbol=(191, 196, 203),
    detect_classified=(150, 200, 240),
    side_blue=_side((26, 58, 96), (110, 178, 255), (210, 234, 255), (78, 156, 255)),
    side_red=_side((96, 34, 42), (248, 124, 124), (255, 216, 216), (255, 106, 106)),
    side_neutral=_side((48, 54, 64), (140, 152, 168), (208, 216, 228), (150, 160, 175)),
    terrain={
        Terrain.WATER: (17, 35, 56),
        Terrain.COAST: (76, 79, 64),
        Terrain.PLAIN: (43, 62, 49),
        Terrain.FOREST: (27, 45, 37),
        Terrain.HILLS: (70, 67, 50),
        Terrain.MOUNTAIN: (93, 90, 85),
        Terrain.URBAN: (51, 55, 65),
        Terrain.DESERT: (93, 85, 64),
        Terrain.MARSH: (37, 57, 53),
    },
)

LIGHT = Theme(
    id="light",
    name="亮色简报风",
    bg=(238, 240, 244),
    bg_map=(226, 230, 236),
    panel=(250, 251, 253),
    panel_hi=(255, 255, 255),
    panel_sunk=(232, 235, 240),
    border=(198, 205, 214),
    border_hi=(140, 150, 164),
    grid=(214, 220, 228),
    text=(26, 32, 42),
    text_dim=(88, 98, 112),
    text_faint=(134, 144, 158),
    cyan=(10, 112, 118),
    blue=(38, 96, 196),
    red=(184, 44, 44),
    amber=(150, 92, 8),
    green=(22, 120, 66),
    purple=(124, 82, 196),
    white=(255, 255, 255),
    map_label=(26, 32, 42),
    map_symbol=(74, 82, 94),
    detect_classified=(30, 80, 160),
    side_blue=_side((196, 218, 248), (46, 104, 200), (18, 44, 96), (58, 116, 214)),
    side_red=_side((250, 208, 206), (206, 62, 58), (112, 20, 20), (216, 72, 68)),
    side_neutral=_side((224, 227, 232), (140, 148, 160), (72, 78, 88), (150, 158, 170)),
    terrain={
        Terrain.WATER: (176, 204, 232),
        Terrain.COAST: (222, 216, 186),
        Terrain.PLAIN: (176, 202, 158),
        Terrain.FOREST: (140, 180, 138),
        Terrain.HILLS: (206, 196, 150),
        Terrain.MOUNTAIN: (198, 192, 184),
        Terrain.URBAN: (196, 196, 202),
        Terrain.DESERT: (224, 210, 166),
        Terrain.MARSH: (162, 194, 184),
    },
    night_tint=(52, 62, 96),
    day_tint=(255, 252, 240),
)

# 色盲友好：敌方改为琥珀色系，友方保持蓝色系，彻底避免红绿混淆。
DEUTERAN = Theme(
    id="deuteranopia",
    name="色盲友好（蓝黄）",
    bg=(9, 12, 16),
    bg_map=(10, 14, 20),
    panel=(14, 20, 28),
    panel_hi=(20, 28, 38),
    panel_sunk=(10, 15, 21),
    border=(30, 40, 54),
    border_hi=(56, 74, 96),
    grid=(22, 30, 40),
    text=(216, 226, 238),
    text_dim=(132, 148, 168),
    text_faint=(84, 98, 118),
    cyan=(120, 214, 232),
    blue=(94, 168, 255),
    red=(255, 186, 92),        # 敌方：琥珀
    amber=(255, 214, 120),
    green=(150, 224, 200),
    purple=(196, 176, 255),
    white=(246, 250, 255),
    map_label=(235, 228, 200),
    map_symbol=(191, 196, 203),
    detect_classified=(150, 200, 240),
    side_blue=_side((26, 58, 96), (120, 186, 255), (214, 236, 255), (94, 168, 255)),
    side_red=_side((94, 66, 24), (255, 198, 120), (255, 236, 202), (255, 186, 92)),
    side_neutral=_side((48, 54, 64), (146, 156, 172), (210, 216, 228), (152, 162, 176)),
    terrain={
        Terrain.WATER: (18, 34, 54),
        Terrain.COAST: (74, 78, 64),
        Terrain.PLAIN: (42, 60, 50),
        Terrain.FOREST: (26, 44, 38),
        Terrain.HILLS: (68, 66, 52),
        Terrain.MOUNTAIN: (92, 90, 86),
        Terrain.URBAN: (50, 54, 64),
        Terrain.DESERT: (92, 84, 66),
        Terrain.MARSH: (36, 56, 54),
    },
)

PROTAN = Theme(
    id="protanopia",
    name="色盲友好（蓝青）",
    bg=(8, 12, 16),
    bg_map=(9, 14, 19),
    panel=(13, 19, 26),
    panel_hi=(19, 26, 35),
    panel_sunk=(9, 14, 20),
    border=(28, 38, 50),
    border_hi=(52, 70, 92),
    grid=(21, 29, 39),
    text=(212, 224, 236),
    text_dim=(128, 146, 166),
    text_faint=(80, 96, 116),
    cyan=(110, 220, 226),
    blue=(96, 172, 255),
    red=(255, 214, 96),        # 敌方：金黄
    amber=(255, 200, 110),
    green=(146, 226, 214),
    purple=(190, 180, 255),
    white=(244, 250, 255),
    map_label=(235, 228, 200),
    map_symbol=(191, 196, 203),
    detect_classified=(150, 200, 240),
    side_blue=_side((24, 56, 94), (116, 184, 255), (210, 234, 255), (96, 172, 255)),
    side_red=_side((88, 68, 20), (255, 206, 120), (255, 240, 206), (255, 214, 96)),
    side_neutral=_side((46, 52, 62), (142, 154, 170), (208, 216, 228), (150, 160, 175)),
    terrain={
        Terrain.WATER: (17, 34, 54),
        Terrain.COAST: (72, 78, 64),
        Terrain.PLAIN: (42, 60, 50),
        Terrain.FOREST: (26, 44, 38),
        Terrain.HILLS: (66, 66, 52),
        Terrain.MOUNTAIN: (90, 90, 86),
        Terrain.URBAN: (49, 54, 64),
        Terrain.DESERT: (90, 84, 66),
        Terrain.MARSH: (35, 56, 54),
    },
)

THEMES: dict[str, Theme] = {
    t.id: t for t in (TACTICAL, LIGHT, DEUTERAN, PROTAN)
}
DEFAULT_THEME = "tactical"

_current: Theme = TACTICAL


def set_theme(theme_id: str) -> bool:
    """切换主题。返回 True 表示切换成功。"""
    global _current
    t = THEMES.get(theme_id)
    if t is None:
        return False
    if t.id == _current.id:
        return True
    _current = t
    _text_cache.clear()
    return True


def current_theme() -> Theme:
    """当前主题对象。"""
    return _current


def theme_ids() -> list[str]:
    """全部主题 ID（按显示顺序）。"""
    return [t.id for t in (TACTICAL, LIGHT, DEUTERAN, PROTAN)]


def theme_name(theme_id: str) -> str:
    t = THEMES.get(theme_id)
    return t.name if t else theme_id


# --------------------------------------------------------------------------- #
# 模块级动态属性：T.TEXT / T.PANEL / ...
# --------------------------------------------------------------------------- #
_MISSING = object()


def __getattr__(name: str):
    """把模块级颜色常量解析到当前主题（PEP 562）。

    这样既保持了 `T.TEXT` 的既有调用方式，又能在运行时切换主题。
    """
    if name.startswith("_"):
        raise AttributeError(name)
    t = _current
    simple = {
        "BG": t.bg, "BG_MAP": t.bg_map,
        "PANEL": t.panel, "PANEL_HI": t.panel_hi, "PANEL_SUNK": t.panel_sunk,
        "BORDER": t.border, "BORDER_HI": t.border_hi, "GRID": t.grid,
        "TEXT": t.text, "TEXT_DIM": t.text_dim, "TEXT_FAINT": t.text_faint,
        "CYAN": t.cyan, "BLUE": t.blue, "RED": t.red, "AMBER": t.amber,
        "GREEN": t.green, "PURPLE": t.purple, "WHITE": t.white,
        "MAP_LABEL": t.map_label, "MAP_SYMBOL": t.map_symbol,
        "DETECT_CLASSIFIED": t.detect_classified,
        "NIGHT_TINT": t.night_tint, "DAY_TINT": t.day_tint,
    }
    if name in simple:
        return simple[name]
    if name == "SIDE_COLORS":
        return {
            Side.BLUE: t.side_blue,
            Side.RED: t.side_red,
            Side.NEUTRAL: t.side_neutral,
        }
    if name == "TERRAIN_COLORS":
        return t.terrain
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + [
        "BG", "BG_MAP", "PANEL", "PANEL_HI", "PANEL_SUNK", "BORDER", "BORDER_HI",
        "GRID", "TEXT", "TEXT_DIM", "TEXT_FAINT", "CYAN", "BLUE", "RED", "AMBER",
        "GREEN", "PURPLE", "WHITE", "SIDE_COLORS", "TERRAIN_COLORS",
        "MAP_LABEL", "MAP_SYMBOL", "DETECT_CLASSIFIED",
        "NIGHT_TINT", "DAY_TINT",
    ])


# --------------------------------------------------------------------------- #
# 字号规范
# --------------------------------------------------------------------------- #
# 小字号在 1500×920 的窗口上确实难读，投影与高分屏上更甚。
# 这里统一定义字号档位，调用方用常量而不是魔法数字——
# 想整体调大只需改这一处。
#
# 下面这些是**基准字号**（标准档），运行时由 `px()` 换算到当前档位。
# 界面代码仍写基准值，这样「调大字号」不需要改几百个调用点。
FS_TINY = 10        # 角标、标签、单位缩写
FS_SMALL = 11       # 次要说明、状态行
FS_BODY = 12        # 正文
FS_LABEL = 13       # 字段名
FS_SUBTITLE = 14    # 小标题
FS_TITLE = 16       # 区块标题
FS_HEAD = 18        # 大标题
FS_HERO = 24        # 结算大标题


# --------------------------------------------------------------------------- #
# 字号缩放（v3.1.1 无障碍）
# --------------------------------------------------------------------------- #
# 只放大文字是没用的：字号一变，行高、卡片高度、面板宽度全都要跟着变，
# 否则文字会顶出容器、行与行互相压住。所以这里一并提供 `m()`（基准度量 →
# 当前档位），界面里凡是「按字号调过的像素值」都走它。
#
# 三档的倍率不是随手取的：1.15 刚好让 11 号字落到 13 号（常见的舒适正文），
# 1.30 让 10 号角标过 13 号——再大就会在 1500×920 的窗口里挤不下侧栏。
@dataclass(frozen=True)
class FontScale:
    """一档字号缩放。"""

    id: str
    name: str
    ratio: float


FONT_SCALES: tuple[FontScale, ...] = (
    FontScale("standard", "标准", 1.00),
    FontScale("large", "大", 1.15),
    FontScale("xlarge", "特大", 1.30),
)
DEFAULT_FONT_SCALE = "standard"

_current_scale: FontScale = FONT_SCALES[0]


def set_font_scale(scale_id: str) -> bool:
    """切换字号档位。返回 True 表示档位有效。

    只改一个倍率，不动任何调用点——`draw_text` / `text_width` /
    `wrap_text` 全都在内部过 `px()`，所以测量与绘制永远一致。
    """
    global _current_scale
    for fs in FONT_SCALES:
        if fs.id != scale_id:
            continue
        if fs.id != _current_scale.id:
            _current_scale = fs
            _text_cache.clear()      # 缓存键含像素字号，但清掉更省内存
        return True
    return False


def font_scale_id() -> str:
    return _current_scale.id


def font_scale_ids() -> list[str]:
    return [f.id for f in FONT_SCALES]


def font_scale_name(scale_id: str) -> str:
    for f in FONT_SCALES:
        if f.id == scale_id:
            return f.name
    return scale_id


def font_ratio() -> float:
    return _current_scale.ratio


@contextlib.contextmanager
def at_font_scale(scale_id: str):
    """临时按指定档位排版，退出时还原（画完即恢复）。

    地图 / 战役编辑器是**工具界面**：三百多个控件的位置按像素写死，
    不跟字号档位走。与其把它们全部改成 `T.m()`（收益低、回归面大），
    不如在绘制编辑器期间强制回到标准档——放大字号后编辑器仍然工整，
    而游戏主界面照常放大。
    """
    global _current_scale
    prev = _current_scale
    try:
        set_font_scale(scale_id)
        yield
    finally:
        _current_scale = prev


def px(size: int) -> int:
    """基准字号 → 当前档位的像素字号。"""
    if _current_scale.ratio == 1.0:
        return int(size)
    # 下限 9：再小连角标都糊成一团，放大档位不该产生更小的字
    return max(9, int(round(size * _current_scale.ratio)))


def m(base: int) -> int:
    """基准度量（行高 / 控件高度 / 内边距）→ 当前档位。

    标准档原样返回，避免给默认路径引入取整误差。
    """
    if _current_scale.ratio == 1.0:
        return int(base)
    return int(round(base * _current_scale.ratio))


def lh(size: int, factor: float = 1.0) -> int:
    """一行文字占的高度（含行距）——排版累加 y 时用它，别写死 +16。"""
    p = px(size)
    return max(p + 2, int(round(p * 1.42 * factor)))


def ellipsize(s: str, size: int, max_width: int, bold: bool = False) -> str:
    """把文字截到 max_width 以内，超出的用省略号收尾。

    字号放大后，原本刚好的标题会顶出卡片。硬切片（``name[:14]``）
    对不同语言的字宽毫无概念，所以这里按**像素**收，而不是按字收。
    """
    s = str(s)
    if max_width <= 0 or text_width(s, size, bold) <= max_width:
        return s
    ell = "…"
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if text_width(s[:mid] + ell, size, bold) <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return (s[:lo] + ell) if lo > 0 else ell


# --------------------------------------------------------------------------- #
# 形状标识（v3.1.1 无障碍）
# --------------------------------------------------------------------------- #
# 红绿是最容易混淆的一对（红绿色盲约占男性 8%）。色盲主题已经把敌方改成
# 琥珀 / 金黄，但那仍是「靠颜色」；本项让敌我与兵种类别**另有形状**，
# 灰度截图里也能读出来。
#
# 默认 `auto`：只在色盲主题下启用。理由是形状会占掉棋子上本就紧张的几个
# 像素——普通主题下多数玩家不需要，不如留白；需要的人可以在设置里强制打开。
SHAPE_MODES: tuple[tuple[str, str], ...] = (
    ("auto", "自动"),
    ("on", "开"),
    ("off", "关"),
)
DEFAULT_SHAPE_MODE = "auto"
_COLORBLIND_THEMES = frozenset({"deuteranopia", "protanopia"})

_shape_mode: str = DEFAULT_SHAPE_MODE


def set_shape_mode(mode: str) -> bool:
    global _shape_mode
    if mode not in {k for k, _ in SHAPE_MODES}:
        return False
    _shape_mode = mode
    return True


def shape_mode() -> str:
    return _shape_mode


def shape_mode_name(mode: str) -> str:
    for k, v in SHAPE_MODES:
        if k == mode:
            return v
    return mode


def shape_cues() -> bool:
    """当前是否绘制形状标识。"""
    if _shape_mode == "on":
        return True
    if _shape_mode == "off":
        return False
    return _current.id in _COLORBLIND_THEMES


# --------------------------------------------------------------------------- #
# 字体
# --------------------------------------------------------------------------- #
_FONT_CANDIDATES_CN = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhl.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\Deng.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]
_FONT_CANDIDATES_BOLD = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\Dengb.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
]
_FONT_MONO = [
    r"C:\Windows\Fonts\consola.ttf",
    r"C:\Windows\Fonts\cour.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]


@lru_cache(maxsize=8)
def _first_existing(paths: tuple[str, ...]) -> str | None:
    for p in paths:
        if os.path.isfile(p):
            return p
    return None


_SYSFONT_OK: bool | None = None


def _sysfont_available() -> bool:
    """系统字体表是否可用（只探测一次）。

    `pygame.font.SysFont` 在 Windows 上要枚举字体注册表，遇到以 DWORD
    登记的字体项会抛 `TypeError: expected str, bytes or os.PathLike
    object, not int`（pygame-ce 的已知问题）。这个探测把异常挡在门外，
    之后一律走字体文件或 pygame 默认字体，绝不让字体初始化崩掉整个游戏。
    """
    global _SYSFONT_OK
    if _SYSFONT_OK is None:
        try:
            pygame.font.SysFont("arial", 12)
            _SYSFONT_OK = True
        except Exception:
            _SYSFONT_OK = False
    return _SYSFONT_OK


class Fonts:
    """按字号缓存字体对象；找不到中文字体时回退到系统默认字体。"""

    def __init__(self) -> None:
        if not pygame.font.get_init():
            pygame.font.init()
        self._cache: dict[tuple[int, bool, bool], pygame.font.Font] = {}

    def get(self, size: int, bold: bool = False, mono: bool = False) -> pygame.font.Font:
        key = (size, bold, mono)
        if key in self._cache:
            return self._cache[key]
        font: pygame.font.Font | None = None
        if mono:
            # 等宽字体（Consolas 等）**没有中文字形**——直接用会让中文
            # 渲染成方框或字距错乱。优先按**文件路径**加载：SysFont 要
            # 枚举系统注册表，既慢又可能抛异常，而且它的 fallback 链
            # 并不按字符回退（选中的仍是同一个字体）。
            path = _first_existing(tuple(_FONT_MONO))
            if path:
                try:
                    font = pygame.font.Font(path, size)
                except Exception:
                    font = None
            if font is None and _sysfont_available():
                # 文件不在（Linux 上字体包可能没装）→ 退到系统等宽字体
                try:
                    font = pygame.font.SysFont(
                        "consolas,couriernew,dejavusansmono,monospace",
                        size, bold=bold)
                except Exception:
                    font = None
        if font is None:
            path = _first_existing(tuple(_FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_CN))
            if path:
                try:
                    font = pygame.font.Font(path, size)
                except Exception:
                    font = None
        if font is None and _sysfont_available():
            try:
                font = pygame.font.SysFont("microsoftyahei,simhei,arial", size, bold=bold)
            except Exception:
                font = None
        if font is None:
            # 最后的兜底：pygame 内置默认字体，永远可用（但没有中文字形）
            font = pygame.font.Font(None, size)
        self._cache[key] = font
        return font


_FONTS: Fonts | None = None


def fonts() -> Fonts:
    global _FONTS
    if _FONTS is None:
        _FONTS = Fonts()
    return _FONTS


# --------------------------------------------------------------------------- #
# 文本缓存
# --------------------------------------------------------------------------- #
_text_cache: dict[tuple, pygame.Surface] = {}
_CACHE_LIMIT = 3000


_CN_CHARS = re.compile(r"[　-〿一-鿿＀-￯]")


def render_text(s: str, size: int, color: tuple[int, int, int],
                bold: bool = False, mono: bool = False) -> pygame.Surface:
    """渲染一行文字。`size` 传**基准字号**，这里统一换算到当前档位。"""
    # 等宽字体（Consolas 等）**没有中文字形**——用它渲染中文会出现
    # 方框或字距错乱。含中文的文本一律改用中文字体；
    # 纯数字 / 纯拉丁文本仍走等宽，保证表格对齐。
    if mono and _CN_CHARS.search(s):
        mono = False
    size = px(size)
    key = (s, size, color, bold, mono)
    surf = _text_cache.get(key)
    if surf is None:
        if len(_text_cache) > _CACHE_LIMIT:
            _text_cache.clear()
        surf = fonts().get(size, bold, mono).render(s, True, color)
        _text_cache[key] = surf
    return surf


# pygame 的 Rect.get_rect 只认这些锚点名。
#
# 两条容易踩的规则：
# 1. **没有** "mid" / "midtop" / "midbottom"——只有 midleft 与 midright；
# 2. **单轴锚点不可用**——"top" / "bottom" / "centerx" / "centery" 单独传给
#    get_rect 会抛 TypeError（它们是坐标轴名，不是锚点名）。
#
# 因此本模块只暴露这 9 个合法值，并在 draw_text 里显式校验，
# 把「写错锚点」从运行时的 AttributeError/TypeError 变成开发期就能看见的 ValueError。
_VALID_ANCHORS = frozenset({
    "topleft", "topright", "bottomleft", "bottomright",
    "midleft", "midright", "center",
})


def draw_text(surface: pygame.Surface, s: str, pos: tuple[int, int], size: int = 13,
              color: tuple[int, int, int] | None = None, bold: bool = False,
              mono: bool = False, anchor: str = "topleft", alpha: int = 255) -> pygame.Rect:
    if color is None:
        color = _current.text
    if anchor not in _VALID_ANCHORS:
        # 锚点写错是低级错误，但崩溃会掩盖真正的问题——
        # 明确报错并指出去处，比让 AttributeError 冒上来好排查。
        raise ValueError(
            f"未知的文字锚点 {anchor!r}；可用值：{sorted(_VALID_ANCHORS)}")
    surf = render_text(str(s), size, color, bold, mono)
    rect = surf.get_rect(**{anchor: pos})
    if alpha < 255:
        surf = surf.copy()
        surf.set_alpha(alpha)
    surface.blit(surf, rect)
    return rect


def text_width(s: str, size: int = 13, bold: bool = False, mono: bool = False) -> int:
    """文字宽度。`size` 同样是基准字号——测量与绘制必须走同一换算，
    否则放大档位下「算出来放得下」会在画的时候顶出去。"""
    return render_text(str(s), size, (255, 255, 255), bold, mono).get_width()


def draw_text_center_x(surface: pygame.Surface, s: str, cx: int, y: int,
                        size: int = 13, color: tuple[int, int, int] | None = None,
                        bold: bool = False, mono: bool = False,
                        alpha: int = 255) -> pygame.Rect:
    """绘制**水平居中于 cx、顶端对齐于 y** 的文字。

    pygame 的锚点里没有 "midtop"（只有 midleft / midright），
    而单轴锚点 "centerx" 也不能单独传给 get_rect——所以这里自己算偏移。
    """
    text = str(s)
    w = text_width(text, size, bold, mono)
    return draw_text(surface, text, (cx - w // 2, y), size, color, bold, mono,
                     anchor="topleft", alpha=alpha)


def wrap_text(s: str, size: int, max_width: int, bold: bool = False) -> list[str]:
    """按像素宽度换行。

    **拉丁文按词断行、中日文按字断行**：一直按字符切会把德语、俄语
    的长单词劈成两半（``Muniti`` / ``on``），读起来像排版事故；而中文
    没有空格，按词切等于整段不换行。所以含空格的段先试按词排，
    某个词本身超宽时才退回按字符硬拆。
    """
    font = fonts().get(px(size), bold, False)
    lines: list[str] = []
    for para in s.split("\n"):
        if not para:
            lines.append("")
            continue

        def _split_by_char(text: str) -> list[str]:
            out: list[str] = []
            cur = ""
            for ch in text:
                test = cur + ch
                if font.size(test)[0] > max_width and cur:
                    out.append(cur)
                    cur = ch
                else:
                    cur = test
            if cur:
                out.append(cur)
            return out

        if " " not in para:
            lines.extend(_split_by_char(para))
            continue

        cur = ""
        for word in para.split(" "):
            candidate = word if not cur else cur + " " + word
            if font.size(candidate)[0] <= max_width:
                cur = candidate
                continue
            if cur:
                lines.append(cur)
                cur = ""
            if font.size(word)[0] <= max_width:
                cur = word
                continue
            pieces = _split_by_char(word)
            lines.extend(pieces[:-1])
            cur = pieces[-1] if pieces else ""
        if cur:
            lines.append(cur)
    return lines


# --------------------------------------------------------------------------- #
# 基础绘制
# --------------------------------------------------------------------------- #
def elevation_shade(base: tuple[int, int, int], elev: int) -> tuple[int, int, int]:
    """按海拔调整明度，形成地形起伏感。"""
    f = 0.74 + min(1.0, elev / 2800.0) * 0.54
    return (
        max(0, min(255, int(base[0] * f))),
        max(0, min(255, int(base[1] * f))),
        max(0, min(255, int(base[2] * f))),
    )


def mix(c1: tuple[int, int, int], c2: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return (int(c1[0] + (c2[0] - c1[0]) * t),
            int(c1[1] + (c2[1] - c1[1]) * t),
            int(c1[2] + (c2[2] - c1[2]) * t))


def button_fill(accent: RGB, hover: bool = False) -> RGB:
    """按钮底色：由主题面板色与强调色派生。

    **不要在界面代码里硬编码按钮底色。** 早期版本把按钮写成
    ``(18, 26, 36)`` 这样的深色常量，切到亮色主题后这些按钮
    在浅色背景上变成突兀的黑块（实测主菜单的「编辑想定」「快速上手」
    「战役模式」「设置」四个按钮全是深色）。

    派生规则：底色始终是「面板色向强调色靠一点」，
    因此深色主题下按钮略亮于面板，浅色主题下按钮略深于面板——
    两套主题下都能看出边界，且不会跑到对面去。

    比例随主题明暗翻转：深色主题多混一点（0.18 / 0.34），
    浅色主题少混一点（0.10 / 0.20），免得浅底被染得太艳。
    """
    t = _current
    light = t.id == "light"
    if light:
        ratio = 0.20 if hover else 0.10
        base = t.panel_hi
    else:
        ratio = 0.34 if hover else 0.18
        base = t.panel
    return mix(base, accent, ratio)


def button_text(accent: RGB, hover: bool = False) -> RGB:
    """按钮上的文字色。

    浅色主题下按钮底色是浅的，文字必须用深色才看得清；
    深色主题反之。用 ``accent`` 微微染色，让按钮文字与边框呼应。
    """
    t = _current
    if t.id == "light":
        return mix(t.text, accent, 0.25)
    return t.white if hover else mix(t.white, accent, 0.15)



def panel(surface: pygame.Surface, rect: pygame.Rect, fill=None, border=None,
          radius: int = 4, width: int = 1) -> pygame.Rect:
    if fill is None:
        fill = _current.panel
    if border is None:
        border = _current.border
    pygame.draw.rect(surface, fill, rect, border_radius=radius)
    if border:
        pygame.draw.rect(surface, border, rect, width=width, border_radius=radius)
    return rect


def hline(surface: pygame.Surface, x1: int, x2: int, y: int, color=None) -> None:
    pygame.draw.line(surface, color if color is not None else _current.border,
                     (x1, y), (x2, y), 1)


def vline(surface: pygame.Surface, x: int, y1: int, y2: int, color=None) -> None:
    pygame.draw.line(surface, color if color is not None else _current.border,
                     (x, y1), (x, y2), 1)


def bar(surface: pygame.Surface, rect: pygame.Rect, ratio: float,
        color: tuple[int, int, int], bg=None, radius: int = 2) -> None:
    ratio = max(0.0, min(1.0, ratio))
    if bg is None:
        bg = mix(_current.panel_sunk, (255, 255, 255), 0.08)
    pygame.draw.rect(surface, bg, rect, border_radius=radius)
    if ratio > 0:
        w = max(1, int(rect.width * ratio))
        pygame.draw.rect(surface, color, pygame.Rect(rect.x, rect.y, w, rect.height),
                         border_radius=radius)


def draw_wind_icon(surface: pygame.Surface, center: tuple[int, int],
                   vec: tuple[int, int], speed: int, color=None,
                   length: int = 7) -> None:
    """画风向箭头：箭头指向烟幕**漂移**的方向。

    顶栏环境块只有 128 px 宽、挤着三行字，「西北风 2 级」这种句子
    在任何语言里都塞不进去（德语光是天候名就要 40 px）。
    方向这件事看图比读字快，所以只画箭头 + 风力数字；
    文字版仍留在战报里，需要准确描述的地方不缺。
    """
    t = _current
    col = color if color is not None else (t.text_dim if speed <= 1 else t.cyan)
    cx, cy = int(center[0]), int(center[1])
    vx, vy = vec
    if speed <= 0 or (vx == 0 and vy == 0):
        # 静风：空心小圈，表示「烟糊在原地不挪窝」
        pygame.draw.circle(surface, col, (cx, cy), 3, 1)
        return
    # 8 方位归一化，否则斜向箭头会比正交箭头长出 40%
    inv = 0.7071 if vx and vy else 1.0
    ux, uy = vx * inv, vy * inv
    tip = (cx + ux * length, cy + uy * length)
    pygame.draw.line(surface, col, (cx - ux * length, cy - uy * length), tip, 1)
    px, py = -uy, ux                      # 垂直于箭头方向
    bx, by = tip[0] - ux * 4, tip[1] - uy * 4
    pygame.draw.line(surface, col, tip, (bx + px * 2.5, by + py * 2.5), 1)
    pygame.draw.line(surface, col, tip, (bx - px * 2.5, by - py * 2.5), 1)


def hp_color(ratio: float) -> tuple[int, int, int]:
    t = _current
    if ratio > 0.65:
        return t.green
    if ratio > 0.32:
        return t.amber
    return t.red


def side_color(side: Side, key: str = "edge"):
    cols = {
        Side.BLUE: _current.side_blue,
        Side.RED: _current.side_red,
        Side.NEUTRAL: _current.side_neutral,
    }
    return cols.get(side, _current.side_neutral)[key]
