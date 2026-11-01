"""视觉主题：配色、字体缓存、文本渲染。

整体采用深色战术终端风格：低饱和地形色 + 高对比阵营色 + 荧光数据字。
"""
from __future__ import annotations

import os
from functools import lru_cache

import pygame

from core.enums import Side, Terrain

# --------------------------------------------------------------------------- #
# 调色板
# --------------------------------------------------------------------------- #
BG = (7, 10, 15)
BG_MAP = (8, 12, 18)
PANEL = (13, 18, 26)
PANEL_HI = (18, 25, 35)
PANEL_SUNK = (9, 13, 19)
BORDER = (27, 37, 50)
BORDER_HI = (48, 66, 88)
GRID = (20, 27, 37)

TEXT = (210, 224, 238)
TEXT_DIM = (126, 145, 166)
TEXT_FAINT = (76, 92, 112)

CYAN = (94, 231, 224)
BLUE = (78, 156, 255)
RED = (255, 106, 106)
AMBER = (255, 196, 92)
GREEN = (92, 222, 150)
PURPLE = (180, 142, 255)
WHITE = (244, 250, 255)

SIDE_COLORS = {
    Side.BLUE: {"fill": (26, 58, 96), "edge": (110, 178, 255), "text": (210, 234, 255),
                "glow": (78, 156, 255)},
    Side.RED: {"fill": (96, 34, 42), "edge": (248, 124, 124), "text": (255, 216, 216),
               "glow": (255, 106, 106)},
    Side.NEUTRAL: {"fill": (48, 54, 64), "edge": (140, 152, 168), "text": (208, 216, 228),
                   "glow": (150, 160, 175)},
}

TERRAIN_COLORS = {
    Terrain.WATER: (17, 35, 56),
    Terrain.COAST: (76, 79, 64),
    Terrain.PLAIN: (43, 62, 49),
    Terrain.FOREST: (27, 45, 37),
    Terrain.HILLS: (70, 67, 50),
    Terrain.MOUNTAIN: (93, 90, 85),
    Terrain.URBAN: (51, 55, 65),
    Terrain.DESERT: (93, 85, 64),
    Terrain.MARSH: (37, 57, 53),
}

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
              color: tuple[int, int, int] = TEXT, bold: bool = False, mono: bool = False,
              anchor: str = "topleft", alpha: int = 255) -> pygame.Rect:
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


def panel(surface: pygame.Surface, rect: pygame.Rect, fill=PANEL, border=BORDER,
          radius: int = 4, width: int = 1) -> pygame.Rect:
    pygame.draw.rect(surface, fill, rect, border_radius=radius)
    if border:
        pygame.draw.rect(surface, border, rect, width=width, border_radius=radius)
    return rect


def hline(surface: pygame.Surface, x1: int, x2: int, y: int, color=BORDER) -> None:
    pygame.draw.line(surface, color, (x1, y), (x2, y), 1)


def vline(surface: pygame.Surface, x: int, y1: int, y2: int, color=BORDER) -> None:
    pygame.draw.line(surface, color, (x, y1), (x, y2), 1)


def bar(surface: pygame.Surface, rect: pygame.Rect, ratio: float,
        color: tuple[int, int, int], bg=(30, 38, 50), radius: int = 2) -> None:
    ratio = max(0.0, min(1.0, ratio))
    pygame.draw.rect(surface, bg, rect, border_radius=radius)
    if ratio > 0:
        w = max(1, int(rect.width * ratio))
        pygame.draw.rect(surface, color, pygame.Rect(rect.x, rect.y, w, rect.height),
                         border_radius=radius)


def hp_color(ratio: float) -> tuple[int, int, int]:
    if ratio > 0.65:
        return GREEN
    if ratio > 0.32:
        return AMBER
    return RED


def side_color(side: Side, key: str = "edge"):
    return SIDE_COLORS.get(side, SIDE_COLORS[Side.NEUTRAL])[key]
