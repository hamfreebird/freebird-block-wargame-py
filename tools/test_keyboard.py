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
