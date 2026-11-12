"""v2.2.1 专项自检：教学触发边界与浅色主题适配。

覆盖两个玩家反馈的缺陷：

1. **正常游玩时出现教程**——`build_tutorial` 曾对所有想定回退到默认脚本，
   于是普通想定的作战日志里会冒出「教学：第 N 步 · …」；
2. **浅色模式下部分按钮仍显示为深色**——按钮底色被硬编码成深色常量，
   切到亮色主题后在浅色背景上变成突兀的黑块。

**主题适配的测法**：不比对像素，而是**比对按钮底色与背景的明暗关系**——
亮色主题下按钮必须与背景同侧（都浅），深色主题下必须都深。
只查「是不是某个具体颜色」太脆，主题一改就失效；
「明暗是否同侧」才是真正要保证的性质。

用法：python tools/test_theme.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402

from core.loader import load_scenario  # noqa: E402
from core.paths import data_root  # noqa: E402
from core.tutorial import TUTORIAL_SCENARIO_ID, build_tutorial  # noqa: E402
from ui import theme as T  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  [OK] {msg}")
    else:
        print(f"  [FAIL] {msg}")
        FAILS.append(msg)


# --------------------------------------------------------------------------- #
# 亮度工具
# --------------------------------------------------------------------------- #
def _lum(c) -> float:
    """感知亮度（Rec. 601 加权）。用于判断「亮」还是「暗」。"""
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def _fill_of(surface: pygame.Surface, rect: pygame.Rect) -> tuple[int, int, int] | None:
    """取矩形内**出现次数最多**的像素作为底色。

    为什么取众数而不是极值：按钮上有文字，极值（最亮/最暗）
    往往会命中文字笔画或边框，测出来的是文字色而不是底色。
    众数才是底色——它是矩形里占比最大的颜色。
    """
    if rect.width <= 0 or rect.height <= 0:
        return None
    sw, sh = surface.get_size()
    counts: dict[tuple[int, int, int], int] = {}
    for dy in (0.1, 0.25, 0.4, 0.55, 0.7, 0.85):
        for dx in (0.04, 0.2, 0.4, 0.6, 0.8, 0.96):
            x = int(rect.x + rect.width * dx)
            y = int(rect.y + rect.height * dy)
            if not (0 <= x < sw and 0 <= y < sh):
                continue
            c = tuple(surface.get_at((x, y))[:3])
            counts[c] = counts.get(c, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]



# --------------------------------------------------------------------------- #
# 1) 教学触发边界
# --------------------------------------------------------------------------- #
def test_tutorial_scope() -> None:
    print("--- 教学触发边界 ---")
    maps_dir = os.path.join(data_root(), "data", "maps")
    armory = os.path.join(data_root(), "data", "units", "armory.json")

    normal_maps = []
    for name in sorted(os.listdir(maps_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(maps_dir, name)
        try:
            sc = load_scenario(path, armory)
        except Exception:
            continue
        raw = sc.raw
        if raw.get("id") == TUTORIAL_SCENARIO_ID:
            continue
        # 声明了自定义教学脚本的想定也算「有意图的教学」
        if isinstance(raw.get("tutorial"), dict) and raw["tutorial"].get("steps"):
            continue
        normal_maps.append((name, raw))

    check(len(normal_maps) >= 2,
          f"找到 {len(normal_maps)} 个普通想定用于验证")

    for name, raw in normal_maps:
        t = build_tutorial(raw)
        check(t is None,
              f"普通想定「{name}」不构造教学引导机（build_tutorial 返回 None）")

    # 教学想定必须有脚本
    tut_path = os.path.join(maps_dir, "training_ground.json")
    if os.path.exists(tut_path):
        sc = load_scenario(tut_path, armory)
        t = build_tutorial(sc.raw)
        check(t is not None and len(t.steps) > 0,
              f"教学想定有教学脚本（{len(t.steps) if t else 0} 步）")


# --------------------------------------------------------------------------- #
# 2) 浅色主题下的按钮明暗
# --------------------------------------------------------------------------- #
def _menu_buttons(app) -> list[tuple[str, pygame.Rect]]:
    return [
        ("开始作战", app._menu_start_rect()),
        ("编辑想定", app._menu_edit_rect()),
        ("快速上手", app._menu_tutorial_rect()),
        ("战役模式", app._menu_campaign_rect()),
        ("设置", app._menu_settings_rect()),
        ("导入回放", app._menu_replay_btn),
        ("导入想定", app._menu_import_btn),
    ]


def test_theme_brightness() -> None:
    print("--- 主题明暗一致性 ---")
    from ui.app import App

    app = App()
    orig = T.current_theme().id

    for theme_id, want_light in (("light", True), ("tactical", False)):
        T.set_theme(theme_id)
        app.draw()
        bg = app.screen.get_at((5, 5))
        bg_lum = _lum(bg)
        print(f"  · {theme_id}：背景亮度 {bg_lum:.0f}")

        # 背景本身的明暗必须符合预期
        if want_light:
            check(bg_lum > 150, f"{theme_id} 主题背景是亮的")
        else:
            check(bg_lum < 80, f"{theme_id} 主题背景是暗的")

        for label, rect in _menu_buttons(app):
            fill = _fill_of(app.screen, rect)
            if fill is None:
                continue
            fl = _lum(fill)
            # 判据：按钮底色的**绝对亮度**必须落在主题对应的一侧。
            #
            # 不用「与背景的差值」——深色主题下按钮本来就比背景亮
            # 一截（要有边界），差值判据会把正常按钮也判成跑偏。
            # 直接用绝对亮度：浅色主题的按钮必须够亮（≥ 120），
            # 深色主题的按钮必须够暗（≤ 110）。硬编码成深色的
            # 旧代码（亮度 25）在浅色主题下必然低于阈值——正好能抓住。
            ok = fl >= 120 if want_light else fl <= 110
            check(ok, f"{theme_id}：主菜单「{label}」底色与背景同侧"
                      f"（{fill} 亮度 {fl:.0f} vs 背景 {bg_lum:.0f}）")

    T.set_theme(orig)


def test_theme_text_legible() -> None:
    """按钮文字与底色必须有足够对比度。"""
    print("--- 按钮文字对比度 ---")
    for theme_id in ("light", "tactical"):
        T.set_theme(theme_id)
        fill = T.button_fill(T.CYAN, False)
        fg = T.button_text(T.CYAN, False)
        lf, lg = _lum(fill), _lum(fg)
        # 亮度差要够大才看得清（差值 < 60 基本读不出来）
        check(abs(lf - lg) >= 60,
              f"{theme_id}：按钮文字与底色亮度差 {abs(lf - lg):.0f} ≥ 60")
    T.set_theme("tactical")


def test_button_fill_derives_from_theme() -> None:
    """button_fill 必须随主题变化——硬编码的常量做不到这点。"""
    print("--- 按钮底色随主题派生 ---")
    fills = {}
    for theme_id in ("light", "tactical"):
        T.set_theme(theme_id)
        fills[theme_id] = T.button_fill(T.CYAN, False)
    T.set_theme("tactical")
    check(fills["light"] != fills["tactical"],
          "浅色与深色主题下的按钮底色不同（说明由主题派生）")
    check(_lum(fills["light"]) > _lum(fills["tactical"]),
          "浅色主题的按钮底色明显更亮")
    check(_lum(fills["tactical"]) < 90,
          f"深色主题的按钮底色仍是暗色（{fills['tactical']}）")


def test_wrap_text_word_break() -> None:
    """换行策略：拉丁文按词断、中日文按字断。

    纯按字符切会把德语长词劈成两半（"Muniti" / "on"）——
    在多语言界面上看起来像排版事故，而中文没有空格可依。
    """
    print("文本换行：")
    width = 120
    latin = "Ein Ziel muss von Sensoren verfolgt werden"
    lines = T.wrap_text(latin, 11, width)
    check(all(T.text_width(ln, 11) <= width for ln in lines),
          "每行都不超过给定宽度")
    # 按词断行：任意两行拼回去必须能还原原文（空格不多不少）
    check(" ".join(lines) == latin, "按词断行，拼回原文一字不差")
    check(not any(ln.endswith(" ") for ln in lines), "行尾没有残留空格")

    cjk = "目标须被传感器跟踪至跟踪级才能装订诸元"
    clines = T.wrap_text(cjk, 11, 60)
    check(len(clines) > 1 and "".join(clines) == cjk, "无空格的中文按字断行且可拼回")

    long_word = "Donaudampfschifffahrtsgesellschaftskapitaen"
    wl = T.wrap_text(long_word, 11, 60)
    check(all(T.text_width(ln, 11) <= 60 for ln in wl) and "".join(wl) == long_word,
          "超长单词退回按字符硬拆")


def main() -> int:
    pygame.init()
    pygame.display.set_mode((1500, 920), pygame.RESIZABLE)
    print("=== 主题与教学自检 ===")
    test_tutorial_scope()
    test_wrap_text_word_break()
    test_button_fill_derives_from_theme()
    test_theme_text_legible()
    test_theme_brightness()
    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("  -", f)
        return 1
    print("主题与教学自检全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
