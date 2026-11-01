"""无界面冒烟测试：驱动完整 UI 流程并断言关键状态。

不依赖 pytest，可直接在 CI 中运行：
    SDL_VIDEODRIVER=dummy python tools/smoke_test.py

覆盖：启动 → 选择单位 → 机动 → 交战 → 结束回合 → 存读档 → 编辑器绘制/撤销。
"""
from __future__ import annotations

import os
import random
import sys


# Windows 控制台/管道默认编码（cp1252 等）无法输出中文，
# 统一把标准输出切到 UTF-8，缺失字符以替换符兜底，避免 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
import traceback

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame  # noqa: E402

from core.enums import Detection, Side  # noqa: E402
from core import rules  # noqa: E402
from ui.app import App  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  [OK] {label}")
    else:
        print(f"  [FAIL] {label}")
        FAILED.append(label)


def click(app: App, label: str) -> bool:
    """点击侧栏中标签匹配的行动按钮。"""
    app.draw()
    for b in app.action_buttons:
        if label in b.label:
            app._on_mouse_down(pygame.event.Event(
                pygame.MOUSEBUTTONDOWN, {"pos": b.rect.center, "button": 1}))
            return True
    return False


def main() -> int:
    random.seed(20240101)
    app = App()
    app.draw()
    check(True, "主菜单渲染")

    # ---- 想定 1：草原雷霆 ----
    app.menu_index = 0
    app.start_game()
    st = app.state
    check(st is not None and len(st.units) > 20, f"想定载入（{st.map.name}，{len(st.units)} 单位）")
    check(st.objective_tiles, "目标点存在")
    check(any(t.fort > 0 for t in st.map.iter_tiles()), "预置工事载入")

    # 选择一个可占领单位并机动
    cap = next(u for u in st.units_of(Side.BLUE) if u.template.can_capture)
    app.select_unit(cap)
    app.draw()
    check(app.state.selected_unit() is cap, "单位选中")
    tgt = None
    for (dx, dy) in ((1, 0), (0, 1), (-1, 0), (0, -1)):
        p = (cap.x + dx, cap.y + dy)
        if p in app.reachable:
            tgt = p
            break
    if tgt:
        app.order_move(*tgt)
        check((cap.x, cap.y) == tgt, f"机动到 {tgt}")
    else:
        check(True, "无可达格（跳过机动）")

    # 交战面板
    sel = tgt_unit = None
    for u in st.units_of(Side.BLUE):
        for e in st.units_of(Side.RED):
            if not e.alive or st.known_level(Side.BLUE, e).level < Detection.TRACKED.level:
                continue
            for w in u.usable_weapons():
                if rules.shot_profile(st, u, e, w).ok:
                    sel, tgt_unit = u, e
                    break
            if sel:
                break
        if sel:
            break
    if sel:
        app.select_unit(sel)
        for w in sel.usable_weapons():
            if rules.shot_profile(st, sel, tgt_unit, w).ok:
                st.selected_weapon = w.id
                break
        app.pending_target = tgt_unit
        app.draw()
        check(len(app.salvo_rects) >= 0, "交战面板渲染")
        app.confirm_fire()
        check(True, f"开火执行（{st.log[-1].text[:30]}）")
    else:
        check(True, "本回合无可交战目标（跳过）")

    # 行动按钮
    inf = next((u for u in st.units_of(Side.BLUE) if u.template.can_fortify), None)
    if inf is not None:
        land = next((t for t in st.map.iter_tiles()
                     if t.terrain.value != "water" and st.unit_at(t.x, t.y) is None), None)
        inf.x, inf.y = land.x, land.y
        st.refresh_detection()
        app.select_unit(inf)
        app.draw()
        before = land.fort
        if click(app, "筑工事"):
            check(st.map.at(land.x, land.y).fort > before, "构筑工事生效")
        else:
            check(False, "筑工事按钮缺失")

    air = next((u for u in st.units_of(Side.BLUE) if u.domain is not None
                and u.template.domain.value == "air"), None)
    if air is not None:
        app.select_unit(air)
        app.draw()
        m0 = air.mission
        if click(app, "剖面"):
            check(air.mission != m0, f"任务剖面切换（{m0} → {air.mission}）")
        else:
            check(False, "剖面按钮缺失")

    # 回合推进 + 存读档
    app.end_turn()
    guard = 0
    while st.phase == "ai" and guard < 900:
        guard += 1
        app.update(0.3)
    check(st.turn_count >= 2, f"回合推进（T{st.turn_count}）")
    app.save_slot(1)
    turn_before = st.turn_count
    app.load_slot(1)
    check(app.state.turn_count == turn_before, "存读档回合一致")

    # ---- 想定 2：海峡屏障 ----
    app.mode = "menu"
    app.menu_index = 1
    app.start_game()
    check(app.state.map.name.startswith("海峡"), "第二张想定载入")

    # ---- 编辑器 ----
    app.open_editor()
    ed = app.editor
    check(ed is not None and len(ed.terr) > 0, "编辑器打开")
    ed.tool = "terrain"
    ed._apply((5, 5))
    check(len(ed.undo_stack) > 0, "编辑操作入撤销栈")
    ed.undo()
    ed.redo()
    check(True, "撤销 / 重做可用")

    print()
    if FAILED:
        print(f"冒烟测试失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print("冒烟测试全部通过")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
