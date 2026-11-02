"""想定包导入浮层。

玩家在主菜单点「导入想定」后弹出：从社区包目录（用户数据目录下的
`packages/`）里挑一个 `.bwscn` 文件导入。

设计要点
--------
* **列出全部候选**，而不是像旧实现那样「导入最近修改的一个」——
  玩家得能自己挑；
* 每个包显示清单信息（名称、作者、想定数量、文件大小、修改时间）；
* 导入目标固定为**用户可写目录**下的 `maps/`，冻结版也能用；
* 悬停即选中，单击确认导入，`Esc` 取消。

包本身是 zip 格式，清单用 `core.pkg.inspect_package` 读取；
读取失败的包仍会列出（标为「无法读取」），但不能导入——
这样玩家能看到「我放了个文件在这儿」，而不是「目录里什么都没有」。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import pygame

from core import pkg as _pkg

from . import theme as T


@dataclass
class PackageItem:
    """一个待导入的想定包。"""

    path: str
    name: str = ""
    author: str = ""
    map_count: int = 0
    size_kb: int = 0
    mtime: str = ""
    error: str = ""

    @property
    def readable(self) -> bool:
        return not self.error


def scan_packages(directory: str) -> list[PackageItem]:
    """扫描目录里的想定包，返回按修改时间倒序的列表。"""
    items: list[PackageItem] = []
    if not os.path.isdir(directory):
        return items
    try:
        raw = _pkg.list_packages(directory)
    except Exception:
        return items
    for p in raw:
        path = p["path"]
        item = PackageItem(path=path)
        try:
            item.size_kb = max(1, os.path.getsize(path) // 1024)
            item.mtime = _fmt_time(os.path.getmtime(path))
        except OSError:
            pass
        try:
            mf = _pkg.inspect_package(path)
            item.name = mf.name or os.path.basename(path)
            item.author = mf.author or ""
            item.map_count = len(mf.maps)
        except Exception as exc:
            item.error = str(exc)
            item.name = os.path.basename(path)
        items.append(item)
    items.sort(key=lambda it: (-_mtime_of(it.path), it.name))
    return items


def _mtime_of(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _fmt_time(ts: float) -> str:
    import time
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


class ImportDialog:
    """想定包导入浮层。"""

    ROW_H = 46
    HEADER_H = 76
    FOOTER_H = 56

    def __init__(self, directory: str, on_import=None):
        self.directory = directory
        self.items: list[PackageItem] = scan_packages(directory)
        self.index = 0
        self.message = ""
        self.message_color = T.TEXT_DIM
        self.rect = pygame.Rect(0, 0, 620, 460)
        self.scroll = 0
        self.rows: list[tuple[pygame.Rect, PackageItem]] = []
        self._btn_import = pygame.Rect(0, 0, 0, 0)
        self._btn_close = pygame.Rect(0, 0, 0, 0)
        self.on_import = on_import
        self._refresh()

    def _refresh(self) -> None:
        """重新扫描目录（导入后需要刷新列表）。"""
        self.items = scan_packages(self.directory)
        self.index = max(0, min(self.index, len(self.items) - 1)) if self.items else 0

    # ------------------------------------------------------------------ #
    def handle_event(self, e: pygame.event.Event) -> bool:
        """处理事件；返回 True 表示已被消费。"""
        if e.type == pygame.KEYDOWN:
            if e.key == pygame.K_ESCAPE:
                self.close_requested = True
                return True
            if e.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                self._do_import()
                return True
            if e.key == pygame.K_UP:
                self.index = max(0, self.index - 1)
                return True
            if e.key == pygame.K_DOWN:
                self.index = min(max(0, len(self.items) - 1), self.index + 1)
                return True
            return True

        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            for rect, item in self.rows:
                if rect.collidepoint(e.pos):
                    self.index = self.items.index(item)
                    self._do_import()
                    return True
            if self._btn_import.collidepoint(e.pos):
                self._do_import()
                return True
            if self._btn_close.collidepoint(e.pos):
                self.close_requested = True
                return True
            return True

        if e.type in (pygame.MOUSEMOTION, pygame.MOUSEBUTTONUP, pygame.MOUSEWHEEL):
            return True
        return False

    def _do_import(self) -> None:
        """执行导入。"""
        if not self.items:
            self.message = "没有可导入的想定包"
            self.message_color = T.AMBER
            return
        item = self.items[self.index]
        if not item.readable:
            self.message = f"该包无法读取：{item.error}"
            self.message_color = T.RED
            return
        if self.on_import:
            self.on_import(item.path)
        self._refresh()

    # ------------------------------------------------------------------ #
    def draw(self, s: pygame.Surface) -> None:
        sw, sh = s.get_size()
        w = min(680, sw - 100)
        h = min(520, sh - 80)
        r = pygame.Rect((sw - w) // 2, (sh - h) // 2, w, h)
        self.rect = r

        dim = pygame.Surface((sw, sh), pygame.SRCALPHA)
        dim.fill((*T.mix(T.BG, (0, 0, 0), 0.5), 200))
        s.blit(dim, (0, 0))

        T.panel(s, r, T.PANEL, T.BORDER_HI, radius=6)
        T.draw_text(s, "导入想定包", (r.x + 22, r.y + 16), 16, T.TEXT, bold=True)
        T.draw_text(s, "从社区包目录选择要导入的 .bwscn 文件",
                    (r.x + 22, r.y + 40), 11, T.TEXT_FAINT)
        T.draw_text(s, self.directory, (r.right - 22, r.y + 20), 10, T.TEXT_FAINT,
                    anchor="topright")
        T.hline(s, r.x + 20, r.right - 20, r.y + self.HEADER_H - 8, T.BORDER)

        # 列表
        body = pygame.Rect(r.x + 16, r.y + self.HEADER_H,
                           r.width - 32, r.height - self.HEADER_H - self.FOOTER_H)
        self.rows = []
        if not self.items:
            T.draw_text(s, "目录中没有 .bwscn 文件", body.center, 13, T.TEXT_DIM,
                        anchor="center")
            T.draw_text(s, "把社区想定包放进上面的目录，然后重新打开本窗口",
                        (body.centerx, body.centery + 22), 11, T.TEXT_FAINT, anchor="center")
        else:
            vis = max(1, body.height // self.ROW_H)
            self.scroll = max(0, min(self.scroll, max(0, len(self.items) - vis)))
            start = self.scroll
            end = min(len(self.items), start + vis)
            for i in range(start, end):
                item = self.items[i]
                row = pygame.Rect(body.x, body.y + (i - start) * self.ROW_H,
                                  body.width, self.ROW_H - 6)
                active = i == self.index
                if active:
                    pygame.draw.rect(s, T.mix(T.PANEL, T.CYAN, 0.16), row, border_radius=3)
                    pygame.draw.rect(s, T.CYAN, row, 1, border_radius=3)
                name_col = T.TEXT if item.readable else T.TEXT_FAINT
                T.draw_text(s, item.name[:26], (row.x + 10, row.y + 4), 12, name_col,
                            bold=active)
                sub = (f"{item.author} · {item.map_count} 张想定 · {item.size_kb} KB"
                       if item.readable else f"无法读取：{item.error[:28]}")
                T.draw_text(s, sub, (row.x + 10, row.y + 22), 10,
                            T.TEXT_DIM if item.readable else T.RED)
                T.draw_text(s, item.mtime, (row.right - 10, row.y + 12), 10, T.TEXT_FAINT,
                            anchor="topright", mono=True)
                self.rows.append((pygame.Rect(row), item))

        # 底部按钮
        by = r.bottom - self.FOOTER_H + 8
        self._btn_import = pygame.Rect(r.right - 16 - 130, by, 130, 32)
        self._btn_close = pygame.Rect(r.x + 16, by, 96, 32)
        for b, text, primary in ((self._btn_import, "导入", True),
                                 (self._btn_close, "取消 [Esc]", False)):
            hover = b.collidepoint(pygame.mouse.get_pos())
            if primary:
                fill = T.mix(T.PANEL, T.CYAN, 0.28 if hover else 0.18)
                border = T.CYAN
                fg = T.TEXT
            else:
                fill = T.PANEL_HI if hover else T.PANEL
                border = T.BORDER_HI if hover else T.BORDER
                fg = T.TEXT if hover else T.TEXT_DIM
            pygame.draw.rect(s, fill, b, border_radius=3)
            pygame.draw.rect(s, border, b, 1, border_radius=3)
            T.draw_text(s, text, b.center, 12, fg, anchor="center")

        if self.message:
            T.draw_text(s, self.message, (r.x + 128, by + 9), 11, self.message_color)
        elif len(self.items) > 1:
            T.draw_text(s, "↑↓ 选择 · Enter 导入 · 悬停点击亦可",
                        (r.x + 128, by + 9), 10, T.TEXT_FAINT)


__all__ = ["ImportDialog", "PackageItem", "scan_packages"]
