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
        # 填充色由主题色派生，保证亮色 / 色盲主题下按钮依然协调
        if not self.enabled:
            fill, border, fg = T.PANEL_SUNK, T.BORDER, T.TEXT_FAINT
        elif self.kind == "primary":
            fill = T.button_fill(T.CYAN, self.hover)
            border = T.CYAN if self.hover else T.mix(T.BORDER, T.CYAN, 0.55)
            fg = T.button_text(T.CYAN, self.hover)
        elif self.kind == "danger":
            fill = T.button_fill(T.RED, self.hover)
            border = T.RED if self.hover else T.mix(T.BORDER, T.RED, 0.55)
            fg = T.button_text(T.RED, self.hover)
        elif self.active:
            fill = T.button_fill(T.GREEN, self.hover)
            border = T.GREEN
            fg = T.button_text(T.GREEN, self.hover)
        else:
            fill = T.PANEL_HI if self.hover else T.PANEL
            border = T.BORDER_HI if self.hover else T.BORDER
            fg = T.TEXT if self.hover else T.TEXT_DIM


        pygame.draw.rect(surf, fill, r, border_radius=3)
        pygame.draw.rect(surf, border, r, width=1, border_radius=3)

        label = self.label
        if self.hotkey:
            label = f"{self.label}"
        # 标签过长时自动降字号——中文标签（如「筑工事 0/3」）在窄按钮里
        # 会顶到边框，字号自适应比硬编码更省事
        size = self.size
        avail = r.width - 10
        if avail > 10 and T.text_width(label, size) > avail:
            for cand in range(size - 1, 7, -1):
                if T.text_width(label, cand) <= avail:
                    size = cand
                    break
            else:
                size = 8
        T.draw_text(surf, label, r.center, size, fg, bold=self.kind == "primary",
                    anchor="center")
        if self.hotkey and r.width > 54:
            T.draw_text(surf, self.hotkey, (r.right - 6, r.centery + 1), self.size - 11,
                        T.TEXT_FAINT, anchor="midright")


# --------------------------------------------------------------------------- #
class LogView:
    """可滚动的作战日志。"""

    # 颜色按当前主题动态解析：写成类属性会在导入时固化，主题切换后不生效
    @staticmethod
    def kind_color(kind: str) -> tuple[int, int, int]:
        return {
            "info": T.TEXT_DIM,
            "combat": T.mix(T.AMBER, T.RED, 0.35),
            "detect": T.mix(T.BLUE, T.CYAN, 0.4),
            "ew": T.PURPLE,
            "move": T.mix(T.GREEN, T.TEXT_DIM, 0.5),
            "system": T.CYAN,
            "victory": T.mix(T.AMBER, T.WHITE, 0.4),
            "tutorial": T.mix(T.GREEN, T.WHITE, 0.35),
        }.get(kind, T.TEXT_DIM)

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
            color = self.kind_color(e.kind)
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
    pygame.draw.rect(surf, T.PANEL, rect, border_radius=4)
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
       width: int, key_color=None, val_color=None, size: int = 12) -> int:
    # 颜色延迟解析：默认参数会在定义时固化，主题切换后就改不动了
    if key_color is None:
        key_color = T.TEXT_FAINT
    if val_color is None:
        val_color = T.TEXT
    T.draw_text(surf, key, (x, y), size, key_color)
    T.draw_text(surf, value, (x + width, y), size, val_color, anchor="topright", mono=True)
    return y + size + 4
