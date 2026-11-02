"""设置面板：键位自定义 + 界面主题 + 音效 + 语言。

以模态浮层形式提供：
* 逐条查看与修改动作绑定；
* 一键恢复默认键位；
* 冲突检测（同一键绑定到多个动作时高亮提示）；
* 主题 / 音效 / 语言切换（即时生效）。

面板本身不保存配置，保存动作交给调用方（App），以便统一处理错误提示。
"""
from __future__ import annotations

import pygame

from core.config import ALL_ACTIONS, ACTION_LABELS, DEFAULT_BINDINGS, SLOT_ACTIONS, UserConfig, label_for
from core import i18n
from core.i18n import available_languages, language_name, tr

from . import theme as T
from .keymap import capture_key_name, hotkey_label, key_name


class SettingsPanel:
    """设置浮层。"""

    ROW_H = 26
    HEADER_H = 92
    FOOTER_H = 62
    OPTIONS_H = 108          # 主题 / 音效 / 语言区块高度

    def __init__(self, cfg: UserConfig, on_theme=None, on_sound=None, on_language=None):
        self.cfg = cfg
        self.capturing: str = ""        # 正在等待按键的动作 ID
        self.scroll = 0
        self.message = ""
        self.message_color = T.TEXT_DIM
        self.rect = pygame.Rect(0, 0, 640, 560)
        self.rows: list[tuple[pygame.Rect, str, str]] = []
        self._btn_reset = pygame.Rect(0, 0, 0, 0)
        self._btn_close = pygame.Rect(0, 0, 0, 0)
        # 回调：主题 / 音效 / 语言的即时应用
        self.on_theme = on_theme
        self.on_sound = on_sound
        self.on_language = on_language
        # 选项控件
        self._theme_buttons: list[tuple[pygame.Rect, str]] = []
        self._lang_buttons: list[tuple[pygame.Rect, str]] = []
        self._sound_btn = pygame.Rect(0, 0, 0, 0)
        self._vol_left = pygame.Rect(0, 0, 0, 0)
        self._vol_right = pygame.Rect(0, 0, 0, 0)
        self._vol_bar = pygame.Rect(0, 0, 0, 0)

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
            # 主题切换
            for rect, tid in self._theme_buttons:
                if rect.collidepoint(e.pos):
                    if self.cfg.set_theme(tid):
                        if self.on_theme:
                            self.on_theme(tid)
                        self.message = f"界面主题：{T.theme_name(tid)}"
                        self.message_color = T.GREEN
                    return True
            # 语言切换
            for rect, code in self._lang_buttons:
                if rect.collidepoint(e.pos):
                    if self.cfg.set_language(code):
                        if self.on_language:
                            self.on_language(code)
                        self.message = f"界面语言：{language_name(code)}"
                        self.message_color = T.GREEN
                    return True
            # 音效开关
            if self._sound_btn.collidepoint(e.pos):
                self.cfg.set_sound(not self.cfg.sound_enabled)
                if self.on_sound:
                    self.on_sound(self.cfg.sound_enabled)
                self.message = "音效：" + ("开启" if self.cfg.sound_enabled else "关闭")
                self.message_color = T.GREEN if self.cfg.sound_enabled else T.TEXT_DIM
                return True
            # 音量调节
            if self._vol_left.collidepoint(e.pos):
                self.cfg.set_volume(self.cfg.volume - 0.1)
                if self.on_sound:
                    self.on_sound(self.cfg.sound_enabled)
                return True
            if self._vol_right.collidepoint(e.pos):
                self.cfg.set_volume(self.cfg.volume + 0.1)
                if self.on_sound:
                    self.on_sound(self.cfg.sound_enabled)
                return True
            if self._vol_bar.collidepoint(e.pos):
                self._set_volume_from_pos(e.pos[0])
                return True
            # 键位列表
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

    def _set_volume_from_pos(self, x: int) -> None:
        """按点击位置设置音量。"""
        if self._vol_bar.width <= 0:
            return
        ratio = (x - self._vol_bar.x) / self._vol_bar.width
        self.cfg.set_volume(max(0.0, min(1.0, ratio)))
        if self.on_sound:
            self.on_sound(self.cfg.sound_enabled)

    # ------------------------------------------------------------------ #
    # 绘制
    # ------------------------------------------------------------------ #
    def draw(self, s: pygame.Surface) -> None:
        sw, sh = s.get_size()
        w = min(700, sw - 120)
        h = min(700, sh - 80)
        r = pygame.Rect((sw - w) // 2, (sh - h) // 2, w, h)
        self.rect = r

        dim = pygame.Surface((sw, sh), pygame.SRCALPHA)
        dim.fill((*T.mix(T.BG, (0, 0, 0), 0.5), 190))
        s.blit(dim, (0, 0))

        T.panel(s, r, T.PANEL, T.BORDER_HI, radius=6)
        T.draw_text(s, tr("settings.title"), (r.x + 22, r.y + 16), 17, T.TEXT, bold=True)
        T.draw_text(s, tr("settings.keys") + " · " + tr("settings.theme") + " · "
                    + tr("settings.sound") + " · " + tr("settings.language"),
                    (r.x + 78, r.y + 21), 11, T.TEXT_FAINT)
        T.hline(s, r.x + 20, r.right - 20, r.y + 62, T.BORDER)

        self._draw_options(s, r)

        conflicts = self.cfg.conflicts()
        self.rows = []
        body = pygame.Rect(r.x + 16, r.y + self.HEADER_H + self.OPTIONS_H - 24,
                           r.width - 32, r.height - self.HEADER_H - self.FOOTER_H
                           - self.OPTIONS_H + 8)
        list_r = pygame.Rect(body.x, body.y, body.width, body.height)

        # 分两栏：主动作 / 武器槽
        col_w = (list_r.width - 12) // 2
        groups = [
            ("常规操作", [a for a in ALL_ACTIONS if a not in SLOT_ACTIONS]),
            ("武器快捷槽", list(SLOT_ACTIONS)),
        ]
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
                    pygame.draw.rect(s, T.PANEL_HI, row, border_radius=3)
                label = label_for(action)
                key = hotkey_label(self.cfg, action)
                is_capturing = self.capturing == action
                if is_capturing:
                    pygame.draw.rect(s, T.mix(T.PANEL, T.AMBER, 0.22), row, border_radius=3)
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

        # 底部按钮
        by = r.bottom - self.FOOTER_H + 8
        self._btn_reset = pygame.Rect(r.x + 16, by, 150, 32)
        self._btn_close = pygame.Rect(r.right - 16 - 150, by, 150, 32)
        for b, text, primary in ((self._btn_reset, tr("settings.reset"), False),
                                 (self._btn_close, tr("settings.done") + " [Esc]", True)):
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

        if conflicts:
            msg = f"注意：{len(conflicts)} 个按键被多个动作共用，已按列表优先顺序生效"
            T.draw_text(s, msg, (r.x + 176, by + 9), 10, T.AMBER)
        elif self.message:
            T.draw_text(s, self.message, (r.x + 176, by + 9), 11, self.message_color)

    # ------------------------------------------------------------------ #
    def _draw_options(self, s: pygame.Surface, r: pygame.Rect) -> None:
        """主题 / 音效 / 语言三个区块。"""
        y = r.y + self.HEADER_H - 16

        # -- 主题 --
        T.draw_text(s, tr("settings.theme"), (r.x + 22, y), 11, T.CYAN, bold=True)
        self._theme_buttons = []
        tx = r.x + 100
        for tid in T.theme_ids():
            name = T.theme_name(tid)
            bw = max(72, T.text_width(name, 11) + 20)
            b = pygame.Rect(tx, y - 4, bw, 24)
            active = (tid == T.current_theme().id)
            hover = b.collidepoint(pygame.mouse.get_pos())
            fill = T.mix(T.PANEL, T.CYAN, 0.26) if active else (T.PANEL_HI if hover else T.PANEL)
            pygame.draw.rect(s, fill, b, border_radius=3)
            pygame.draw.rect(s, T.CYAN if active else (T.BORDER_HI if hover else T.BORDER),
                             b, 1, border_radius=3)
            T.draw_text(s, name, b.center, 11, T.TEXT if active else T.TEXT_DIM,
                        anchor="center")
            self._theme_buttons.append((b, tid))
            tx += bw + 8
        y += 34

        # -- 音效 --
        T.draw_text(s, tr("settings.sound"), (r.x + 22, y), 11, T.CYAN, bold=True)
        self._sound_btn = pygame.Rect(r.x + 100, y - 4, 76, 24)
        on = self.cfg.sound_enabled
        hover = self._sound_btn.collidepoint(pygame.mouse.get_pos())
        fill = T.mix(T.PANEL, T.GREEN, 0.24) if on else T.PANEL
        pygame.draw.rect(s, fill, self._sound_btn, border_radius=3)
        pygame.draw.rect(s, T.GREEN if on else (T.BORDER_HI if hover else T.BORDER),
                         self._sound_btn, 1, border_radius=3)
        T.draw_text(s, ("开启" if on else "关闭") if i18n.current_language() == "zh"
                        else ("On" if on else "Off"), self._sound_btn.center, 11,
                    T.TEXT, anchor="center")
        # 音量
        vol_x = r.x + 190
        self._vol_left = pygame.Rect(vol_x, y - 4, 24, 24)
        self._vol_right = pygame.Rect(vol_x + 168, y - 4, 24, 24)
        self._vol_bar = pygame.Rect(vol_x + 30, y + 4, 132, 12)
        for btn, sym in ((self._vol_left, "−"), (self._vol_right, "+")):
            hv = btn.collidepoint(pygame.mouse.get_pos())
            pygame.draw.rect(s, T.PANEL_HI if hv else T.PANEL, btn, border_radius=3)
            pygame.draw.rect(s, T.BORDER_HI if hv else T.BORDER, btn, 1, border_radius=3)
            T.draw_text(s, sym, btn.center, 13, T.TEXT if hv else T.TEXT_DIM, anchor="center")
        pygame.draw.rect(s, T.PANEL_SUNK, self._vol_bar, border_radius=3)
        vw = int(self._vol_bar.width * self.cfg.volume)
        if vw > 0:
            pygame.draw.rect(s, T.GREEN, pygame.Rect(self._vol_bar.x, self._vol_bar.y,
                                                      vw, self._vol_bar.height), border_radius=3)
        T.draw_text(s, f"{int(self.cfg.volume * 100)}%", (self._vol_right.right + 12, y + 2),
                    11, T.TEXT_DIM, mono=True)
        y += 34

        # -- 语言 --
        T.draw_text(s, tr("settings.language"), (r.x + 22, y), 11, T.CYAN, bold=True)
        self._lang_buttons = []
        lx = r.x + 100
        for code in available_languages():
            name = language_name(code)
            bw = max(64, T.text_width(name, 11) + 20)
            b = pygame.Rect(lx, y - 4, bw, 24)
            active = (code == self.cfg.language)
            hover = b.collidepoint(pygame.mouse.get_pos())
            fill = T.mix(T.PANEL, T.CYAN, 0.26) if active else (T.PANEL_HI if hover else T.PANEL)
            pygame.draw.rect(s, fill, b, border_radius=3)
            pygame.draw.rect(s, T.CYAN if active else (T.BORDER_HI if hover else T.BORDER),
                             b, 1, border_radius=3)
            T.draw_text(s, name, b.center, 11, T.TEXT if active else T.TEXT_DIM,
                        anchor="center")
            self._lang_buttons.append((b, code))
            lx += bw + 8

        T.hline(s, r.x + 20, r.right - 20, r.y + self.HEADER_H + self.OPTIONS_H - 26,
                T.BORDER)


__all__ = ["SettingsPanel"]
