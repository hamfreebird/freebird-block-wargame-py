"""v3.1.1 专项自检：字号缩放与色盲形状标识。

两条验收标准都是「看得见才算数」，所以本脚本测的是**可度量的性质**，
不是某张截图长什么样：

* 9.6 字号缩放——三档下 `px()` / `m()` / `lh()` 必须自洽：字号随档位单调
  增大，行高永远 ≥ 字号 + 2（否则行与行会压住），标准档必须与旧行为逐像素
  相同（否则默认玩家的界面会悄悄位移）。截断函数必须真的收在给定宽度内。
* 9.7 形状标识——开启后，敌方棋子右上角必须是**被切掉**的（alpha=0），
  我方必须完整；兵种角标必须真的落在棋子上。这样验收「灰度截图里仍能分辨
  敌我与兵种类别」时，不依赖任何人肉眼判断。

用法：python tools/test_a11y.py
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

import shutil  # noqa: E402
import tempfile  # noqa: E402

_USER_DIR = tempfile.mkdtemp(prefix="bw_a11y_")
os.environ["BLOCKWARGAME_USER_DIR"] = _USER_DIR

import pygame  # noqa: E402

from core import config as C  # noqa: E402
from core import i18n  # noqa: E402
from core.enums import UnitClass  # noqa: E402
from ui import theme as T  # noqa: E402
from ui import mapview as MV  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  [OK] {msg}")
    else:
        print(f"  [FAIL] {msg}")
        FAILS.append(msg)


def section(title: str) -> None:
    print(f"\n-- {title} " + "-" * max(0, 56 - len(title)))


# --------------------------------------------------------------------------- #
# 1. 配置层与主题层的档位常量必须一致
# --------------------------------------------------------------------------- #
def test_ids_in_sync() -> None:
    section("档位常量一致性")
    # core.config 不能 import ui（要在无 pygame 的环境里跑），
    # 所以两份常量必然重复——重复就得有测试盯着。
    check(tuple(T.font_scale_ids()) == tuple(C.FONT_SCALE_IDS),
          f"字号档位：theme={T.font_scale_ids()} == config={list(C.FONT_SCALE_IDS)}")
    check(tuple(k for k, _ in T.SHAPE_MODES) == tuple(C.SHAPE_MODE_IDS),
          f"形状档位：theme={[k for k, _ in T.SHAPE_MODES]} == config={list(C.SHAPE_MODE_IDS)}")
    check(C.DEFAULT_FONT_SCALE in C.FONT_SCALE_IDS, "默认字号档位在可选集合内")
    check(C.DEFAULT_SHAPE_MODE in C.SHAPE_MODE_IDS, "默认形状档位在可选集合内")


# --------------------------------------------------------------------------- #
# 2. 度量函数
# --------------------------------------------------------------------------- #
def test_metrics() -> None:
    section("度量函数")
    T.set_font_scale("standard")
    # 标准档必须与旧行为逐像素相同，否则默认玩家的界面会整体位移
    check(T.font_ratio() == 1.0, "标准档倍率为 1.0")
    same = all(T.px(s) == s for s in (10, 11, 12, 13, 14, 16, 18, 24))
    check(same, "标准档 px(n) 对所有基准字号恒等")
    check(T.m(148) == 148, "标准档 m(148) 恒等")
    check(T.lh(11) >= T.px(11) + 2, f"行高 ≥ 字号+2（lh(11)={T.lh(11)}）")

    prev_body = 0
    for scale_id in T.font_scale_ids():
        T.set_font_scale(scale_id)
        small, body = T.px(10), T.px(12)
        check(small >= 9, f"{scale_id}：10 号字不低于 9px（实际 {small}）")
        check(body >= prev_body, f"{scale_id}：12 号字不小于上一档（{body} ≥ {prev_body}）")
        check(T.lh(12) > T.px(12), f"{scale_id}：行高严格大于字号（{T.lh(12)} > {T.px(12)}）")
        check(T.m(148) >= 148, f"{scale_id}：m(148) ≥ 基准（{T.m(148)}）")
        prev_body = body

    # 放大后文字必须真的变宽，否则整条链路白做
    T.set_font_scale("standard")
    w_std = T.text_width("战场环境", 11)
    for scale_id in ("large", "xlarge"):
        T.set_font_scale(scale_id)
        w = T.text_width("战场环境", 11)
        check(w > w_std, f"{scale_id}：同一段文字比标准档宽（{w} > {w_std}）")
    T.set_font_scale("standard")


def test_ellipsize() -> None:
    section("像素级截断")
    long = "红旗-17A 近程防空连（第 3 营）"
    for scale_id in T.font_scale_ids():
        T.set_font_scale(scale_id)
        for size in (11, 13, 16):
            for limit in (40, 80, 160, 400):
                out = T.ellipsize(long, size, limit)
                w = T.text_width(out, size)
                ok = w <= limit or out == "…"
                check(ok, f"{scale_id}/{size}px 限 {limit}px：{out!r} 实测 {w}px")
                if limit < T.text_width(long, size) and len(out) < len(long):
                    check(out.endswith("…"), f"{scale_id}/{size}px 截断以省略号收尾")
        # 足够宽时必须原样返回，不能多此一举加省略号
        check(T.ellipsize(long, 11, 9999) == long, f"{scale_id}：宽度够时不截断")
    T.set_font_scale("standard")


def test_scale_switch() -> None:
    section("档位切换")
    check(T.set_font_scale("standard"), "切回标准档成功")
    check(not T.set_font_scale("no-such-scale"), "非法档位被拒绝")
    check(T.font_scale_id() == "standard", "非法档位不会改动当前档位")
    for scale_id in T.font_scale_ids():
        check(T.set_font_scale(scale_id) and T.font_scale_id() == scale_id,
              f"切换到 {scale_id} 生效")
    T.set_font_scale("standard")


# --------------------------------------------------------------------------- #
# 3. 形状标识
# --------------------------------------------------------------------------- #
def test_shape_mode() -> None:
    section("形状标识开关")
    check(not T.set_shape_mode("bogus"), "非法形状档位被拒绝")
    T.set_shape_mode("off")
    check(not T.shape_cues(), "关：不画形状标识")
    T.set_shape_mode("on")
    check(T.shape_cues(), "开：任何主题下都画")

    T.set_shape_mode("auto")
    for tid in T.theme_ids():
        T.set_theme(tid)
        want = tid in {"deuteranopia", "protanopia"}
        check(T.shape_cues() == want,
              f"自动：{tid} → {'画' if want else '不画'}（实际 {'画' if T.shape_cues() else '不画'}）")
    T.set_theme("tactical-night" if "tactical-night" in T.theme_ids() else T.theme_ids()[0])
    T.set_shape_mode("auto")


def test_badge_shapes() -> None:
    section("兵种角标映射")
    kinds = set(MV.class_badge_shape.values())
    check("square" in kinds and "dot" in kinds and "tri_up" in kinds
          and "tri_down" in kinds and "diamond" in kinds and "ring" in kinds
          and "cross" in kinds, f"七种角标形状齐全：{sorted(kinds)}")
    # 每个兵种都要有角标——漏了的会落到默认方块，等于两个兵种长得一样
    missing = [c for c in UnitClass if c not in MV.class_badge_shape]
    check(not missing, f"全部 {len(list(UnitClass))} 个兵种都有角标映射（缺 {missing}）")
    # 同一家族的形状必须一致，否则「家族」这个说法就不成立了
    check(MV.class_badge_shape[UnitClass.SAM] == MV.class_badge_shape[UnitClass.AAA],
          "防空两兵种同属一个家族")
    check(MV.class_badge_shape[UnitClass.MBT] == MV.class_badge_shape[UnitClass.IFV],
          "坦克与步战同属一个家族")
    check(MV.class_badge_shape[UnitClass.MBT] != MV.class_badge_shape[UnitClass.ARTY],
          "装甲与炮兵必须是不同家族")


def _body_sprite(friendly: bool, cls: UnitClass) -> pygame.Surface:
    """按真实尺寸画一颗棋子，用来检查切角与角标。"""
    surf = pygame.Surface((46, 46), pygame.SRCALPHA)
    body = pygame.Rect(1, 1, 44, 44)
    cols = {"fill": (40, 52, 92), "edge": (90, 130, 200), "text": (200, 215, 240)}
    pygame.draw.rect(surf, cols["fill"], body, border_radius=3)
    if not friendly:
        MV.draw_enemy_notch(surf, body, cols["edge"])
    else:
        pygame.draw.rect(surf, cols["edge"], body, width=1, border_radius=3)
    MV.draw_class_badge(surf, body, cls, (235, 240, 255), cols["fill"])
    return surf


def test_notch_and_badge() -> None:
    section("棋子形状标识（像素级）")
    pygame.display.set_mode((64, 64))
    for cls in (UnitClass.MBT, UnitClass.SAM, UnitClass.FIGHTER):
        mine = _body_sprite(True, cls)
        foe = _body_sprite(False, cls)
        # 右上角：我方实心，敌方被切掉（alpha = 0）
        p = (43, 3)
        check(mine.get_at(p)[3] > 0, f"{cls.value}：我方右上角完整")
        check(foe.get_at(p)[3] == 0, f"{cls.value}：敌方右上角切角（alpha=0）")
        # 切角不能让整个棋子塌掉：中心必须仍然实心
        check(foe.get_at((23, 23))[3] > 0, f"{cls.value}：敌方棋子主体仍在")
        # 左下角（角标位置）必须被画过——与纯底色不同
        base = _body_sprite(True, cls)
        px = (6, 44 - MV.badge_size(pygame.Rect(0, 0, 44, 44)) // 2 - 1)
        # 角标画在左下，取角标中心附近任一像素有高亮即可
        found = False
        s = MV.badge_size(pygame.Rect(0, 0, 44, 44))
        for dx in range(0, s):
            for dy in range(0, s):
                if base.get_at((3 + dx, 44 - 3 - s + dy))[:3] != (40, 52, 92):
                    found = True
                    break
        check(found, f"{cls.value}：左下角画出了兵种角标")


# --------------------------------------------------------------------------- #
# 4. 设置面板能真正改变全局状态
# --------------------------------------------------------------------------- #
def test_settings_roundtrip() -> None:
    section("设置面板 → 全局状态")
    from ui.app import App

    app = App()
    app.open_settings()
    p = app.settings_panel
    app.draw()
    check(len(p._scale_buttons) == len(C.FONT_SCALE_IDS), "设置面板有全部字号档位按钮")
    check(len(p._shape_buttons) == len(T.SHAPE_MODES), "设置面板有全部形状档位按钮")
    # 点一下「特大」，全局字号倍率必须跟着变
    rect, sid = p._scale_buttons[-1]
    p.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN,
                                      {"pos": rect.center, "button": 1}))
    check(T.font_scale_id() == sid, f"点击按钮后字号档位变为 {sid}")
    check(T.font_ratio() > 1.0, "点击后倍率真的放大了")
    # 面板控件必须都在面板内（放大档位下语言行最容易跑出去）
    app.draw()
    pr = p.rect
    out = [(n, r) for n, r in
           [(f"theme/{t}", r) for r, t in p._theme_buttons]
           + [(f"scale/{s}", r) for r, s in p._scale_buttons]
           + [(f"shape/{m}", r) for r, m in p._shape_buttons]
           + [(f"lang/{c}", r) for r, c in p._lang_buttons]
           if r.left < pr.left or r.right > pr.right or r.top < pr.top or r.bottom > pr.bottom]
    check(not out, f"设置控件全部在面板内（越界 {out}）")
    T.set_font_scale("standard")


# --------------------------------------------------------------------------- #
# 5. 词条
# --------------------------------------------------------------------------- #
def test_i18n() -> None:
    section("无障碍相关词条")
    keys = ["settings.font_scale", "settings.shape_cues"]
    ui_keys = [f"ui.scale.{s}" for s in T.font_scale_ids()] + \
              [f"ui.shape.{k}" for k, _ in T.SHAPE_MODES]
    langs = i18n.available_languages()
    missing: list[str] = []
    for code in langs:
        i18n.set_language(code)
        for k in keys + ui_keys:
            v = i18n.tr(k)
            if not v or v == k:
                missing.append(f"{code}:{k}")
    check(not missing, f"{len(langs)} 种语言都取得到无障碍词条（缺 {missing[:6]}）")
    i18n.set_language("zh")
    check(i18n.tr("ui.scale.xlarge") == "特大", "中文「特大」正确")
    check(i18n.tr("settings.shape_cues") == "形状标识", "中文「形状标识」正确")


def main() -> int:
    print("无障碍自检：字号缩放 + 色盲形状标识")
    test_ids_in_sync()
    test_metrics()
    test_ellipsize()
    test_scale_switch()
    test_shape_mode()
    test_badge_shapes()
    test_notch_and_badge()
    test_settings_roundtrip()
    test_i18n()

    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("  -", f)
        return 1
    print("无障碍自检全部通过")
    return 0


if __name__ == "__main__":
    try:
        _rc = main()
    finally:
        shutil.rmtree(_USER_DIR, ignore_errors=True)
    raise SystemExit(_rc)
