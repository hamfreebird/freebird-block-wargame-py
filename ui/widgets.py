"""通用控件：按钮、开关、进度条、日志视图、提示框。"""
from __future__ import annotations

from typing import Callable

import pygame

from . import theme as T


# --------------------------------------------------------------------------- #
class Button:
    """轻量按钮。由调用方在每帧绘制并在事件中查询 hit()。"""

    def __init__(self, rect: pygame.Rect, label: str, on_click: Callable[[], None] | None = None,
                 kind: str = "ghost", size: int = 13, hotkey: str = "",
                 tooltip: str = "", enabled: bool = True):
        self.rect = pygame.Rect(rect)
        self.label = label
        self.on_click = on_click
        self.kind = kind          # primary / ghost / danger / toggle
        self.size = size
        self.hotkey = hotkey
        self.tooltip = tooltip
        self.enabled = enabled
        self.active = False       # toggle 状态
        self.hover = False
        self._pressed = False

    # -- 状态 -------------------------------------------------------------
    def update(self, mouse_pos: tuple[int, int]) -> None:
        self.hover = self.rect.collidepoint(mouse_pos)

    def hit(self, pos: tuple[int, int]) -> bool:
        return self.enabled and self.rect.collidepoint(pos)

    def handle_click(self, pos: tuple[int, int]) -> bool:
        if self.hit(pos):
            if self.on_click:
                self.on_click()
            return True
        return False

    # -- 绘制 -------------------------------------------------------------
    def draw(self, surf: pygame.Surface) -> None:
        r = self.rect
        if not self.enabled:
            fill, border, fg = T.PANEL_SUNK, (30, 38, 50), T.TEXT_FAINT
        elif self.kind == "primary":
            fill = (28, 92, 120) if self.hover else (22, 74, 98)
            border, fg = (T.CYAN if self.hover else (48, 130, 158)), T.WHITE
        elif self.kind == "danger":
            fill = (110, 36, 40) if self.hover else (78, 28, 32)
            border, fg = (T.RED if self.hover else (120, 60, 66)), T.WHITE
        elif self.active:
            fill = (30, 82, 70) if self.hover else (24, 66, 58)
            border, fg = T.GREEN, (200, 255, 220)
        else:
            fill = T.PANEL_HI if self.hover else T.PANEL
            border = T.BORDER_HI if self.hover else T.BORDER
            fg = T.TEXT if self.hover else T.TEXT_DIM

        pygame.draw.rect(surf, fill, r, border_radius=3)
        pygame.draw.rect(surf, border, r, width=1, border_radius=3)

        label = self.label
        if self.hotkey:
            label = f"{self.label}"
        T.draw_text(surf, label, r.center, self.size, fg, bold=self.kind == "primary",
                    anchor="center")
        if self.hotkey and r.width > 54:
            T.draw_text(surf, self.hotkey, (r.right - 6, r.centery + 1), self.size - 3,
                        T.TEXT_FAINT, anchor="midright")


# --------------------------------------------------------------------------- #
class LogView:
    """可滚动的作战日志。"""

    KIND_COLOR = {
        "info": T.TEXT_DIM,
        "combat": (255, 176, 120),
        "detect": (140, 200, 255),
        "ew": (186, 150, 255),
        "move": (150, 190, 170),
        "system": T.CYAN,
        "victory": (255, 226, 130),
    }

    def __init__(self, rect: pygame.Rect, size: int = 12):
        self.rect = pygame.Rect(rect)
        self.size = size
        self.scroll = 0
        self._wrapped_cache: tuple[int, list[tuple[str, tuple[int, int, int]]]] | None = None

    def scroll_by(self, dy: int) -> None:
        self.scroll = max(0, self.scroll + dy)

    def _build(self, entries, width: int) -> list[tuple[str, tuple[int, int, int]]]:
        lines: list[tuple[str, tuple[int, int, int]]] = []
        for e in entries:
            color = self.KIND_COLOR.get(e.kind, T.TEXT_DIM)
            prefix = f"T{e.turn} "
            for i, seg in enumerate(theme_wrap(prefix + e.text, self.size, width - 10)):
                lines.append((seg, color if i == 0 else T.TEXT_FAINT))
        return lines

    def draw(self, surf: pygame.Surface, entries) -> None:
        pygame.draw.rect(surf, T.PANEL_SUNK, self.rect, border_radius=3)
        pygame.draw.rect(surf, T.BORDER, self.rect, width=1, border_radius=3)
        lines = self._build(entries, self.rect.width)
        line_h = self.size + 5
        visible = (self.rect.height - 8) // line_h
        max_scroll = max(0, len(lines) - visible)
        self.scroll = min(self.scroll, max_scroll)
        end = len(lines) - self.scroll
        start = max(0, end - visible)
        y = self.rect.y + 5
        for text, color in lines[start:end]:
            T.draw_text(surf, text, (self.rect.x + 6, y), self.size, color)
            y += line_h


def theme_wrap(s: str, size: int, width: int) -> list[str]:
    return T.wrap_text(s, size, max(40, width))


# --------------------------------------------------------------------------- #
def draw_tooltip(surf: pygame.Surface, text: str, pos: tuple[int, int],
                 max_w: int = 300, size: int = 12) -> None:
    lines = T.wrap_text(text, size, max_w)
    if not lines:
        return
    w = min(max_w, max(T.text_width(l, size) for l in lines)) + 16
    h = len(lines) * (size + 4) + 10
    x, y = pos
    sw, sh = surf.get_size()
    if x + w > sw - 8:
        x = sw - w - 8
    if y + h > sh - 8:
        y = y - h - 10
    rect = pygame.Rect(x, y, w, h)
    pygame.draw.rect(surf, (14, 19, 27), rect, border_radius=4)
    pygame.draw.rect(surf, T.BORDER_HI, rect, width=1, border_radius=4)
    yy = y + 5
    for l in lines:
        T.draw_text(surf, l, (x + 8, yy), size, T.TEXT)
        yy += size + 4


# --------------------------------------------------------------------------- #
def section(surf: pygame.Surface, rect: pygame.Rect, title: str,
            accent: tuple[int, int, int] = T.CYAN) -> pygame.Rect:
    """标题栏 + 细分隔线，返回内容区起点 y。"""
    pygame.draw.rect(surf, accent, pygame.Rect(rect.x, rect.y + 3, 3, 12))
    T.draw_text(surf, title, (rect.x + 9, rect.y), 12, T.TEXT_DIM, bold=True)
    T.hline(surf, rect.x, rect.right, rect.y + 19, (28, 37, 49))
    return pygame.Rect(rect.x, rect.y + 24, rect.width, rect.height - 24)


def kv(surf: pygame.Surface, x: int, y: int, key: str, value: str,
       width: int, key_color=T.TEXT_FAINT, val_color=T.TEXT, size: int = 12) -> int:
    T.draw_text(surf, key, (x, y), size, key_color)
    T.draw_text(surf, value, (x + width, y), size, val_color, anchor="topright", mono=True)
    return y + size + 4
