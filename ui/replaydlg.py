"""回放导入浮层。

玩家在主菜单点「导入回放」后弹出：列出用户数据目录 `replays/` 下的
`.bwrep` 文件，选一个即可重演那一局。

回放文件里存的是**想定 + 种子 + 命令流**，所以回放不依赖当初那台
机器的分辨率、鼠标位置或设置——只要想定还在，任何人都能看同一局。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import pygame

from core.i18n import tr
from core.replay import ReplayFile

from . import theme as T


@dataclass
class ReplayItem:
    """一个待播放的回放。"""

    path: str
    name: str = ""
    scenario: str = ""
    seed: int = 0
    commands: int = 0
    size_kb: int = 0
    mtime: str = ""
    error: str = ""
    kind: str = "match"
    #: 回放是否内嵌了想定（换机器也能放）
    self_contained: bool = False

    @property
    def readable(self) -> bool:
        return not self.error


def scan_replays(directory: str) -> list[ReplayItem]:
    """扫描回放目录，返回按修改时间倒序的列表。"""
    items: list[ReplayItem] = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return items
    for name in names:
        if not name.lower().endswith(".bwrep"):
            continue
        path = os.path.join(directory, name)
        item = ReplayItem(path=path, name=name)
        try:
            item.size_kb = max(1, os.path.getsize(path) // 1024)
            item.mtime = time.strftime("%m-%d %H:%M",
                                       time.localtime(os.path.getmtime(path)))
        except OSError:
            pass
        try:
            rf = ReplayFile.load(path)
            item.scenario = rf.scenario
            item.seed = rf.seed
            item.commands = len(rf.commands)
            item.kind = rf.kind
            item.self_contained = rf.scenario_data is not None
        except Exception as exc:
            item.error = str(exc)
        items.append(item)
    items.sort(key=lambda it: it.name)
    items.reverse()
    return items


class ReplayDialog:
    """回放列表浮层。"""

    ROW_H = 46
    HEADER_H = 76
    FOOTER_H = 56

    def __init__(self, directory: str, on_play=None):
        self.directory = directory
        self.items: list[ReplayItem] = scan_replays(directory)
        self.index = 0
        self.message = ""
        self.message_color = T.TEXT_DIM
        self.rect = pygame.Rect(0, 0, 620, 460)
        self.scroll = 0
        self.rows: list[tuple[pygame.Rect, ReplayItem]] = []
        self._btn_play = pygame.Rect(0, 0, 0, 0)
        self._btn_close = pygame.Rect(0, 0, 0, 0)
        self.on_play = on_play
        self.close_requested = False
        self._refresh()

    def _refresh(self) -> None:
        self.items = scan_replays(self.directory)
        self.index = (max(0, min(self.index, len(self.items) - 1))
                      if self.items else 0)

    # ------------------------------------------------------------------ #
    def handle_event(self, e: pygame.event.Event) -> bool:
        if e.type == pygame.KEYDOWN:
            if e.key == pygame.K_ESCAPE:
                self.close_requested = True
                return True
            if e.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                self._do_play()
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
                    self._do_play()
                    return True
            if self._btn_play.collidepoint(e.pos):
                self._do_play()
                return True
            if self._btn_close.collidepoint(e.pos):
                self.close_requested = True
                return True
            return True

        if e.type in (pygame.MOUSEMOTION, pygame.MOUSEBUTTONUP, pygame.MOUSEWHEEL):
            return True
        return False

    def _do_play(self) -> None:
        if not self.items:
            self.message = "没有可播放的回放"
            self.message_color = T.AMBER
            return
        item = self.items[self.index]
        if not item.readable:
            self.message = f"该回放无法读取：{item.error}"
            self.message_color = T.RED
            return
        if self.on_play:
            self.on_play(item.path)

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
        T.draw_text(s, tr("ui.import_replay_title"), (r.x + 22, r.y + 16), 16, T.TEXT, bold=True)
        T.draw_text(s, tr("ui.import_replay_desc"), (r.x + 22, r.y + 40),
                    11, T.TEXT_FAINT)
        T.draw_text(s, self.directory, (r.right - 22, r.y + 20), 11, T.TEXT_FAINT,
                    anchor="topright")
        T.hline(s, r.x + 20, r.right - 20, r.y + self.HEADER_H - 8, T.BORDER)

        body = pygame.Rect(r.x + 16, r.y + self.HEADER_H,
                           r.width - 32, r.height - self.HEADER_H - self.FOOTER_H)
        self.rows = []
        if not self.items:
            T.draw_text(s, tr("ui.import_replay_empty"), body.center, 13, T.TEXT_DIM,
                        anchor="center")
            T.draw_text(s, tr("ui.import_replay_hint"),
                        (body.centerx, body.centery + 22), 11, T.TEXT_FAINT,
                        anchor="center")
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
                T.draw_text(s, T.ellipsize(item.name, 12, row.width - 120),
                            (row.x + 10, row.y + 4), 12, name_col, bold=active)
                if item.readable:
                    # 观战回放没有命令流，写成「0 条操作」会让人以为文件坏了
                    detail = ("AI 对战 · 种子" if item.kind == "ai_vs_ai"
                              else f"{item.commands} 条操作 · 种子")
                    sub = f"{item.scenario or '未知想定'} · {detail} {item.seed}"
                    if item.self_contained:
                        sub += " · 含想定"
                else:
                    sub = f"无法读取：{item.error[:30]}"
                T.draw_text(s, sub, (row.x + 10, row.y + 22), 11,
                            T.TEXT_DIM if item.readable else T.RED)
                T.draw_text(s, item.mtime, (row.right - 10, row.y + 12), 11,
                            T.TEXT_FAINT, anchor="topright", mono=True)
                self.rows.append((pygame.Rect(row), item))

        by = r.bottom - self.FOOTER_H + 8
        self._btn_play = pygame.Rect(r.right - 16 - 130, by, 130, 32)
        self._btn_close = pygame.Rect(r.x + 16, by, 96, 32)
        for b, text, primary in ((self._btn_play, "播放", True),
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
            T.draw_text(s, tr("ui.import_replay_nav"),
                        (r.x + 128, by + 9), 11, T.TEXT_FAINT)


__all__ = ["ReplayDialog", "ReplayItem", "scan_replays"]
