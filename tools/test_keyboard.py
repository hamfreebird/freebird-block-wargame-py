"""v3.0 专项自检：纯键盘界面导航（8.3 无障碍）。

覆盖三条此前只能靠鼠标的链路：

* **设置面板**——↑↓ 移动焦点、←→ 调整值、Enter 触发、Esc 关闭、
  捕获态重绑定；
* **主菜单底部入口**——E / C / O 打开编辑器 / 战役 / 设置；
* **系统菜单**（F10）——↑↓ 移动焦点、Enter 触发当前项、Esc 关闭。

**为什么单独一个脚本**：``smoke_test.py`` 主要验鼠标点击路径，
键盘路径需要按序发一串 KEYDOWN 并逐步断言焦点状态，混在一起
既难定位失败，也无法在 CI 里单独重跑。

用法：python tools/test_keyboard.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

# Windows CI 的 stdout 默认 cp1252，中文断言信息会直接把脚本打崩
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("BLOCKWARGAME_NO_AUDIO", "1")

# 测试会开关音效、切换主题与语言并保存配置——不隔离就会污染玩家真实设置
_TEST_USER_DIR = tempfile.mkdtemp(prefix="bw_kbd_")
os.environ["BLOCKWARGAME_USER_DIR"] = _TEST_USER_DIR

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame  # noqa: E402

from core import rules  # noqa: E402
from core.enums import Side, Terrain  # noqa: E402
from ui.app import App  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILS.append(msg)


def key(k: int) -> pygame.event.Event:
    return pygame.event.Event(pygame.KEYDOWN,
                              {"key": k, "mod": 0, "unicode": ""})


def main() -> int:
    pygame.init()
    app = App()
    screen = app.screen

    try:
        # ---- 1. 设置面板 --------------------------------------------------
        app.open_settings()
        p = app.settings_panel
        app.draw()                       # 生成控件矩形，同时验证焦点框可绘制
        items = p._focus_items()
        n = len(items)
        check(n > 10, f"设置面板：焦点项已生成（{n} 项）")
        check(p.focus_index == 0, "设置面板：初始焦点在首项")

        app._on_key(key(pygame.K_UP))
        check(p.focus_index == n - 1, "设置面板：↑ 从首项回绕到末项")
        app._on_key(key(pygame.K_DOWN))
        check(p.focus_index == 0, "设置面板：↓ 回到首项")

        t0 = app.cfg.theme
        app._on_key(key(pygame.K_RIGHT))
        check(app.cfg.theme != t0, "设置面板：→ 切换主题")
        t0 = app.cfg.theme
        app._on_key(key(pygame.K_LEFT))
        check(app.cfg.theme != t0, "设置面板：← 反向切换主题")

        p.focus_index = 4                # language
        l0 = app.cfg.language
        app._on_key(key(pygame.K_RIGHT))
        check(app.cfg.language != l0, "设置面板：←→ 可切换界面语言")

        p.focus_index = 3                # volume
        v0 = app.cfg.volume
        app._on_key(key(pygame.K_LEFT))
        check(app.cfg.volume < v0, "设置面板：←→ 可调整音量")

        p.focus_index = 2                # sound
        s0 = app.cfg.sound_enabled
        app._on_key(key(pygame.K_RETURN))
        check(app.cfg.sound_enabled != s0, "设置面板：Enter 可开关音效")

        kidx = next(i for i, (kind, _) in enumerate(items) if kind == "key")
        p.focus_index = kidx
        app._on_key(key(pygame.K_RETURN))
        check(bool(p.capturing), "设置面板：Enter 进入键位重绑定")
        app._on_key(key(pygame.K_ESCAPE))
        check(not p.capturing, "设置面板：捕获态按 Esc 可取消")

        p.focus_index = n - 1            # done
        app._on_key(key(pygame.K_RETURN))
        check(not app.show_settings, "设置面板：Enter 在「完成」上关闭面板")

        # 恢复默认，避免影响下面基于配置的断言
        app._apply_language("zh")
        app._apply_sound(True)
        app._apply_theme("tactical")

        # ---- 2. 主菜单底部入口 --------------------------------------------
        app.mode = "menu"
        app.show_settings = False
        app.show_campaign_menu = False
        app._on_key(key(pygame.K_o))
        check(app.show_settings, "主菜单：O 打开设置面板")
        app.close_settings()

        app._on_key(key(pygame.K_c))
        check(app.show_campaign_menu, "主菜单：C 打开战役菜单")
        app.show_campaign_menu = False

        app._on_key(key(pygame.K_e))
        check(app.mode == "editor", "主菜单：E 打开编辑器")
        app.mode = "menu"

        # ---- 3. 系统菜单（F10） -------------------------------------------
        app.mode = "game"
        app.show_menu = True
        app.sys_menu_index = 0
        app._refresh_slots()
        app._draw_system_menu(screen)     # 只画菜单，不依赖对局 state
        check(bool(app.menu_rects), "系统菜单：菜单项已生成")
        m = len(app.menu_rects)
        app._on_key(key(pygame.K_DOWN))
        check(app.sys_menu_index == 1 % m, "系统菜单：↓ 移动焦点")
        app._on_key(key(pygame.K_UP))
        check(app.sys_menu_index == 0, "系统菜单：↑ 移动焦点")

        ridx = next(i for i, (_r, a, _s) in enumerate(app.menu_rects)
                    if a == "resume")
        app.sys_menu_index = ridx
        app._on_key(key(pygame.K_RETURN))
        check(not app.show_menu, "系统菜单：Enter 触发当前项")

        app.show_menu = True
        app._on_key(key(pygame.K_ESCAPE))
        check(not app.show_menu, "系统菜单：Esc 关闭")

        # ---- 4. 实战键盘地图光标 ------------------------------------------
        # 起一局真实对局，验证方向键移动光标 + 回车在光标处执行左键等价动作，
        # 这是「纯键盘可完成整局」的核心缺口（此前只能用鼠标选/动/瞄）。
        app.mode = "menu"
        app.show_menu = False
        app.menu_side = Side.BLUE
        if app.scenarios:
            app.menu_index = 0
            app.start_game()
            app.show_briefing = False
            app.tutorial_visible = False
            st = app.state
            mv = app.mapview
            check(st is not None and mv is not None, "实战：已起局且地图视图存在")

            blue = st.units_of(Side.BLUE)
            if st is not None and blue:
                u0 = blue[0]
                app.select_unit(u0)
                # 方向键：首次按下前光标不显示；按下后落到选中单位并变为可见
                app.cursor_visible = False
                app._on_key(key(pygame.K_RIGHT))
                check(app.cursor_visible, "实战：方向键首次按下即显示键盘光标")
                at_edge = u0.x >= st.map.width - 1 or st.map.width <= 1
                moved = app.cursor != (u0.x, u0.y) or at_edge
                check(moved, "实战：方向键可移动光标（或已贴右边缘被钳制）")

                # 回车在自己单位格 → 选中该单位（与左键等价）
                app.cursor = (u0.x, u0.y)
                app.cursor_visible = True
                app._on_key(key(pygame.K_RETURN))
                check(st.selected_unit() is not None, "实战：回车在光标处可选中己方单位")

                # 回车在空地 → 机动（用 flood_reachable 找真正可达的格子，
                # 避免拿到舰/机等无法上陆或机动点为 0 的单位）
                moved_any = False
                for u in blue:
                    if u.mp <= 0.5:
                        continue
                    reach = rules.flood_reachable(st, u)
                    dest = next(((x, y) for (x, y) in reach
                                 if (x, y) != (u.x, u.y)), None)
                    if dest is None:
                        continue
                    app.select_unit(u)
                    app.cursor = dest
                    app.cursor_visible = True
                    x0, y0 = u.x, u.y
                    app._on_key(key(pygame.K_RETURN))
                    if (u.x, u.y) != (x0, y0):
                        moved_any = True
                        break
                check(moved_any, "实战：回车在光标可达空地处可下达机动指令")

                # 回车在敌方单位格 → 锁定或报告未识别（与左键等价，不崩溃）
                red = st.units_of(Side.RED)
                if red:
                    r0 = red[0]
                    app.select_unit(u0)
                    app.cursor = (r0.x, r0.y)
                    app.cursor_visible = True
                    app._on_key(key(pygame.K_RETURN))
                    locked = app.pending_target is r0
                    recognized = "识别" in app.status
                    check(locked or recognized,
                          "实战：回车在敌方格可锁定目标或报告未识别")

                # 绘制时键盘光标方框可正常绘制（不抛异常）
                try:
                    app.draw()
                    check(True, "实战：含键盘光标的帧可绘制")
                except Exception as exc:  # noqa: BLE001
                    check(False, f"实战：绘制抛异常 {exc!r}")
            else:
                check(False, "实战：未找到己方单位用于测试")
        else:
            check(False, "实战：无可加载的想定")

        # ---- 5. 键盘光标自动滚屏方向 --------------------------------------
        # 回归：v3.0 曾把 pan 符号写反——光标越界后画面朝同方向推，
        # 光标从视野里越跑越远。这里用假地图视图直接验四个方向。
        class _FakeMV:
            def __init__(self):
                self.rect = pygame.Rect(0, 0, 400, 300)
                self.tile = 20
                self.ox = 0.0
                self.oy = 0.0

            def grid_to_rect(self, x, y):
                return pygame.Rect(int(self.rect.x + self.ox + x * self.tile),
                                   int(self.rect.y + self.oy + y * self.tile),
                                   self.tile, self.tile)

            def pan(self, dx, dy):
                self.ox += dx
                self.oy += dy

        class _Dummy:
            pass

        scrolled_ok = True
        for label, cur in (("右", (25, 2)), ("左", (-3, 2)),
                           ("下", (5, 20)), ("上", (5, -2))):
            d = _Dummy()
            d.mapview = _FakeMV()
            d.cursor = cur
            App._ensure_cursor_visible(d)
            r = d.mapview.grid_to_rect(*cur)
            m = d.mapview.tile
            inside = (r.left >= d.mapview.rect.left + m
                      and r.right <= d.mapview.rect.right - m
                      and r.top >= d.mapview.rect.top + m
                      and r.bottom <= d.mapview.rect.bottom - m)
            if not inside:
                scrolled_ok = False
                print(f"        {label}越界未回到视野：{r}")
        check(scrolled_ok, "实战：光标越界自动滚屏方向正确（上下左右）")
    finally:
        shutil.rmtree(_TEST_USER_DIR, ignore_errors=True)

    print()
    if FAILS:
        print(f"✗ 键盘导航自检失败 {len(FAILS)} 项：")
        for msg in FAILS:
            print(f"    - {msg}")
        return 1
    print("✓ 键盘导航自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
