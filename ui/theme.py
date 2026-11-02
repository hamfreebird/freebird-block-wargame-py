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

import os
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
    cyan=(14, 132, 136),
    blue=(38, 96, 196),
    red=(198, 52, 52),
    amber=(176, 112, 12),
    green=(30, 142, 82),
    purple=(124, 82, 196),
    white=(255, 255, 255),
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
        "NIGHT_TINT", "DAY_TINT",
    ])


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
            path = _first_existing(tuple(_FONT_MONO))
            if path:
                font = pygame.font.Font(path, size)
        if font is None:
            path = _first_existing(tuple(_FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_CN))
            if path:
                try:
                    font = pygame.font.Font(path, size)
                except Exception:
                    font = None
        if font is None:
            font = pygame.font.SysFont("microsoftyahei,simhei,arial", size, bold=bold)
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


def render_text(s: str, size: int, color: tuple[int, int, int],
                bold: bool = False, mono: bool = False) -> pygame.Surface:
    key = (s, size, color, bold, mono)
    surf = _text_cache.get(key)
    if surf is None:
        if len(_text_cache) > _CACHE_LIMIT:
            _text_cache.clear()
        surf = fonts().get(size, bold, mono).render(s, True, color)
        _text_cache[key] = surf
    return surf


def draw_text(surface: pygame.Surface, s: str, pos: tuple[int, int], size: int = 13,
              color: tuple[int, int, int] | None = None, bold: bool = False,
              mono: bool = False, anchor: str = "topleft", alpha: int = 255) -> pygame.Rect:
    if color is None:
        color = _current.text
    surf = render_text(str(s), size, color, bold, mono)
    rect = surf.get_rect(**{anchor: pos})
    if alpha < 255:
        surf = surf.copy()
        surf.set_alpha(alpha)
    surface.blit(surf, rect)
    return rect


def text_width(s: str, size: int, bold: bool = False, mono: bool = False) -> int:
    return render_text(str(s), size, (255, 255, 255), bold, mono).get_width()


def wrap_text(s: str, size: int, max_width: int, bold: bool = False) -> list[str]:
    """按像素宽度换行（支持中英文混排）。"""
    font = fonts().get(size, bold, False)
    lines: list[str] = []
    for para in s.split("\n"):
        if not para:
            lines.append("")
            continue
        cur = ""
        for ch in para:
            test = cur + ch
            if font.size(test)[0] > max_width and cur:
                lines.append(cur)
                cur = ch
            else:
                cur = test
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
