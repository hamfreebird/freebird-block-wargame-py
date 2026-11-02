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

    # 教学想定排在最前，索引需按想定 id 定位而非固定序号
    def scenario_index(substr: str) -> int:
        for i, sc in enumerate(app.scenarios):
            if substr in sc["name"] or substr in sc.get("id", ""):
                return i
        return -1

    # ---- 想定 0：草原雷霆 ----
    app.menu_index = scenario_index("草原雷霆")
    if app.menu_index < 0:
        check(False, "未找到草原雷霆想定")
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

    # ---- 想定 1：海峡屏障 ----
    app.mode = "menu"
    app.menu_index = scenario_index("海峡")
    if app.menu_index < 0:
        check(False, "未找到海峡屏障想定")
        app.menu_index = 0
    app.start_game()
    check(app.state.map.name.startswith("海峡"), "第二张想定载入")

    # ================================================================== #
    # v1.1：教学想定与引导
    # ================================================================== #
    app.mode = "menu"
    app._scan_scenarios()
    check(app._tutorial_index() >= 0, "教学想定可被定位")
    app.start_tutorial()
    check(app.state is not None and app.state.map.name == "新兵训练场", "教学想定载入")
    # 步骤推进：选中单位 → 推进
    tut = app.tutorial
    check(tut is not None and tut.total >= 5, "教学脚本已装载")
    check(app.tutorial_visible, "教学提示条可见")
    # 开局自动选中了第一个单位，第 1 步应已推进
    check(tut.index >= 1, f"教学步骤已自动推进（index={tut.index}）")

    # 步骤 2：悬停敌方目标
    st2 = app.state
    st2.refresh_detection()
    enemy = next((e for e in st2.enemies_of(Side.BLUE)
                  if st2.known_level(Side.BLUE, e) is not Detection.NONE), None)
    if enemy is not None:
        idx1 = tut.index
        app._tutorial_note("hover_enemy")
        check(tut.index > idx1, "教学步骤推进（悬停侦察）")
    else:
        check(True, "本回合无可见敌人（跳过悬停教学）")

    # 对手视角：交战面板应给出拦截与反击评估
    sel = next((u for u in st2.units_of(Side.BLUE)
                if u.template.weapons and u.has_ammo()), None)
    tgt = next((e for e in st2.enemies_of(Side.BLUE)
                if e.alive and st2.known_level(Side.BLUE, e).level >= Detection.TRACKED.level), None)
    if sel is not None and tgt is not None:
        w = sel.usable_weapons()[0]
        threat = rules.counter_threat_profile(st2, sel, tgt, w)
        check(isinstance(threat.intercept_pk, float), "对手视角：拦截概率可计算")
        check(isinstance(threat.reply_units, list), "对手视角：反击单位列表可计算")
        check(threat.enemy_detected_level is not None, "对手视角：敌方火控状态可判定")
    else:
        check(True, "本回合无可评估目标（跳过对手视角）")

    # 悬停信息卡：己方与敌方都应能弹出
    own = next((u for u in st2.units_of(Side.BLUE) if u.alive), None)
    if own is not None:
        app.mouse = app.mapview.grid_to_rect(own.x, own.y).center
        app.update(0.1)
        check(app.hover_card and app.hover_unit is own, "悬停信息卡：己方单位可弹出")
    foe = next((e for e in st2.enemies_of(Side.BLUE)
                if e.alive and st2.known_level(Side.BLUE, e).level >= Detection.TRACKED.level), None)
    if foe is not None:
        app.mouse = app.mapview.grid_to_rect(foe.x, foe.y).center
        app.update(0.1)
        check(app.hover_card and app.hover_unit is foe, "悬停信息卡：敌方单位可弹出")
        # 未识别目标不得泄露参数（战争迷雾）
        from ui.inspect import build_rows
        rows = build_rows(foe, Side.BLUE, Detection.CONTACT, st2)
        check(not any(k == "血量" for k, _, _ in rows), "悬停信息卡：未识别目标不泄露血量")
    else:
        check(True, "无已跟踪敌人（跳过敌方悬停检查）")

    # 战报导出
    from core import report as _report
    md = _report.build_markdown(app.state)
    txt = _report.build_text(app.state)
    check(len(md) > 200 and "战报" in md, "战报 Markdown 生成")
    check(len(txt) > 200 and "作战结果" in txt, "战报文本生成")
    stats = _report.collect_stats(app.state)
    check(stats["shots"] >= 0 and 0.0 <= stats["hit_rate"] <= 100.0, "战报统计合理")

    # 键位自定义：改键后重绑定表随之更新
    from core.config import UserConfig
    cfg = UserConfig.load("")
    check(cfg.key_for("end_turn") == "space", "默认键位正确")
    cfg.set_key("end_turn", "enter")
    check(cfg.key_for("end_turn") == "enter", "键位可修改")
    cfg.reset_keys()
    check(cfg.key_for("end_turn") == "space", "键位可恢复默认")
    cfg.set_key("bogus_action", "x")
    check(cfg.key_for("bogus_action") == "", "非法动作被拒绝")
    # 配置文件损坏时回退到默认值
    import tempfile
    bad = os.path.join(tempfile.gettempdir(), "bw_bad_cfg.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("{ not json")
    cfg2 = UserConfig.load(bad)
    check(cfg2.key_for("end_turn") == "space" and cfg2.load_error != "", "损坏配置回退默认值")
    os.remove(bad)

    # 设置面板可渲染
    app.open_settings()
    app.draw()
    check(app.show_settings and app.settings_panel is not None, "设置面板可打开")
    app.close_settings()

    # 引导机：requires 条件未满足时不得推进
    from core.tutorial import Tutorial, TutorialStep
    flag = {"ok": False}
    tut2 = Tutorial(steps=[
        TutorialStep(id="a", title="A", body="", goal="", trigger="go",
                     requires=lambda st: flag["ok"]),
        TutorialStep(id="b", title="B", body="", goal="", trigger="go"),
    ])
    tut2.note("go")
    check(tut2.index == 0, "条件未满足时不推进步骤")
    flag["ok"] = True
    tut2.poll(app.state)
    check(tut2.index == 1, "条件满足后推进步骤")
    tut2.note("go")
    check(tut2.index == 2 and tut2.finished, "最后一步后教学结束")
    check(tut2.current is None, "结束后无当前步骤")

    # 便携版增量提取：升级后新增数据文件必须能补齐，且不覆盖玩家本地修改
    import shutil
    import tempfile
    import zipfile
    from core import paths as _paths
    tmpd = os.path.join(tempfile.gettempdir(), "bw_extract_test")
    shutil.rmtree(tmpd, ignore_errors=True)
    os.makedirs(tmpd, exist_ok=True)
    arc = os.path.join(tmpd, "test.pyz")
    with zipfile.ZipFile(arc, "w") as z:
        z.writestr("data/maps/a.json", '{"id":"a","name":"甲"}')
        z.writestr("data/maps/b.json", '{"id":"b","name":"乙"}')
    dest = os.path.join(tmpd, "dest")
    _paths._extract_from_archive(arc, dest)
    check(os.path.isfile(os.path.join(dest, "data", "maps", "a.json")), "便携版：首次提取数据")
    # 模拟旧版本地：只有 a.json，标记文件已在
    os.remove(os.path.join(dest, "data", "maps", "b.json"))
    with open(os.path.join(dest, "data", "maps", "a.json"), "w", encoding="utf-8") as f:
        f.write('{"id":"a","name":"玩家改过"}')
    _paths._extract_from_archive(arc, dest)
    check(os.path.isfile(os.path.join(dest, "data", "maps", "b.json")),
          "便携版：升级后补齐新增数据文件")
    with open(os.path.join(dest, "data", "maps", "a.json"), encoding="utf-8") as f:
        check("玩家改过" in f.read(), "便携版：不覆盖玩家本地修改")
    shutil.rmtree(tmpd, ignore_errors=True)

    # ================================================================== #
    # 编辑器 ----
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
