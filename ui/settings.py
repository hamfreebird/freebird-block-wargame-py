"""设置面板：键位自定义。

以模态浮层形式提供：
* 逐条查看与修改动作绑定；
* 一键恢复默认键位；
* 冲突检测（同一键绑定到多个动作时高亮提示）。

面板本身不保存配置，保存动作交给调用方（App），以便统一处理错误提示。
"""
from __future__ import annotations

import pygame

from core.config import ALL_ACTIONS, ACTION_LABELS, DEFAULT_BINDINGS, SLOT_ACTIONS, UserConfig, label_for

from . import theme as T
from .keymap import capture_key_name, hotkey_label, key_name


class SettingsPanel:
    """键位设置浮层。"""

    ROW_H = 26
    HEADER_H = 92
    FOOTER_H = 62

    def __init__(self, cfg: UserConfig):
        self.cfg = cfg
        self.capturing: str = ""        # 正在等待按键的动作 ID
        self.scroll = 0
        self.message = ""
        self.message_color = T.TEXT_DIM
        self.rect = pygame.Rect(0, 0, 640, 560)
        self.rows: list[tuple[pygame.Rect, str, str]] = []
        self._btn_reset = pygame.Rect(0, 0, 0, 0)
        self._btn_close = pygame.Rect(0, 0, 0, 0)
        self._btn_defaults_all = pygame.Rect(0, 0, 0, 0)

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def handle_event(self, e: pygame.event.Event) -> bool:
        """处理事件；返回 True 表示事件已被面板消费。"""
        if e.type == pygame.KEYDOWN:
            if self.capturing:
                if e.key == pygame.K_ESCAPE:
                    self.capturing = ""
                    self.message = "已取消本次修改"
                    self.message_color = T.TEXT_DIM
                    return True
                name = capture_key_name(e)
                if name:
                    action = self.capturing
                    self.cfg.set_key(action, name)
                    self.capturing = ""
                    self.message = f"{label_for(action)} → {hotkey_label(self.cfg, action)}"
                    self.message_color = T.GREEN
                return True
            if e.key == pygame.K_ESCAPE:
                self.close_requested = True
                return True
            return True

        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            for rect, action, _ in self.rows:
                if rect.collidepoint(e.pos):
                    if self.cfg.key_for(action) == DEFAULT_BINDINGS.get(action):
                        self.message = ""
                    self.capturing = action
                    self.message_color = T.AMBER
                    self.message = f"请按下新的按键（当前：{label_for(action)}）"
                    return True
            if self._btn_reset.collidepoint(e.pos):
                self.cfg.reset_keys()
                self.message = "已恢复默认键位"
                self.message_color = T.GREEN
                return True
            if self._btn_close.collidepoint(e.pos):
                self.close_requested = True
                return True
            return True
        return False

    # ------------------------------------------------------------------ #
    # 绘制
    # ------------------------------------------------------------------ #
    def draw(self, s: pygame.Surface) -> None:
        sw, sh = s.get_size()
        w = min(680, sw - 120)
        h = min(660, sh - 80)
        r = pygame.Rect((sw - w) // 2, (sh - h) // 2, w, h)
        self.rect = r

        dim = pygame.Surface((sw, sh), pygame.SRCALPHA)
        dim.fill((5, 8, 12, 190))
        s.blit(dim, (0, 0))

        T.panel(s, r, (14, 19, 27), T.BORDER_HI, radius=6)
        T.draw_text(s, "设置 · 键位自定义", (r.x + 22, r.y + 16), 17, T.TEXT, bold=True)
        T.draw_text(s, "点击任一动作后按下新按键；Esc 取消本次修改",
                    (r.x + 22, r.y + 40), 11, T.TEXT_FAINT)
        T.hline(s, r.x + 20, r.right - 20, r.y + 62, T.BORDER)

        conflicts = self.cfg.conflicts()
        self.rows = []
        body = pygame.Rect(r.x + 16, r.y + self.HEADER_H - 24, r.width - 32,
                           r.height - self.HEADER_H - self.FOOTER_H + 8)
        list_r = pygame.Rect(body.x, body.y, body.width, body.height)

        # 分两栏：主动作 / 武器槽
        col_w = (list_r.width - 12) // 2
        groups = [
            ("常规操作", [a for a in ALL_ACTIONS if a not in SLOT_ACTIONS]),
            ("武器快捷槽", list(SLOT_ACTIONS)),
        ]
        max_scroll = 0
        for ci, (title, actions) in enumerate(groups):
            cx = list_r.x + ci * (col_w + 12)
            y = list_r.y
            T.draw_text(s, title, (cx, y), 11, T.CYAN, bold=True)
            y += 18
            for action in actions:
                row = pygame.Rect(cx, y, col_w, self.ROW_H - 4)
                if row.bottom > list_r.bottom:
                    break
                hovered = row.collidepoint(pygame.mouse.get_pos())
                if hovered:
                    pygame.draw.rect(s, (22, 30, 40), row, border_radius=3)
                label = label_for(action)
                key = hotkey_label(self.cfg, action)
                is_capturing = self.capturing == action
                if is_capturing:
                    pygame.draw.rect(s, (46, 40, 16), row, border_radius=3)
                    pygame.draw.rect(s, T.AMBER, row, 1, border_radius=3)
                    key = "…"
                    kcol = T.AMBER
                elif key in conflicts.get(self.cfg.key_for(action), []):
                    kcol = T.RED
                elif key == DEFAULT_BINDINGS.get(action):
                    kcol = T.TEXT_DIM
                else:
                    kcol = T.CYAN
                T.draw_text(s, label, (row.x + 8, row.y + 4), 12,
                            T.TEXT if hovered else T.TEXT_DIM)
                T.draw_text(s, key, (row.right - 8, row.y + 4), 11, kcol,
                            anchor="topright", mono=True)
                self.rows.append((pygame.Rect(row), action, key))
                y += self.ROW_H
            max_scroll = max(max_scroll, y - list_r.bottom)

        # 底部按钮
        by = r.bottom - self.FOOTER_H + 8
        self._btn_reset = pygame.Rect(r.x + 16, by, 150, 32)
        self._btn_close = pygame.Rect(r.right - 16 - 150, by, 150, 32)
        for b, text, primary in ((self._btn_reset, "恢复默认键位", False),
                                 (self._btn_close, "完成 [Esc]", True)):
            hover = b.collidepoint(pygame.mouse.get_pos())
            if primary:
                fill = (30, 104, 128) if hover else (22, 78, 98)
                border = T.CYAN
                fg = T.WHITE
            else:
                fill = (30, 40, 54) if hover else (20, 28, 38)
                border = T.BORDER_HI if hover else T.BORDER
                fg = T.TEXT if hover else T.TEXT_DIM
            pygame.draw.rect(s, fill, b, border_radius=3)
            pygame.draw.rect(s, border, b, 1, border_radius=3)
            T.draw_text(s, text, b.center, 12, fg, anchor="center")

        if conflicts:
            msg = f"注意：{len(conflicts)} 个按键被多个动作共用，已按列表优先顺序生效"
            T.draw_text(s, msg, (r.x + 176, by + 9), 10, T.AMBER)
        elif self.message:
            T.draw_text(s, self.message, (r.x + 176, by + 9), 11, self.message_color)


__all__ = ["SettingsPanel"]
