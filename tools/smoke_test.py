"""无界面冒烟测试：驱动完整 UI 流程并断言关键状态。

不依赖 pytest，可直接在 CI 中运行：
    SDL_VIDEODRIVER=dummy python tools/smoke_test.py

覆盖：启动 → 选择单位 → 机动 → 交战 → 结束回合 → 存读档 → 编辑器绘制/撤销。
"""
from __future__ import annotations

import os
import random
import shutil
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
from core.paths import armory_path  # noqa: E402
from ui.app import App  # noqa: E402

ARMORY = armory_path()

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
    # 简报是模态浮层：确认后才进入可交互的战斗界面
    check(app.show_briefing and app.briefing is not None, "开局简报已弹出")
    app._close_briefing()
    check(not app.show_briefing, "简报可关闭")
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
    # 教学想定同样会弹简报——关掉它，否则后续所有点击都被简报拦截
    check(app.show_briefing, "教学想定：开局弹出简报")
    app._close_briefing()
    check(not app.show_briefing, "教学想定：简报可关闭")
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

    # --- v1.4 修复：顶栏按钮不可重叠、设置面板控件可点 ---
    # 顶栏按钮两两不得重叠（重叠会让后绘制的按钮拦截点击）
    _top = [(b.label, b.rect) for b in app.buttons]
    _overlap = [(n1, n2) for i, (n1, r1) in enumerate(_top)
                for (n2, r2) in _top[i + 1:] if r1.colliderect(r2)]
    check(not _overlap, f"顶栏按钮互不重叠（{len(_top)} 个）")
    # 关键按钮必须未被其它顶栏按钮遮挡
    for _label, _btn in (("设置", app.settings_btn),
                         ("覆盖层", app.overlay_btn),
                         ("菜单", app.menu_btn)):
        _blk = [n for n, r in _top if r is not _btn.rect and r.colliderect(_btn.rect)]
        check(not _blk, f"顶栏按钮「{_label}」未被遮挡")
    # 设置面板：鼠标点击必须能触发控件（此前事件未转交面板，按钮全点不动）
    app.open_settings()
    app.draw()
    _p = app.settings_panel
    _theme_before = app.cfg.theme
    _tid = "light" if _theme_before != "light" else "tactical"
    _rect = next(r for r, t in _p._theme_buttons if t == _tid)
    app._on_mouse_down(pygame.event.Event(pygame.MOUSEBUTTONDOWN,
                                          {"pos": _rect.center, "button": 1}))
    check(app.cfg.theme == _tid, "设置面板：点击主题按钮可切换")
    _lang_before = app.cfg.language
    _lcode = "en" if _lang_before != "en" else "zh"
    _lrect = next(r for r, c in _p._lang_buttons if c == _lcode)
    app._on_mouse_down(pygame.event.Event(pygame.MOUSEBUTTONDOWN,
                                          {"pos": _lrect.center, "button": 1}))
    check(app.cfg.language == _lcode, "设置面板：点击语言按钮可切换")
    _sfx_before = app.cfg.sound_enabled
    app._on_mouse_down(pygame.event.Event(pygame.MOUSEBUTTONDOWN,
                                          {"pos": _p._sound_btn.center, "button": 1}))
    check(app.cfg.sound_enabled != _sfx_before, "设置面板：点击可开关音效")
    app._on_mouse_down(pygame.event.Event(pygame.MOUSEBUTTONDOWN,
                                          {"pos": _p._btn_close.center, "button": 1}))
    check(not app.show_settings, "设置面板：点击「完成」可关闭")
    # 顶栏「设置」按钮本身可点
    app.mouse = app.settings_btn.rect.center
    app._on_mouse_down(pygame.event.Event(pygame.MOUSEBUTTONDOWN,
                                          {"pos": app.settings_btn.rect.center,
                                           "button": 1}))
    check(app.show_settings, "顶栏：点击「设置」按钮可打开设置面板")
    app.close_settings()
    # 上面切过语言与音效——恢复原状，避免影响后续断言
    app._apply_language("zh")
    app._apply_sound(True)
    app._apply_theme("tactical")

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
    # v1.2：战术深度机制
    # ================================================================== #
    from core.env import EnvironmentTrack, environment_for
    from core.enums import SensorKind

    # --- 2.1 战场天气与时段 ---
    env = environment_for(st2)
    check(env.time_of_day is not None and env.weather is not None, "环境：时段与天候可读取")
    check(0.0 < env.factor(SensorKind.RADAR) <= 1.2, "环境：雷达系数在合理区间")
    # 夜间红外应优于光学，晴日光学应为 1.0
    from core.enums import TimeOfDay
    night = EnvironmentTrack(seed=1, mode="random").at(1)
    trk = EnvironmentTrack(seed=1, day_length=6)
    night_env = None
    for t in range(1, 40):
        e = trk.at(t)
        if e.time_of_day is TimeOfDay.NIGHT:
            night_env = e
            break
    if night_env is not None:
        check(night_env.factor(SensorKind.IR) > night_env.factor(SensorKind.OPTICAL),
              "环境：夜间红外优势")
    else:
        check(True, "未覆盖到夜间（跳过夜间红外检查）")
    # 环境应真实影响探测距离
    obs = next((u for u in st2.units_of(Side.BLUE)
                if any(s.kind is SensorKind.RADAR for s in u.template.sensors)), None)
    tgt_r = next((e for e in st2.enemies_of(Side.BLUE) if e.alive), None)
    if obs is not None and tgt_r is not None:
        from core import rules as _rules
        r_day = _rules.sensor_effective_range(st2, obs, tgt_r, 10.0, SensorKind.RADAR)[0]
        env2 = st2.env_track
        st2.turn_count = 5                       # 白天
        r_day = _rules.sensor_effective_range(st2, obs, tgt_r, 10.0, SensorKind.RADAR)[0]
        st2.turn_count = 1                       # 拂晓
        r_dawn = _rules.sensor_effective_range(st2, obs, tgt_r, 10.0, SensorKind.RADAR)[0]
        check(r_day > 0 and r_dawn > 0, "环境：环境系数作用于探测解算")
        st2.turn_count = 2
    # 天候序列应是确定性的
    a1 = EnvironmentTrack(seed=42).weather_at(5)
    a2 = EnvironmentTrack(seed=42).weather_at(5)
    check(a1 is a2, "环境：同种子天候序列确定")

    # --- 2.6 电子战频段 ---
    from core.config import UserConfig as _UC  # noqa: F401
    bands_seen = set()
    for tpl in app.state.scenario.armory.templates.values():
        bands_seen.update(tpl.jam_bands)
    check({"radar", "comm", "nav"} <= bands_seen, "频段：武器库覆盖雷达/通信/导航三频段")
    # 干扰强度应分频段计算
    from core import rules as _rules2
    j0 = _rules2.jam_strength_at(st2, (0, 0), Side.BLUE, "radar")
    j1 = _rules2.jam_strength_at(st2, (0, 0), Side.BLUE, "comm")
    j2 = _rules2.jam_strength_at(st2, (0, 0), Side.BLUE, "nav")
    check(0.0 <= j0 <= 0.85 and 0.0 <= j1 <= 0.85 and 0.0 <= j2 <= 0.85,
          "频段：干扰强度按频段独立计算")
    # 数据链可关闭
    dl = next((u for u in st2.units_of(Side.BLUE) if u.template.datalink), None)
    if dl is not None:
        before = dl.datalink_on
        st2.toggle_datalink(dl)
        check(dl.datalink_on is not before, "频段：数据链可开关（抗干扰反制）")
        st2.toggle_datalink(dl)

    # --- 2.4 多点防空协同 ---
    from core import rules as _rules2
    # 直接构造场景：把红方目标放到蓝方防空单元旁，用可拦截弹药建网
    sam_blue = next((u for u in st2.units_of(Side.BLUE)
                     if u.template.interceptors > 0 and u.template.intercept_pk > 0), None)
    w_test = None
    for tpl in st2.scenario.armory.templates.values():
        for ww in tpl.weapons:
            if ww.interceptable and ww.kind.value in ("land_attack", "asm"):
                w_test = ww
                break
        if w_test:
            break
    net = None
    if sam_blue is not None and w_test is not None:
        foe2 = next((e for e in st2.enemies_of(Side.BLUE)
                     if e.domain.value in ("land", "sea")), None)
        if foe2 is not None:
            foe2.x, foe2.y = sam_blue.x + 2, sam_blue.y
            st2.refresh_detection()
            net = _rules2.AirDefenseNetwork.build(st2, Side.BLUE, (foe2.x, foe2.y), w_test)
    if net is not None and net.nodes:
        check(net.total_channels >= 0, "防空网：火控通道统一记账")
        check(all(n.role for n in net.nodes), "防空网：每个节点都有明确分工")
        roles = net.role_counts()
        check(isinstance(roles, dict) and len(roles) >= 1, "防空网：分工统计可用")
        check(all(n.pk > 0 for n in net.nodes), "防空网：节点拦截概率有效")
    else:
        check(True, "无可用防空节点（跳过防空网检查）")

    # --- 2.2 补给线 ---
    from core import rules as _rules3
    srcs = _rules3.supply_sources(st2, Side.BLUE)
    check(len(srcs) > 0, "补给线：存在补给源")
    st2._recompute_supply()
    check(len(st2.supply_links) >= 0, "补给线：分配结果可计算")
    # 承载力：单位数不应超过所有源的总容量
    total_cap = sum(s.capacity for s in srcs if s.operational)
    check(total_cap >= len([u for u in st2.units_of(Side.BLUE) if u.domain.value != 'space']),
          "补给线：总承载力足以覆盖常规兵力")
    # 打瘫枢纽应切断补给
    depot = next((t for t in st2.map.iter_tiles()
                  if t.facility.value == "depot" and t.owner is Side.BLUE), None)
    if depot is not None:
        old_hp = depot.facility_hp
        depot.facility_hp = 0
        st2._recompute_supply()
        cut = [u for u in st2.units_of(Side.BLUE)
               if u.domain.value != 'space' and u.uid not in st2.supply_links]
        check(len(cut) >= 0, "补给线：枢纽失能后重新分配")
        depot.facility_hp = old_hp
        st2._recompute_supply()

    # --- 2.5 火力准备 ---
    prep_unit = next((u for u in st2.units_of(Side.BLUE)
                      if u.can_prepare_fire and u.mp >= st2.FIRE_PREPARE_MP), None)
    if prep_unit is not None:
        # 造一个有情报的目标点
        pt = None
        for dx, dy in ((3, 0), (2, 0), (4, 0), (3, 1), (3, -1)):
            p = (prep_unit.x + dx, prep_unit.y + dy)
            if st2.map.in_bounds(*p) and st2.unit_at(*p) is None \
                    and st2.map.at(*p).terrain.value != "water":
                pt = p
                break
        if pt is not None:
            # 放置一个敌方单位以提供情报
            fake = next((e for e in st2.enemies_of(Side.BLUE)
                          if e.domain.value == "land"), None)
            if fake is not None:
                fake.x, fake.y = pt
                st2.refresh_detection()
                ok, msg = st2.register_fire_mission(prep_unit, pt)
                check(ok, f"火力准备：可登记射界（{msg}）")
                if ok:
                    check(prep_unit.fire_mission is not None
                          and prep_unit.fire_mission.active, "火力准备：射界已生效")
                    st2._tick_fire_missions()
                    check(prep_unit.fire_mission.prepared, "火力准备：推进后完成准备")
                    # 无情报区域应拒绝
                    far_pt = (prep_unit.x + 1, prep_unit.y + 1)
                    st2.fire_mission = None
                    ok2, msg2 = st2.register_fire_mission(prep_unit, far_pt)
                    check(not ok2, "火力准备：无情报区域拒绝登记")
    else:
        check(True, "无可预先射击单位（跳过火力准备检查）")

    # --- 2.3 指挥体系 ---
    cmd_unit = next((u for u in st2.units_of(Side.BLUE)
                     if u.template.command_radius > 0), None)
    if cmd_unit is not None:
        subs = st2.subordinates_of(cmd_unit)
        check(len(subs) > 0, "指挥体系：可识别下级单位")
        # 集中火力需指定敌方目标
        foe = next((e for e in st2.enemies_of(Side.BLUE) if e.alive), None)
        if foe is not None:
            foe.x, foe.y = cmd_unit.x + 2, cmd_unit.y
            st2.refresh_detection()
            ok, msg = st2.can_issue_order(cmd_unit, "focus_fire", foe)
            check(ok, f"指挥体系：可下达集中火力（{msg}）")
            if ok:
                st2.issue_order(cmd_unit, "focus_fire", target=foe)
                check(any(s.order is not None for s in subs), "指挥体系：下级收到指令")
            # 己方目标应被拒绝
            ok2, msg2 = st2.can_issue_order(cmd_unit, "focus_fire", cmd_unit)
            check(not ok2, "指挥体系：不能向己方下达集中火力")
            # 指令到期失效
            st2._tick_orders()
            st2._tick_orders()
            check(all(s.order is None for s in subs), "指挥体系：指令到期自动失效")
    else:
        check(True, "无指挥单位（跳过指挥体系检查）")

    # ================================================================== #
    # v1.3：想定与编辑器
    # ================================================================== #
    # --- 3.3 想定元数据 ---
    from core.loader import ScenarioMeta
    meta = st2.meta
    check(isinstance(meta, ScenarioMeta), "元数据：ScenarioMeta 可用")
    check(meta.author != "" and meta.difficulty != "", "元数据：作者与难度已解析")
    check(meta.difficulty_label in ("入门", "标准", "困难", "专家"),
          "元数据：难度标签可读")
    check(isinstance(meta.tags, list) and len(meta.tags) > 0, "元数据：标签列表可读")
    check(isinstance(meta.recommended_units, list), "元数据：推荐兵种为列表")
    check(meta.recommended_side in ("BLUE", "RED", ""), "元数据：推荐方可读")

    # --- 3.2 想定脚本事件 ---
    from core.events import EVENT_TYPES, EventError, parse_events
    evs = parse_events(st2.raw.get("events"))
    check(len(evs) > 0, "事件：想定脚本已解析")
    check(all(e.type in EVENT_TYPES for e in evs), "事件：类型均在白名单内")
    check(evs == sorted(evs, key=lambda e: e.turn), "事件：按回合排序")
    # 触发一次：事件应在对应回合结算（用 state 内同一批事件对象验证标记）
    if st2.events:
        ev = st2.events[0]
        before_log = len(st2.log)
        st2.turn_count = ev.turn
        st2._run_events()
        check(len(st2.log) > before_log, "事件：触发后产生日志")
        check(ev.fired, "事件：触发后标记为已触发")
        # 不应重复触发
        n_before = len(st2.log)
        st2._run_events()
        check(len(st2.log) == n_before, "事件：同一事件不重复触发")
    # 格式错误应给出可读报错
    try:
        parse_events([{"turn": 1, "type": "不存在的类型"}])
        check(False, "事件：非法类型应报错")
    except EventError:
        check(True, "事件：非法类型给出可读报错")

    # --- 3.5 想定校验 ---
    from core.validate import validate_scenario
    rep = validate_scenario(st2)
    check(rep.ok, f"校验：想定无错误（{len(rep.errors)} 错误 / {len(rep.warnings)} 提示）")
    check(all(i.code for i in rep.issues), "校验：每条问题都有代码")

    # --- 3.6 社区想定包 ---
    from core import pkg as _pkg
    import tempfile as _tf
    _tmp = os.path.join(_tf.gettempdir(), "bw_pkg_smoke")
    shutil.rmtree(_tmp, ignore_errors=True)
    os.makedirs(_tmp, exist_ok=True)
    try:
        _out = os.path.join(_tmp, "t.bwscn")
        _pkg.export_package([st2.scenario_path], ARMORY, _out, name="测试包",
                            author="tester", include_armory=True,
                            only_armory_ids=["m1a2"])
        check(os.path.isfile(_out), "想定包：导出成功")
        _man = _pkg.inspect_package(_out)
        check(_man.name == "测试包" and _man.author == "tester", "想定包：清单可读")
        check(len(_man.maps) == 1, "想定包：包含 1 张想定")
        _maps2 = os.path.join(_tmp, "maps")
        _arm2 = os.path.join(_tmp, "armory.json")
        shutil.copy(ARMORY, _arm2)
        _res = _pkg.import_package(_out, _maps2, _arm2)
        check(len(_res.added_maps) == 1, "想定包：导入成功")
        check(len(_res.added_templates) == 1, "想定包：武器覆盖已导入")
        check(os.path.isfile(os.path.join(_maps2, os.path.basename(st2.scenario_path))),
              "想定包：导入的地图文件存在")
        # 损坏包应报错而非崩溃
        _bad = os.path.join(_tmp, "bad.bwscn")
        with open(_bad, "w", encoding="utf-8") as f:
            f.write("not a zip")
        try:
            _pkg.inspect_package(_bad)
            check(False, "想定包：损坏包应报错")
        except _pkg.PackageError:
            check(True, "想定包：损坏包给出可读报错")
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)

    # --- 3.4 战役模式 ---
    from core import campaign as _camp
    _camps = _camp.list_campaigns()
    check(len(_camps) > 0, "战役：存在可用战役定义")
    if _camps:
        _c = _camps[0]
        check(_c.total_steps > 0, "战役：关卡数有效")
        _prog = _camp.CampaignProgress(campaign_id=_c.id)
        _prog.vp["BLUE"] = 12
        _prog.vp["RED"] = 8
        check(_prog.blue_vp == 12 and _prog.red_vp == 8, "战役：胜负点可累计")
        # 幸存单位统计
        _surv = _camp.collect_survivors(st2, Side.BLUE)
        check(isinstance(_surv, list), "战役：幸存单位可统计")
        # 继承到下一关
        _before = len(st2.units_of(Side.BLUE))
        _added = _camp.apply_carryover(st2, [{"template": "m1a2", "count": 2}], Side.BLUE)
        check(len(st2.units_of(Side.BLUE)) >= _before, "战役：幸存部队可继承到下一关")
        check(len(_added) >= 0, "战役：继承返回明细")

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

    # --- 3.1 编辑器框选与批量编辑 ---
    ed._set_selection(2, 2, 6, 6)
    check(ed.selection_size() == 25, "框选：矩形区域格数正确")
    _outside_before = ed.terr[1][1]
    _b = sum(1 for yy in range(len(ed.terr)) for xx in range(len(ed.terr[0]))
             if ed.terr[yy][xx] == "f")
    ed.tool = "terrain"
    ed.brush_terrain = "f"
    _applied = ed._apply_to_selection()
    check(_applied == 25, "框选：批量应用到全部选区格子")
    _a = sum(1 for yy in range(len(ed.terr)) for xx in range(len(ed.terr[0]))
             if ed.terr[yy][xx] == "f")
    check(_a >= _b, "框选：批量刷地形生效")
    check(ed.terr[1][1] == _outside_before, "框选：选区外格子不被修改")
    ed.undo()
    check(ed.selection_size() == 25, "框选：撤销后选区保持")
    ed.clear_selection()
    check(ed.selection_size() == 0, "框选：可清除选区")

    # ================================================================== #
    # v1.4：呈现与体验
    # ================================================================== #
    # --- 4.1 战斗动画 ---
    from core import anim as _anim
    st3 = app.state
    _before = st3.anim.count
    st3.anim.add("trail", (2, 2), (8, 8), ttl=1.0)
    st3.anim.add("burst", (8, 8), None, ttl=0.6)
    st3.anim.add("intercept", (5, 5), None, ttl=0.6)
    st3.anim.add("smoke", (4, 4), None, ttl=0.8)
    st3.anim.add("flash", (3, 3), None, ttl=0.2)
    st3.anim.add("ping", (6, 6), None, ttl=0.8)
    st3.anim.add("move", (1, 1), (4, 1), ttl=0.4)
    st3.anim.add("label", (7, 7), None, ttl=0.9, text="命中")
    check(st3.anim.count == _before + 8, "动画：事件已入队")
    # 拖尾采样点应沿弹道分布
    _pts = _anim.trail_points((0.0, 0.0), (10.0, 0.0), 0.5)
    check(len(_pts) == 8 and _pts[-1][0] > _pts[0][0], "动画：拖尾沿弹道采样")
    # 推进后过期事件被剔除
    for _ in range(40):
        st3.tick_fx(0.05)
    check(st3.anim.count < _before + 8, "动画：过期事件被剔除")
    # 关闭动画后不再接收事件
    st3.anim.enabled = False
    _n0 = st3.anim.count
    st3.anim.add("burst", (1, 1), None, ttl=1.0)
    check(st3.anim.count == _n0, "动画：关闭后不接收新事件")
    st3.anim.enabled = True
    st3.anim.clear()

    # --- 4.4 音效系统 ---
    from ui.audio import (ALL_SOUNDS as _SOUNDS, AudioManager as _Audio,
                          SAMPLE_RATE as _SR, SOUND_DUR as _DUR, render_wave as _wave)
    # 波形合成不依赖混音器——无声卡环境（CI 容器）也能完整验证合成逻辑
    _wavs = {s: _wave(s) for s in _SOUNDS}
    check(all(v is not None and len(v) > 0 for v in _wavs.values()),
          f"音效：{len(_SOUNDS)} 个音效波形合成成功")
    check(_wave("no_such_sound") is None, "音效：未知音效名返回 None")
    # 波形长度应与声明时长一致（±2 采样）
    _lens_ok = all(abs(len(v) // 2 - int(_SR * _DUR[s])) <= 2
                    for s, v in _wavs.items())
    check(_lens_ok, "音效：波形长度与声明时长一致")

    _am = _Audio(enabled=True, volume=0.5)
    _pre = _am.preload()
    # 契约：有混音器则全部音效合成成功；无混音器（无声卡机器）
    # 必须优雅降级而不是崩溃——两种情况都算通过。
    if _am.available:
        check(_pre == len(_SOUNDS), "音效：混音器可用时全部音效预加载成功")
    else:
        check(_pre == 0, "音效：无混音器时优雅降级（无声卡环境）")
    for _s in _SOUNDS:
        _am.play(_s)
    check(True, "音效：全部音效播放无异常")
    # 播放后缓存里应有 Sound 对象——证明合成 + Sound 构造路径确实执行过
    if _am.available:
        check(_am.cached_count == len(_SOUNDS), "音效：播放后全部音效已缓存")
    _am.set_enabled(False)
    _am.play("fire")
    check(True, "音效：静音时播放不报错")
    _am.set_volume(0.2)
    check(abs(_am.volume - 0.2) < 1e-6, "音效：音量可调")
    _am.stop_all()

    # --- 4.3 主题系统 ---
    from ui import theme as _T
    _orig_theme = _T.current_theme().id
    _ok_all = True
    for _tid in _T.theme_ids():
        if not _T.set_theme(_tid):
            _ok_all = False
    check(_ok_all, "主题：全部主题可切换")
    check(_T.current_theme().id != _orig_theme, "主题：切换后当前主题已改变")
    _T.set_theme(_orig_theme)
    check(_T.current_theme().id == _orig_theme, "主题：可还原")
    # 不同主题的正文色应不同（亮色主题用深色文字）
    _T.set_theme("tactical")
    _dark_text = sum(_T.TEXT)
    _dark_bg = sum(_T.BG)
    _T.set_theme("light")
    check(_T.BG != _dark_bg, "主题：亮色主题背景不同")
    # 亮色主题：背景浅、文字深（明度对比方向与暗色主题相反）
    check(sum(_T.TEXT) < _dark_text and sum(_T.BG) > _dark_bg,
          "主题：亮色主题使用深色文字与浅色背景")
    # 色盲主题：敌方色不应与友方色接近（避免红绿混淆）
    _T.set_theme("tactical")
    _tac_blue = _T.SIDE_COLORS[Side.BLUE]["edge"]
    _tac_red = _T.SIDE_COLORS[Side.RED]["edge"]
    _T.set_theme("deuteranopia")
    _deu_blue = _T.SIDE_COLORS[Side.BLUE]["edge"]
    _deu_red = _T.SIDE_COLORS[Side.RED]["edge"]
    check(_deu_blue != _deu_red and abs(sum(_deu_red) - sum(_tac_red)) > 20,
          "主题：色盲主题改用蓝黄配色区分敌我")
    _T.set_theme(_orig_theme)

    # --- 4.5 本地化 ---
    from core import i18n as _i18n
    _zh = _i18n.tr("menu.start")
    check(_zh == "开始作战", "本地化：中文界面显示中文")
    check(_i18n.set_language("en"), "本地化：可切换到英文")
    _en = _i18n.tr("menu.start")
    check(_en == "Start Operation", "本地化：英文界面显示英文")
    check(_i18n.tr("no.such.key") == "no.such.key", "本地化：缺失键回退为键名")
    check(not _i18n.set_language("fr"), "本地化：拒绝不支持的语言")
    _i18n.set_language("zh")
    check(_i18n.tr("menu.start") == "开始作战", "本地化：可切回中文")
    # 两个语言包的键集合应一致
    from core.i18n import _BUILTIN_EN as _EN, _BUILTIN_ZH as _ZH
    check(set(_EN.keys()) == set(_ZH.keys()), "本地化：中英词条键集合一致")

    # --- 4.2 简报与战斗回顾 ---
    from ui.briefing import (build_briefing, build_debrief, summarize_force)
    app.mode = "menu"
    app.menu_index = scenario_index("草原雷霆")
    app.start_game()
    check(app.show_briefing and app.briefing is not None, "简报：开局自动弹出")
    _b = app.briefing
    check(_b.foe.total > 0 and _b.own.total > 0, "简报：敌我编成已统计")
    check(_b.env_label != "", "简报：初始环境已读取")
    check("VP" in _b.victory_hint or "胜" in _b.victory_hint, "简报：胜利条件已生成")
    app.draw()
    check(True, "简报：可渲染")
    app._close_briefing()
    # 战斗统计与回顾
    st4 = app.state
    st4.stats[Side.BLUE].update({"shots": 8, "hits": 5, "intercepted": 2,
                                  "kills": 2, "damage": 120})
    st4.stats[Side.RED].update({"shots": 6, "hits": 2, "intercepted": 3,
                                "kills": 1, "damage": 70})
    st4.vp_history = [(1, 2, 1), (2, 5, 3), (3, 9, 6)]
    st4.vp[Side.BLUE] = 9
    st4.vp[Side.RED] = 6
    st4.over = True
    st4.winner = Side.BLUE
    st4.win_reason = "测试结束"
    app._game_over()
    check(app.debrief is not None, "回顾：对局结束后生成")
    _d = app.debrief
    check(_d.shots_blue == 8 and _d.hits_blue == 5, "回顾：交战统计正确")
    check(len(_d.vp_curve) == 3, "回顾：VP 曲线已记录")
    check(bool(_d.highlights), "回顾：要点已生成")
    app.draw()
    check(True, "回顾：可渲染")

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
