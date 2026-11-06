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
from core import icons as _icons

from . import theme as T
from .keymap import capture_key_name, hotkey_label, key_name


class SettingsPanel:
    """设置浮层。"""

    ROW_H = 26
    HEADER_H = 92
    FOOTER_H = 62
    # 主题 / 字号 / 形状标识 / 图标包 / 音效 / 语言 六个区块，每块 34px。
    # 高度必须跟着字号档位走（`T.m`），否则放大后最后一块会压到键位列表上。
    OPTIONS_ROWS = 6
    OPTIONS_ROW_H = 34
    OPTIONS_H = OPTIONS_ROWS * OPTIONS_ROW_H

    def __init__(self, cfg: UserConfig, on_theme=None, on_sound=None, on_language=None,
                 on_icons=None, on_font_scale=None, on_shape_cues=None):
        self.cfg = cfg
        self.capturing: str = ""        # 正在等待按键的动作 ID
        self.scroll = 0
        self.message = ""
        self.message_color = T.TEXT_DIM
        # v3.0 无障碍：键盘焦点。此前非捕获态只认 Esc，整个面板纯鼠标。
        self.focus_index = 0
        # 显式初始化——以前只在事件里赋值，调用方不得不用 getattr 兜底
        self.close_requested = False
        self.rect = pygame.Rect(0, 0, 640, 560)
        self.rows: list[tuple[pygame.Rect, str, str]] = []
        self._btn_reset = pygame.Rect(0, 0, 0, 0)
        self._btn_close = pygame.Rect(0, 0, 0, 0)
        # 回调：主题 / 音效 / 语言 / 图标包 / 字号 / 形状标识的即时应用
        self.on_theme = on_theme
        self.on_sound = on_sound
        self.on_language = on_language
        self.on_icons = on_icons
        self.on_font_scale = on_font_scale
        self.on_shape_cues = on_shape_cues
        # 选项控件
        self._theme_buttons: list[tuple[pygame.Rect, str]] = []
        self._lang_buttons: list[tuple[pygame.Rect, str]] = []
        self._icon_buttons: list[tuple[pygame.Rect, str]] = []
        self._scale_buttons: list[tuple[pygame.Rect, str]] = []
        self._shape_buttons: list[tuple[pygame.Rect, str]] = []
        self._sound_btn = pygame.Rect(0, 0, 0, 0)
        self._vol_left = pygame.Rect(0, 0, 0, 0)
        self._vol_right = pygame.Rect(0, 0, 0, 0)
        self._vol_bar = pygame.Rect(0, 0, 0, 0)
        # 可用图标包（启动时扫描一次）
        self._icon_packs = _icons.list_packs()

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
            # -- 纯键盘导航（v3.0 无障碍）--------------------------------
            # ↑↓ / Tab 在焦点项之间移动，←→ 调整当前项的值，Enter 触发按钮，
            # Esc 关闭。焦点项顺序与视觉从上到下一致，见 _focus_items()。
            if e.key == pygame.K_ESCAPE:
                self.close_requested = True
                return True
            if e.key in (pygame.K_UP, pygame.K_DOWN, pygame.K_TAB):
                step = -1 if e.key == pygame.K_UP else 1
                n = len(self._focus_items())
                self.focus_index = (self.focus_index + step) % n
                return True
            if e.key in (pygame.K_LEFT, pygame.K_RIGHT):
                self._focus_adjust(-1 if e.key == pygame.K_LEFT else 1)
                return True
            if e.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
                self._focus_activate()
                return True
            # 其余键一律吞掉：设置面板是模态，不能漏给底下的地图
            return True

        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            # 鼠标与键盘共用下面那组操作方法，避免两处逻辑慢慢分叉
            for rect, tid in self._theme_buttons:
                if rect.collidepoint(e.pos):
                    self._select_theme(tid)
                    return True
            for rect, sid in self._scale_buttons:
                if rect.collidepoint(e.pos):
                    self._select_font_scale(sid)
                    return True
            for rect, mode in self._shape_buttons:
                if rect.collidepoint(e.pos):
                    self._select_shape_cues(mode)
                    return True
            for rect, code in self._lang_buttons:
                if rect.collidepoint(e.pos):
                    self._select_language(code)
                    return True
            for rect, pid in self._icon_buttons:
                if rect.collidepoint(e.pos):
                    self._select_icons(pid)
                    return True
            if self._sound_btn.collidepoint(e.pos):
                self._toggle_sound()
                return True
            if self._vol_left.collidepoint(e.pos):
                self._change_volume(-0.1)
                return True
            if self._vol_right.collidepoint(e.pos):
                self._change_volume(0.1)
                return True
            if self._vol_bar.collidepoint(e.pos):
                self._set_volume_from_pos(e.pos[0])
                return True
            for rect, action, _ in self.rows:
                if rect.collidepoint(e.pos):
                    self._begin_capture(action)
                    return True
            if self._btn_reset.collidepoint(e.pos):
                self._reset_keys()
                return True
            if self._btn_close.collidepoint(e.pos):
                self.close_requested = True
                return True
            return True
        # 设置面板是模态：其余鼠标事件（移动、抬起、滚轮）也一并吞掉，
        # 避免穿透到底下的地图与侧栏。
        if e.type in (pygame.MOUSEMOTION, pygame.MOUSEBUTTONUP, pygame.MOUSEWHEEL):
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
    # 共享操作：鼠标与键盘都走这里，避免两处逻辑分叉
    # ------------------------------------------------------------------ #
    def _select_theme(self, tid: str) -> None:
        if self.cfg.set_theme(tid):
            if self.on_theme:
                self.on_theme(tid)
            self.message = f"界面主题：{T.theme_name(tid)}"
            self.message_color = T.GREEN

    def _select_language(self, code: str) -> None:
        if self.cfg.set_language(code):
            if self.on_language:
                self.on_language(code)
            self.message = f"界面语言：{language_name(code)}"
            self.message_color = T.GREEN

    def _select_font_scale(self, scale_id: str) -> None:
        if self.cfg.set_font_scale(scale_id):
            if self.on_font_scale:
                self.on_font_scale(scale_id)
            self.message = tr("settings.font_scale") + "：" + tr(f"ui.scale.{scale_id}")
            self.message_color = T.GREEN

    def _select_shape_cues(self, mode: str) -> None:
        if self.cfg.set_shape_cues(mode):
            if self.on_shape_cues:
                self.on_shape_cues(mode)
            self.message = tr("settings.shape_cues") + "：" + tr(f"ui.shape.{mode}")
            self.message_color = T.GREEN

    def _select_icons(self, pid: str) -> None:
        self.cfg.set_pref("icon_pack", "" if pid == "__none__" else pid)
        if self.on_icons:
            self.on_icons(self.cfg.pref("icon_pack", ""))
        name = "内置矢量图形" if pid == "__none__" else pid
        self.message = f"单位图标包：{name}"
        self.message_color = T.GREEN

    def _toggle_sound(self) -> None:
        self.cfg.set_sound(not self.cfg.sound_enabled)
        if self.on_sound:
            self.on_sound(self.cfg.sound_enabled)
        self.message = (tr("settings.sound") + ": "
                        + (tr("ui.on") if self.cfg.sound_enabled else tr("ui.off")))
        self.message_color = T.GREEN if self.cfg.sound_enabled else T.TEXT_DIM

    def _change_volume(self, delta: float) -> None:
        self.cfg.set_volume(self.cfg.volume + delta)
        if self.on_sound:
            self.on_sound(self.cfg.sound_enabled)

    def _begin_capture(self, action: str) -> None:
        if self.cfg.key_for(action) == DEFAULT_BINDINGS.get(action):
            self.message = ""
        self.capturing = action
        self.message_color = T.AMBER
        self.message = f"请按下新的按键（当前：{label_for(action)}）"

    def _reset_keys(self) -> None:
        self.cfg.reset_keys()
        self.message = "已恢复默认键位"
        self.message_color = T.GREEN

    # ------------------------------------------------------------------ #
    # 键盘焦点
    # ------------------------------------------------------------------ #
    def _focus_items(self) -> list[tuple[str, str]]:
        """按视觉从上到下列出所有可操作项。

        ``(种类, 参数)``：参数只对 ``key`` 有意义（动作 ID）。
        顶部 5 行各算一项，值用 ←→ 调；键位列表每项算一项，Enter 进入重绑定。
        """
        items = [("theme", ""), ("font_scale", ""), ("shape", ""),
                 ("icons", ""), ("sound", ""), ("volume", ""), ("language", "")]
        # 与 draw() 里的分栏顺序一致：左栏常规操作，右栏武器槽
        for a in ALL_ACTIONS:
            if a not in SLOT_ACTIONS:
                items.append(("key", a))
        for a in SLOT_ACTIONS:
            items.append(("key", a))
        items.append(("reset", ""))
        items.append(("done", ""))
        return items

    def _focus(self) -> tuple[str, str]:
        items = self._focus_items()
        self.focus_index = max(0, min(self.focus_index, len(items) - 1))
        return items[self.focus_index]

    def _focus_adjust(self, delta: int) -> None:
        """←→ 调整当前焦点项的值。"""
        kind, _ = self._focus()
        if kind == "theme":
            ids = T.theme_ids()
            cur = ids.index(T.current_theme().id) if T.current_theme().id in ids else 0
            self._select_theme(ids[(cur + delta) % len(ids)])
        elif kind == "font_scale":
            ids = T.font_scale_ids()
            cur = ids.index(T.font_scale_id()) if T.font_scale_id() in ids else 0
            self._select_font_scale(ids[(cur + delta) % len(ids)])
        elif kind == "shape":
            ids = [k for k, _ in T.SHAPE_MODES]
            cur = ids.index(T.shape_mode()) if T.shape_mode() in ids else 0
            self._select_shape_cues(ids[(cur + delta) % len(ids)])
        elif kind == "icons":
            entries = ["__none__"] + [p.id for p in self._icon_packs]
            cur = str(self.cfg.pref("icon_pack", "") or "") or "__none__"
            idx = entries.index(cur) if cur in entries else 0
            self._select_icons(entries[(idx + delta) % len(entries)])
        elif kind == "sound":
            self._toggle_sound()
        elif kind == "volume":
            self._change_volume(0.1 * delta)
        elif kind == "language":
            codes = available_languages()
            cur = codes.index(self.cfg.language) if self.cfg.language in codes else 0
            self._select_language(codes[(cur + delta) % len(codes)])

    def _focus_activate(self) -> None:
        """Enter / Space 触发当前焦点项。"""
        kind, arg = self._focus()
        if kind == "key":
            self._begin_capture(arg)
        elif kind == "sound":
            self._toggle_sound()
        elif kind == "reset":
            self._reset_keys()
        elif kind == "done":
            self.close_requested = True
        # theme/icons/language/volume 用 ←→ 调整，Enter 不重复触发

    # ------------------------------------------------------------------ #
    # 选项区块
    # ------------------------------------------------------------------ #
    def _label_col(self, r: pygame.Rect) -> tuple[int, int]:
        """选项标签列：(标签起点 x, 控件起点 x)。

        控件列按**最宽的标签**对齐，而不是写死 ``r.x + T.m(100)``——
        德语「Schriftgröße」这类长标签在放大档位下会直接压到按钮上，
        而写死更窄的话短标签又白留一大截。
        """
        labels = [tr("settings.theme"), tr("settings.font_scale"), tr("settings.shape_cues"),
                  tr("ui.settings_icon_pack"), tr("settings.sound"), tr("settings.language")]
        lx = r.x + T.m(22)
        widest = max(T.text_width(lb, 11, bold=True) for lb in labels)
        return lx, max(r.x + T.m(100), lx + widest + T.m(14))

    def _option_row(self, s: pygame.Surface, r: pygame.Rect, y: int, label: str) -> int:
        """画一行选项的标题，返回按钮起始 x。"""
        lx, cx = self._label_col(r)
        T.draw_text(s, label, (lx, y), 11, T.CYAN, bold=True)
        return cx

    def _draw_choice(self, s: pygame.Surface, x: int, y: int, name: str,
                     active: bool, width: int | None = None) -> tuple[pygame.Rect, int]:
        """画一个选项按钮，返回 (矩形, 下一个按钮的 x)。

        ``width`` 给定时用它，不给就按文字宽度自适应。语言行语言多、
        放大档位下放不下，必须由调用方按剩余宽度均分——否则最后一个
        按钮会直接跑到面板外面去。
        """
        bw = width if width is not None else max(T.m(72), T.text_width(name, 11) + T.m(20))
        b = pygame.Rect(x, y - T.m(4), bw, T.m(24))
        hover = b.collidepoint(pygame.mouse.get_pos())
        fill = T.mix(T.PANEL, T.CYAN, 0.26) if active else (T.PANEL_HI if hover else T.PANEL)
        pygame.draw.rect(s, fill, b, border_radius=3)
        pygame.draw.rect(s, T.CYAN if active else (T.BORDER_HI if hover else T.BORDER),
                         b, 1, border_radius=3)
        T.draw_text(s, T.ellipsize(name, 11, bw - T.m(10)), b.center, 11,
                    T.TEXT if active else T.TEXT_DIM, anchor="center")
        return b, b.right + T.m(8)

    def _fit_choice_width(self, r: pygame.Rect, x: int, count: int) -> int:
        """把 count 个按钮塞进 ``x`` 到面板右边距之间的宽度。"""
        avail = r.right - T.m(22) - x
        gap = T.m(8)
        return max(T.m(46), (avail - gap * max(0, count - 1)) // max(1, count))

    def _draw_options(self, s: pygame.Surface, r: pygame.Rect) -> None:
        """主题 / 字号 / 形状标识 / 图标包 / 音效 / 语言六个区块。"""
        y = r.y + T.m(self.HEADER_H - 16)
        step = T.m(self.OPTIONS_ROW_H)

        # -- 主题 --
        tx = self._option_row(s, r, y, tr("settings.theme"))
        self._theme_buttons = []
        for tid in T.theme_ids():
            b, tx = self._draw_choice(s, tx, y, T.theme_name(tid),
                                      tid == T.current_theme().id)
            self._theme_buttons.append((b, tid))
        y += step

        # -- 字号（无障碍：标准 / 大 / 特大）--
        fx = self._option_row(s, r, y, tr("settings.font_scale"))
        self._scale_buttons = []
        for sid in T.font_scale_ids():
            b, fx = self._draw_choice(s, fx, y, tr(f"ui.scale.{sid}"),
                                      sid == T.font_scale_id())
            self._scale_buttons.append((b, sid))
        y += step

        # -- 形状标识（无障碍：自动 / 开 / 关）--
        sx = self._option_row(s, r, y, tr("settings.shape_cues"))
        self._shape_buttons = []
        for mode, _label in T.SHAPE_MODES:
            b, sx = self._draw_choice(s, sx, y, tr(f"ui.shape.{mode}"),
                                      mode == T.shape_mode())
            self._shape_buttons.append((b, mode))
        y += step

        # -- 图标包 --
        ix = self._option_row(s, r, y, tr("ui.settings_icon_pack"))
        self._icon_buttons = []
        cur = str(self.cfg.pref("icon_pack", "") or "")
        entries: list[tuple[str, str]] = [("__none__", "内置矢量")]
        for p in self._icon_packs:
            entries.append((p.id, p.name))
        for pid, name in entries:
            active = (cur == "" and pid == "__none__") or (cur == pid)
            b, ix = self._draw_choice(s, ix, y, name, active)
            self._icon_buttons.append((b, pid))
        y += step

        # -- 音效 --
        cx = self._option_row(s, r, y, tr("settings.sound"))
        self._sound_btn = pygame.Rect(cx, y - T.m(4), T.m(76), T.m(24))
        on = self.cfg.sound_enabled
        hover = self._sound_btn.collidepoint(pygame.mouse.get_pos())
        fill = T.mix(T.PANEL, T.GREEN, 0.24) if on else T.PANEL
        pygame.draw.rect(s, fill, self._sound_btn, border_radius=3)
        pygame.draw.rect(s, T.GREEN if on else (T.BORDER_HI if hover else T.BORDER),
                         self._sound_btn, 1, border_radius=3)
        T.draw_text(s, (tr("ui.on") if on else tr("ui.off")) if i18n.current_language() == "zh"
                        else ("On" if on else "Off"), self._sound_btn.center, 11,
                    T.TEXT, anchor="center")
        # 音量：跟在音效按钮右侧，位置由实测宽度推出来（「开启」在不同语言下宽度差一倍）
        vol_x = self._sound_btn.right + T.m(16)
        self._vol_left = pygame.Rect(vol_x, y - T.m(4), T.m(24), T.m(24))
        self._vol_right = pygame.Rect(vol_x + T.m(168), y - T.m(4), T.m(24), T.m(24))
        self._vol_bar = pygame.Rect(vol_x + T.m(30), y + T.m(4), T.m(132), T.m(12))
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
        T.draw_text(s, f"{int(self.cfg.volume * 100)}%", (self._vol_right.right + T.m(12),
                                                          y + T.m(2)),
                    11, T.TEXT_DIM, mono=True)
        y += step

        # -- 语言 --
        # 8 种语言 × 放大档位必然放不下，按剩余宽度均分；
        # 写死宽度会让最后一个按钮跑到面板外（这也是布局自检没覆盖到的位置）。
        lx = self._option_row(s, r, y, tr("settings.language"))
        self._lang_buttons = []
        codes = available_languages()
        bw = self._fit_choice_width(r, lx, len(codes))
        for code in codes:
            b, lx = self._draw_choice(s, lx, y, language_name(code),
                                      code == self.cfg.language, width=bw)
            self._lang_buttons.append((b, code))

        T.hline(s, r.x + T.m(20), r.right - T.m(20),
                r.y + T.m(self.HEADER_H + self.OPTIONS_H - 26), T.BORDER)

    # ------------------------------------------------------------------ #
    # 绘制
    # ------------------------------------------------------------------ #
    def draw(self, s: pygame.Surface) -> None:
        sw, sh = s.get_size()
        # 面板高度跟着字号档位长：放大后如果还卡在 700，键位列表会被挤没。
        # 上限取 sh - 40，留 20px 上下边距（布局自检要求不越界）。
        w = min(T.m(700), sw - 120)
        h = min(T.m(700), sh - 40)
        r = pygame.Rect((sw - w) // 2, (sh - h) // 2, w, h)
        self.rect = r

        dim = pygame.Surface((sw, sh), pygame.SRCALPHA)
        dim.fill((*T.mix(T.BG, (0, 0, 0), 0.5), 190))
        s.blit(dim, (0, 0))

        T.panel(s, r, T.PANEL, T.BORDER_HI, radius=6)
        title = tr("settings.title")
        T.draw_text(s, title, (r.x + T.m(22), r.y + T.m(16)), 17, T.TEXT, bold=True)
        # 副标题接在标题右侧，位置由**实测宽度**推出来——写死 x+78 的话，
        # 放大档位或德语长标题都会压上去。
        sub_x = r.x + T.m(22) + T.text_width(title, 17, bold=True) + T.m(12)
        T.draw_text(s, tr("settings.keys") + " · " + tr("settings.theme") + " · "
                    + tr("settings.sound") + " · " + tr("settings.language"),
                    (sub_x, r.y + T.m(21)), 11, T.TEXT_FAINT)
        # 键盘提示单独占一行，避免与上面那行在长译文下挤到一起
        T.draw_text(s, tr("ui.settings_kbd_hint"), (r.x + T.m(22), r.y + T.m(40)), 10,
                    T.TEXT_FAINT)
        T.hline(s, r.x + T.m(20), r.right - T.m(20), r.y + T.m(62), T.BORDER)

        self._draw_options(s, r)

        hh, oh, fh = T.m(self.HEADER_H), T.m(self.OPTIONS_H), T.m(self.FOOTER_H)
        rh = T.m(self.ROW_H)
        conflicts = self.cfg.conflicts()
        self.rows = []
        body = pygame.Rect(r.x + T.m(16), r.y + hh + oh - T.m(24),
                           r.width - T.m(32), r.height - hh - fh - oh + T.m(8))
        list_r = pygame.Rect(body.x, body.y, body.width, body.height)

        # 分两栏：主动作 / 武器槽
        col_w = (list_r.width - T.m(12)) // 2
        groups = [
            ("常规操作", [a for a in ALL_ACTIONS if a not in SLOT_ACTIONS]),
            ("武器快捷槽", list(SLOT_ACTIONS)),
        ]
        for ci, (title_g, actions) in enumerate(groups):
            cx = list_r.x + ci * (col_w + T.m(12))
            y = list_r.y
            T.draw_text(s, title_g, (cx, y), 11, T.CYAN, bold=True)
            y += T.m(18)
            for action in actions:
                row = pygame.Rect(cx, y, col_w, rh - T.m(4))
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
                T.draw_text(s, label, (row.x + T.m(8), row.y + T.m(4)), 12,
                            T.TEXT if hovered else T.TEXT_DIM)
                T.draw_text(s, key, (row.right - T.m(8), row.y + T.m(4)), 11, kcol,
                            anchor="topright", mono=True)
                self.rows.append((pygame.Rect(row), action, key))
                y += rh

        # 底部按钮
        by = r.bottom - fh + T.m(8)
        bh = T.m(32)
        self._btn_reset = pygame.Rect(r.x + T.m(16), by, T.m(150), bh)
        self._btn_close = pygame.Rect(r.right - T.m(16) - T.m(150), by, T.m(150), bh)
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

        msg_x = self._btn_reset.right + T.m(12)
        avail = self._btn_close.left - msg_x - T.m(8)
        if conflicts:
            msg = f"注意：{len(conflicts)} 个按键被多个动作共用，已按列表优先顺序生效"
            T.draw_text(s, T.ellipsize(msg, 11, avail), (msg_x, by + T.m(9)), 11, T.AMBER)
        elif self.message:
            T.draw_text(s, T.ellipsize(self.message, 11, avail), (msg_x, by + T.m(9)), 11,
                        self.message_color)

        self._draw_focus(s)

    def _draw_focus(self, s: pygame.Surface) -> None:
        """给当前键盘焦点画一圈琥珀色框。

        用琥珀色而不是青色：青色已经表示「当前值」，焦点要与之区分，
        否则玩家分不清哪个是选中项、哪个是键盘停在哪。
        找不到矩形（首帧还没 draw）时静默跳过，不影响交互。
        """
        kind, arg = self._focus()
        rect = None
        if kind == "theme":
            for b, tid in self._theme_buttons:
                if tid == T.current_theme().id:
                    rect = b
                    break
        elif kind == "icons":
            cur = str(self.cfg.pref("icon_pack", "") or "") or "__none__"
            for b, pid in self._icon_buttons:
                if pid == cur:
                    rect = b
                    break
        elif kind == "sound":
            rect = self._sound_btn
        elif kind == "volume":
            rect = pygame.Rect(self._vol_left.left, self._vol_left.top,
                               self._vol_right.right - self._vol_left.left,
                               self._vol_left.height)
        elif kind == "language":
            for b, code in self._lang_buttons:
                if code == self.cfg.language:
                    rect = b
                    break
        elif kind == "key":
            for row, action, _ in self.rows:
                if action == arg:
                    rect = row
                    break
        elif kind == "reset":
            rect = self._btn_reset
        elif kind == "done":
            rect = self._btn_close
        if rect is None or rect.width <= 0:
            return
        pygame.draw.rect(s, T.AMBER, rect.inflate(4, 4), 2, border_radius=4)


__all__ = ["SettingsPanel"]
