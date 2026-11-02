"""UI 布局自检：检测可点击元素之间的重叠与越界。

背景
----
按钮重叠是最容易被忽略的一类缺陷——视觉上看着正常，但后绘制的按钮
会拦截点击，导致「某个按钮点不了」。本脚本遍历界面上**所有**可点击
元素集合，报告两两重叠与越界情况。

用法::

    python tools/check_layout.py

退出码非 0 表示发现布局问题（可直接接入 CI）。
"""
from __future__ import annotations

import os
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame  # noqa: E402

from core.enums import Side  # noqa: E402
from ui.app import App  # noqa: E402


def _overlaps(a: pygame.Rect, b: pygame.Rect) -> bool:
    """两个矩形是否有实际重叠区域（相切不算重叠）。"""
    return (a.left < b.right and b.left < a.right
            and a.top < b.bottom and b.top < a.bottom)


def _collect(app: App, include_overlay: bool = True) -> list[tuple[str, pygame.Rect]]:
    """收集界面上所有可点击（或需要独占点击的）元素。

    `include_overlay=False` 时只收集底层界面元素——用于把浮层
    （系统菜单、简报、回顾）与底层的重叠排除掉：浮层本来就覆盖
    在界面之上，与底层重叠是设计使然，不是缺陷。
    """
    out: list[tuple[str, pygame.Rect]] = []

    # -- 顶栏按钮 --
    for b in app.buttons:
        out.append((f"顶栏/{b.label}", b.rect))

    # -- 侧栏页签 --
    for rect, key in app._tab_rects:
        out.append((f"页签/{key}", rect))

    # -- 侧栏行动按钮 --
    for b in app.action_buttons:
        out.append((f"行动/{b.label}", b.rect))

    # -- 武器行 --
    for rect, wid in app.weapon_rows:
        out.append((f"武器/{wid}", rect))

    # -- 齐射选项 --
    for rect, n in app.salvo_rects:
        out.append((f"齐射/{n}", rect))

    # -- 开火按钮 --
    if app._fire_btn.width > 0:
        out.append(("交战/确认开火", app._fire_btn))

    # -- 系统菜单（浮层）--
    if include_overlay:
        for rect, action, _ in app.menu_rects:
            out.append((f"系统菜单/{action}", rect))

    # -- 设置面板 --
    p = app.settings_panel
    if p is not None and include_overlay:
        for rect, tid in p._theme_buttons:
            out.append((f"设置/主题/{tid}", rect))
        for rect, code in p._lang_buttons:
            out.append((f"设置/语言/{code}", rect))
        if p._sound_btn.width > 0:
            out.append(("设置/音效开关", p._sound_btn))
        if p._vol_bar.width > 0:
            out.append(("设置/音量条", p._vol_bar))
        for rect, action, _ in p.rows:
            out.append((f"设置/键位/{action}", rect))
        if p._btn_reset.width > 0:
            out.append(("设置/恢复默认", p._btn_reset))
        if p._btn_close.width > 0:
            out.append(("设置/完成", p._btn_close))

    return out


def _check_bounds(app: App, elements: list[tuple[str, pygame.Rect]]) -> list[str]:
    """检查元素是否超出窗口边界。"""
    problems: list[str] = []
    w, h = app.w, app.h
    for name, r in elements:
        if r.width <= 0 or r.height <= 0:
            continue
        if r.left < 0 or r.top < 0 or r.right > w or r.bottom > h:
            problems.append(f"{name} 越界：{r}（窗口 {w}×{h}）")
    return problems


def _check_all_screens(app: App) -> list[str]:
    """遍历所有界面状态，逐个检查布局。

    侧栏三个页签、系统菜单、简报、回顾、编辑器、主菜单——每一个
    状态下元素集合都不同，只测一个状态会漏掉问题。
    """
    problems: list[str] = []

    def _scan(tag: str) -> None:
        app.draw()
        elements = _collect(app, include_overlay=False)
        seen: set[tuple[str, str]] = set()
        for i in range(len(elements)):
            for j in range(i + 1, len(elements)):
                n1, r1 = elements[i]
                n2, r2 = elements[j]
                if _overlaps(r1, r2):
                    key = tuple(sorted((n1, n2)))
                    if key in seen:
                        continue
                    seen.add(key)
                    problems.append(f"[{tag}] 元素重叠：{n1} {r1} ↔ {n2} {r2}")
        problems.extend(f"[{tag}] {p}" for p in _check_bounds(app, elements))

    def _scan_overlay(tag: str) -> None:
        """检查浮层内部元素之间的重叠（浮层与底层的重叠是设计使然）。"""
        elements = [(n, r) for n, r in _collect(app)
                    if n.startswith("系统菜单/") or n.startswith("设置/")]
        for i in range(len(elements)):
            for j in range(i + 1, len(elements)):
                n1, r1 = elements[i]
                n2, r2 = elements[j]
                if _overlaps(r1, r2):
                    problems.append(f"[{tag}] 浮层内重叠：{n1} {r1} ↔ {n2} {r2}")

    # -- 对局界面：三个侧栏页签 --
    app.start_tutorial()
    app._close_briefing()
    for tab in ("unit", "ops", "log"):
        app.tab = tab
        _scan(f"对局/{tab}")

    # -- 交战面板（打开交战视图）--
    app.tab = "unit"
    sel = next((u for u in app.state.units_of(Side.BLUE)
                if u.template.weapons and u.has_ammo()), None)
    tgt = next((e for e in app.state.enemies_of(Side.BLUE)
                if e.alive and app.state.known_level(Side.BLUE, e).level >= 2), None)
    if sel is not None and tgt is not None:
        app.select_unit(sel)
        app.pending_target = tgt
        app.draw()
        elements = _collect(app)
        for i in range(len(elements)):
            for j in range(i + 1, len(elements)):
                n1, r1 = elements[i]
                n2, r2 = elements[j]
                if _overlaps(r1, r2):
                    problems.append(f"[交战面板] 元素重叠：{n1} {r1} ↔ {n2} {r2}")

    # -- 系统菜单（浮层：只查浮层内部元素之间的重叠）--
    app.pending_target = None
    app.show_menu = True
    app.draw()
    _scan_overlay("系统菜单")
    app.show_menu = False

    # -- 简报 --
    app.mode = "menu"
    app.menu_index = 0
    app.start_game()
    _scan("简报")
    app._close_briefing()

    # -- 回顾 --
    app.state.over = True
    app.state.winner = Side.BLUE
    app._game_over()
    _scan("回顾")

    # -- 主菜单 --
    app.mode = "menu"
    app.debrief = None
    _scan("主菜单")
    # 主菜单的按钮是独立的一套矩形（_menu_*_rect），也要查
    menu_rects = [
        ("菜单/开始作战", app._menu_start_rect()),
        ("菜单/编辑器", app._menu_edit_rect()),
        ("菜单/快速上手", app._menu_tutorial_rect()),
        ("菜单/战役", app._menu_campaign_rect()),
        ("菜单/设置", app._menu_settings_rect()),
    ]
    for i in range(len(menu_rects)):
        for j in range(i + 1, len(menu_rects)):
            n1, r1 = menu_rects[i]
            n2, r2 = menu_rects[j]
            if _overlaps(r1, r2):
                problems.append(f"[主菜单] 按钮重叠：{n1} {r1} ↔ {n2} {r2}")
    problems.extend(f"[主菜单] {p}" for p in _check_bounds(app, menu_rects))

    # -- 编辑器 --
    app.open_editor()
    app.editor.draw(app.screen)
    ed = app.editor
    ed_buttons = [(f"编辑器/{lbl}", r) for r, lbl, _ in getattr(ed, "buttons", [])]
    for i in range(len(ed_buttons)):
        for j in range(i + 1, len(ed_buttons)):
            n1, r1 = ed_buttons[i]
            n2, r2 = ed_buttons[j]
            if _overlaps(r1, r2):
                problems.append(f"[编辑器] 按钮重叠：{n1} {r1} ↔ {n2} {r2}")

    return problems


def _check_min_size(app: App) -> list[str]:
    """在最小窗口尺寸下检查布局。

    按钮按固定像素宽度从右向左排布，窗口变窄时最先出问题——
    顶栏按钮会挤成一团甚至越界。这是布局缺陷的高发区。
    """
    problems: list[str] = []
    from ui.app import MIN_H, MIN_W

    # 回到对局界面——否则 draw() 走主菜单分支，侧栏元素是上一帧的残留
    app.mode = "game"
    app.screen = pygame.display.set_mode((MIN_W, MIN_H), pygame.RESIZABLE)
    app.w, app.h = app.screen.get_size()
    app._layout_game()
    app.draw()

    elements = _collect(app, include_overlay=False)
    for i in range(len(elements)):
        for j in range(i + 1, len(elements)):
            n1, r1 = elements[i]
            n2, r2 = elements[j]
            if _overlaps(r1, r2):
                problems.append(f"[最小窗口] 元素重叠：{n1} {r1} ↔ {n2} {r2}")
    problems.extend(f"[最小窗口] {p}" for p in _check_bounds(app, elements))

    # 顶栏关键按钮仍必须可点
    def _covered(target: pygame.Rect, others) -> str | None:
        for name, r in others:
            if r is target:
                continue
            if _overlaps(target, r):
                return name
        return None

    for label, btn in (("设置", app.settings_btn),
                       ("覆盖层", app.overlay_btn),
                       ("菜单", app.menu_btn)):
        others = [(n, r) for n, r in elements if r is not btn.rect]
        blocker = _covered(btn.rect, others)
        if blocker:
            problems.append(f"[最小窗口] 顶栏按钮「{label}」被「{blocker}」遮挡")
    return problems


def main() -> int:
    app = App()

    problems: list[str] = []
    problems.extend(_check_all_screens(app))
    problems.extend(_check_min_size(app))

    # -- 设置面板（在多个界面下都要能用）--
    app.mode = "game"
    app.open_settings()
    app.draw()                      # 面板的控件矩形是在 draw 里计算的
    p = app.settings_panel
    panel_elements: list[tuple[str, pygame.Rect]] = []
    for rect, tid in p._theme_buttons:
        panel_elements.append((f"主题/{tid}", rect))
    for rect, code in p._lang_buttons:
        panel_elements.append((f"语言/{code}", rect))
    if p._sound_btn.width > 0:
        panel_elements.append(("音效开关", p._sound_btn))
    if p._vol_left.width > 0:
        panel_elements.append(("音量减", p._vol_left))
    if p._vol_right.width > 0:
        panel_elements.append(("音量加", p._vol_right))
    if p._vol_bar.width > 0:
        panel_elements.append(("音量条", p._vol_bar))
    if p._btn_reset.width > 0:
        panel_elements.append(("恢复默认", p._btn_reset))
    if p._btn_close.width > 0:
        panel_elements.append(("完成", p._btn_close))
    for i in range(len(panel_elements)):
        for j in range(i + 1, len(panel_elements)):
            n1, r1 = panel_elements[i]
            n2, r2 = panel_elements[j]
            if _overlaps(r1, r2):
                problems.append(f"设置面板重叠：{n1} {r1} ↔ {n2} {r2}")
    # 设置面板的键位行之间不应重叠
    for i in range(len(p.rows)):
        for j in range(i + 1, len(p.rows)):
            r1, r2 = p.rows[i][0], p.rows[j][0]
            if _overlaps(r1, r2):
                problems.append(f"设置面板键位行重叠：{p.rows[i][1]} ↔ {p.rows[j][1]}")

    def _covered(target: pygame.Rect, others: list[tuple[str, pygame.Rect]]) -> str | None:
        for name, r in others:
            if r is target:
                continue
            if _overlaps(target, r):
                return name
        return None

    # -- 关键按钮必须可点（未被任何其它元素覆盖）--
    #     注意：按钮标签会随状态变化（如「覆盖层」↔「无覆盖」），
    #     所以这里按**矩形对象身份**排除自身，不能靠标签匹配。
    app.mode = "game"
    app.state = None
    app.start_tutorial()
    app._close_briefing()
    elements = _collect(app, include_overlay=False)
    for label, btn in (("设置", app.settings_btn),
                       ("覆盖层", app.overlay_btn),
                       ("菜单", app.menu_btn)):
        others = [(n, r) for n, r in elements if r is not btn.rect]
        blocker = _covered(btn.rect, others)
        if blocker:
            problems.append(f"顶栏按钮「{label}」被「{blocker}」遮挡，点击无效")

    # -- 设置面板控件必须可点 --
    for name, rect in panel_elements:
        others = [(n, r) for n, r in panel_elements if r is not rect]
        blocker = _covered(rect, others)
        if blocker:
            problems.append(f"设置面板控件「{name}」被「{blocker}」遮挡，点击无效")

    print(f"检查元素 {len(elements)} 个（顶栏 / 侧栏 / 系统菜单）"
          f" + {len(panel_elements)} 个（设置面板）")
    if problems:
        print(f"\n发现 {len(problems)} 个布局问题：")
        for pr in problems:
            print("  [FAIL]", pr)
        return 1
    print("布局检查通过：无重叠、无越界、无遮挡")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
