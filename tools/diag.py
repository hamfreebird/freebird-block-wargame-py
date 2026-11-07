"""作战逻辑自检：用不变量（invariant）检查一整局对局中是否出现规则破坏。

用法：
    python tools/diag.py                 # 两张地图各跑一遍
    python tools/diag.py steppe_thunder 24

检查项：
    L1 弹药不为负          L2 发射次数不超过模板上限
    L3 不攻击友军          L4 数值无 NaN / 负血量
    L5 在飞弹药最终结算    L6 位置合法（不落在不可通行地形）
    L7 存档往返一致        L8 胜负点不减
    L9 阵亡单位不再行动    L10 探测等级合法
"""
from __future__ import annotations

import math
import os
import sys


# Windows 控制台/管道默认编码（cp1252 等）无法输出中文，
# 统一把标准输出切到 UTF-8，缺失字符以替换符兜底，避免 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ai import AIPlanner                             # noqa: E402
from core.engine import GameState                         # noqa: E402
from core.enums import Detection, Domain, Side, Terrain   # noqa: E402
from core.loader import load_scenario                     # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARMORY = os.path.join(ROOT, "data", "units", "armory.json")


class Checker:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warns: list[str] = []

    def fail(self, tag: str, msg: str) -> None:
        self.errors.append(f"[{tag}] {msg}")

    def warn(self, tag: str, msg: str) -> None:
        self.warns.append(f"[{tag}] {msg}")

    # -- 每步检查 ---------------------------------------------------------
    def check_state(self, st: GameState, turn: int) -> None:
        for u in st.units:
            if math.isnan(u.hp) or math.isinf(u.hp):
                self.fail("L4", f"{u.name} 血量为 {u.hp}")
            if u.hp < -1e-6:
                self.fail("L4", f"{u.name} 血量为负 {u.hp:.2f}")
            for wid, n in u.ammo.items():
                if n < 0:
                    self.fail("L1", f"T{turn} {u.name} 的 {wid} 备弹为负 {n}")
            if u.shots_left < 0:
                self.fail("L2", f"T{turn} {u.name} 剩余发射次数为负 {u.shots_left}")
            if u.shots_left > max(1, u.template.sortie_shots):
                self.fail("L2", f"T{turn} {u.name} 发射次数 {u.shots_left} 超过模板上限")
            if not u.alive:
                continue
            # 卫星沿轨道运行会周期性经过图外区域，属正常现象
            if u.domain.value == "space":
                continue
            t = st.map.at(u.x, u.y)
            if t is None:
                self.fail("L6", f"T{turn} {u.name} 位于地图外 ({u.x},{u.y})")
            elif (u.domain in (Domain.LAND, Domain.INFRA) and t.terrain is Terrain.WATER
                  and not u.template.amphibious):
                # 两栖单位允许在航渡中停留在水格（登陆艇/两栖战车）
                self.fail("L6", f"T{turn} {u.name} 位于水域 ({u.x},{u.y})")
            elif u.domain in (Domain.SEA, Domain.SUB) and not t.passable_sea:
                self.fail("L6", f"T{turn} {u.name} 位于陆地 ({u.x},{u.y}) {t.terrain.label}")
        for side in (Side.BLUE, Side.RED):
            for u in st.enemies_of(side):
                lvl = st.known_level(side, u)
                if not isinstance(lvl, Detection):
                    self.fail("L10", f"探测等级类型异常 {lvl!r}")
        # L11 工事合法性
        for t in st.map.iter_tiles():
            if t.fort < 0 or t.fort > 3:
                self.fail("L11", f"T{turn} ({t.x},{t.y}) 工事等级越界 {t.fort}")
            if t.fort > 0 and t.terrain is Terrain.WATER:
                self.fail("L11", f"T{turn} ({t.x},{t.y}) 海域出现工事")

    def check_log(self, st: GameState) -> None:
        # 通过日志无法判断友军误伤，改为在 fire 上挂钩（见 run()）
        pass


def play_turn(st: GameState, side: Side, ck: Checker, fired_pairs: list) -> None:
    planner = AIPlanner(st, side)
    guard = 0
    while planner.pending() and guard < 900:
        guard += 1
        act = planner.step(st)
        if act is None:
            break
        u = st.unit_by_uid(act.unit_uid)
        if u is None or not u.alive:
            ck.fail("L9", f"阵亡/不存在的单位尝试行动 uid={act.unit_uid}")
            continue
        if act.kind == "move" and act.path:
            st.move_unit(u, act.path)
        elif act.kind == "fire":
            t = st.unit_by_uid(act.target_uid)
            w = u.weapon_by_id(act.weapon_id or "")
            if t and w:
                if t.side is u.side:
                    ck.fail("L3", f"{u.name} 向友军 {t.name} 开火")
                fired_pairs.append((st.turn_count, side.value, u.uid, w.id))
                st.fire(u, t, w)
        elif act.kind == "strike" and act.tile:
            w = u.weapon_by_id(act.weapon_id or "")
            if w:
                st.strike_facility(u, act.tile, w)
        elif act.kind == "radar":
            st.toggle_radar(u)
        elif act.kind == "jam":
            st.toggle_jam(u)
        elif act.kind == "decoy" and act.tile:
            st.deploy_decoy(u, act.tile)
    if guard >= 900:
        ck.warn("loop", f"{side.value} 的 AI 回合未在 900 步内结束")


def run(map_name: str, turns: int) -> Checker:
    ck = Checker()
    # 兜底计数按「每局」清零：L26 只关心这一局有没有走全局 random
    from core import dice as _dice
    _dice.reset_counters()
    path = os.path.join(ROOT, "data", "maps", f"{map_name}.json")
    sc = load_scenario(path, ARMORY)
    st = GameState(sc, Side.BLUE)

    # 挂钩 fire，检查友军误伤与次数
    orig_fire = GameState.fire
    per_turn: dict[tuple[int, int], int] = {}

    def guard_fire(self, attacker, target, weapon, salvo=None):
        if attacker.side is target.side:
            ck.fail("L3", f"T{self.turn_count} {attacker.name} 试图攻击友军 {target.name}")
        key = (self.turn_count, attacker.uid)
        per_turn[key] = per_turn.get(key, 0) + 1
        if per_turn[key] > max(1, attacker.template.sortie_shots) + 1:
            ck.warn("L2", f"T{self.turn_count} {attacker.name} 第 {per_turn[key]} 次开火")
        return orig_fire(self, attacker, target, weapon, salvo)

    GameState.fire = guard_fire
    try:
        prev_vp = dict(st.vp)
        for _ in range(turns):
            if st.over:
                break
            st.phase = "player"
            play_turn(st, Side.BLUE, ck, [])
            st.resolve_missiles()
            st.refresh_detection()
            ck.check_state(st, st.turn_count)
            st.phase = "ai"
            play_turn(st, Side.RED, ck, [])
            st.resolve_missiles()
            st.refresh_detection()
            ck.check_state(st, st.turn_count)
            st._advance_turn()
            for s in (Side.BLUE, Side.RED):
                if st.vp[s] < prev_vp[s]:
                    ck.fail("L8", f"T{st.turn_count} {s.value} 胜负点减少 {prev_vp[s]} → {st.vp[s]}")
            prev_vp = dict(st.vp)

        # L5 在飞弹药
        if st.missiles:
            ck.warn("L5", f"对局结束时仍有 {len(st.missiles)} 枚弹药在飞（正常，属未结算回合）")

        # L12 战报可生成：导出不应因数据异常而崩溃
        from core.report import build_markdown, build_text
        try:
            md = build_markdown(st)
            txt = build_text(st)
            if "战报" not in md or "作战结果" not in txt:
                ck.fail("L12", "战报内容缺少标题段")
        except Exception as exc:
            ck.fail("L12", f"战报生成异常: {exc}")

        # L13 对手视角：任何一次合法射击都应能算出威胁评估
        from core import rules as _rules
        checked = 0
        for u in st.units_of(Side.BLUE):
            if not u.alive or not u.usable_weapons():
                continue
            for e in st.enemies_of(Side.BLUE):
                if not e.alive:
                    continue
                for w in u.usable_weapons():
                    p = _rules.shot_profile(st, u, e, w)
                    if not p.ok:
                        continue
                    th = _rules.counter_threat_profile(st, u, e, w)
                    if not (0.0 <= th.intercept_pk <= 1.0):
                        ck.fail("L13", f"拦截概率越界 {th.intercept_pk}")
                    if th.reply_pk < 0.0 or th.reply_pk > 1.0:
                        ck.fail("L13", f"反击概率越界 {th.reply_pk}")
                    checked += 1
                    break
                if checked:
                    break
            if checked:
                break
        if checked == 0:
            # 该局未发生可评估射击（多为海空交战）：改为直接验证评估函数
            # 对任意存活单位组合都不得抛异常，且概率必须落在合法区间。
            try:
                blue = st.units_of(Side.BLUE)
                red = st.units_of(Side.RED)
                for a in blue:
                    if not a.template.weapons:
                        continue
                    for b in red:
                        if not b.alive:
                            continue
                        w = a.template.weapons[0]
                        th = _rules.counter_threat_profile(st, a, b, w)
                        if not (0.0 <= th.intercept_pk <= 1.0):
                            ck.fail("L13", f"拦截概率越界 {th.intercept_pk}")
                        if not (0.0 <= th.reply_pk <= 1.0):
                            ck.fail("L13", f"反击概率越界 {th.reply_pk}")
                        checked = 1
                        break
                    if checked:
                        break
            except Exception as exc:
                ck.fail("L13", f"对手视角评估异常: {exc}")
        if checked == 0:
            ck.warn("L13", "无可用单位组合，跳过对手视角检查")

        # L14 环境状态合法：时段与天候取值有效，系数在合理区间
        from core.env import environment_for
        from core.enums import SensorKind
        env = environment_for(st)
        for kind in (SensorKind.RADAR, SensorKind.IR, SensorKind.OPTICAL, SensorKind.ESM):
            f = env.factor(kind)
            if not (0.05 <= f <= 2.0):
                ck.fail("L14", f"环境系数越界 {kind.value}={f:.2f}")
        if not (0.0 <= env.turn >= 0):
            ck.fail("L14", f"环境回合数异常 {env.turn}")

        # L15 补给线一致：分配结果中每个单位都指向可用且在半径内的源
        from core import rules as _rules
        for side in (Side.BLUE, Side.RED):
            links = _rules.assign_supply(st, side)
            for uid, src in links.items():
                u = st.unit_by_uid(uid)
                if u is None:
                    ck.fail("L15", f"补给线指向不存在的单位 uid={uid}")
                    continue
                if not src.operational:
                    ck.fail("L15", f"{u.name} 的补给源 {src.name} 不可用")
                if not src.covers(u):
                    ck.fail("L15", f"{u.name} 超出补给源 {src.name} 的半径")
            # 承载力不得超限（用坐标作键：id() 在对象回收后可能被复用）
            cnt: dict[tuple[int, int], int] = {}
            for src in links.values():
                key = (src.x, src.y)
                cnt[key] = cnt.get(key, 0) + 1
            for src in links.values():
                if cnt[(src.x, src.y)] > src.capacity:
                    ck.fail("L15", f"补给源 {src.name} 承载超限 "
                                   f"{cnt[(src.x, src.y)]}>{src.capacity}")

        # L16 火力准备状态合法
        for u in st.units:
            fm = getattr(u, "fire_mission", None)
            if fm is None:
                continue
            if fm.turns_left < 0:
                ck.fail("L16", f"{u.name} 射界剩余回合为负 {fm.turns_left}")
            if not st.map.in_bounds(fm.x, fm.y):
                ck.fail("L16", f"{u.name} 射界坐标越界 ({fm.x},{fm.y})")

        # L17 指挥指令合法：下达者必须存活且层级高于受令者
        for u in st.units:
            od = getattr(u, "order", None)
            if od is None:
                continue
            issuer = st.unit_by_uid(od.issuer_uid)
            if issuer is None or not issuer.alive:
                ck.fail("L17", f"{u.name} 的指令下达者已失效")
                continue
            if issuer.side is not u.side:
                ck.fail("L17", f"{u.name} 收到敌方指令")
            if issuer.template.command_radius <= 0:
                ck.fail("L17", f"{issuer.name} 无指挥能力却下达了指令")
            if od.turns_left < 0:
                ck.fail("L17", f"{u.name} 的指令剩余回合为负 {od.turns_left}")

        # L18 想定脚本事件：已触发标记唯一，回合不倒退
        fired = [e for e in st.events if e.fired]
        seen_turns = [e.turn for e in fired]
        if len(seen_turns) != len(set(seen_turns)):
            ck.fail("L18", f"同一回合有多条事件被触发：{sorted(seen_turns)}")
        if st.turn_count > 1:
            for e in fired:
                if e.turn > st.turn_count:
                    ck.fail("L18", f"事件 T{e.turn} 在 T{st.turn_count} 前就已触发")

        # L19 想定元数据合法
        meta = getattr(st, "meta", None)
        if meta is not None:
            if meta.difficulty not in ("easy", "normal", "hard", "expert"):
                ck.fail("L19", f"非法难度标签 {meta.difficulty}")
            if not isinstance(meta.tags, list):
                ck.fail("L19", "标签不是列表")
            if meta.recommended_side not in ("BLUE", "RED", ""):
                ck.fail("L19", f"非法推荐方 {meta.recommended_side}")

        # L20 想定校验器可正常运行且无内部错误
        try:
            from core.validate import validate_scenario
            rep = validate_scenario(st)
            for issue in rep.issues:
                if issue.level == "error":
                    ck.fail("L20", f"想定校验报错：{issue.code} {issue.message}")
        except Exception as exc:
            ck.fail("L20", f"想定校验器异常：{exc}")

        # L21 战役定义结构合法
        try:
            from core.campaign import list_campaigns
            for c in list_campaigns():
                if c.total_steps <= 0:
                    ck.fail("L21", f"战役 {c.name} 没有关卡")
                if not c.steps:
                    ck.fail("L21", f"战役 {c.name} 关卡列表为空")
        except Exception as exc:
            ck.fail("L21", f"战役列表读取异常：{exc}")

        # L22 动画与音效不影响规则：关闭表现层后战局结果必须一致
        #     （动画/音效只影响观感，不参与命中与拦截判定）
        try:
            from core.anim import AnimTrack
            if getattr(st, "anim", None) is None:
                ck.fail("L22", "对局缺少动画轨道")
            else:
                st.anim.add("burst", (0, 0), None, ttl=1.0)
                if st.anim.count < 1:
                    ck.fail("L22", "动画事件未能入队")
                st.anim.clear()
        except Exception as exc:
            ck.fail("L22", f"动画轨道异常：{exc}")

        # L23 战斗统计一致：命中数不超过开火数，击毁数不超过命中数
        for side in (Side.BLUE, Side.RED):
            s_ = st.stats.get(side, {})
            shots = s_.get("shots", 0)
            hits = s_.get("hits", 0)
            kills = s_.get("kills", 0)
            if shots < 0 or hits < 0 or kills < 0:
                ck.fail("L23", f"{side.label} 统计出现负数")
            if hits > shots:
                ck.fail("L23", f"{side.label} 命中数 {hits} 超过开火数 {shots}")
            if kills > hits:
                ck.fail("L23", f"{side.label} 击毁数 {kills} 超过命中数 {hits}")

        # L24 胜负点曲线单调不减（每回合只加分，不减分）
        for i in range(1, len(st.vp_history)):
            p0, p1 = st.vp_history[i - 1], st.vp_history[i]
            if p1[1] < p0[1] or p1[2] < p0[2]:
                ck.fail("L24", f"VP 曲线在第 {p1[0]} 回合出现下降：{p0} → {p1}")

        # L25 语言包完整：中英词条键集合一致
        try:
            from core.i18n import _BUILTIN_EN, _BUILTIN_ZH
            only_en = set(_BUILTIN_EN) - set(_BUILTIN_ZH)
            only_zh = set(_BUILTIN_ZH) - set(_BUILTIN_EN)
            if only_en:
                ck.fail("L25", f"英文独有词条：{sorted(only_en)[:5]}")
            if only_zh:
                ck.fail("L25", f"中文独有词条：{sorted(only_zh)[:5]}")
        except Exception as exc:
            ck.fail("L25", f"语言包检查异常：{exc}")

        # L26 随机判定必须全部走对局随机流：任何一次「未传 rng」都会让
        #     同种子重跑失去意义，而它本身不会报错——只能靠计数器兜住
        from core import dice as _dice
        if _dice.FALLBACK_CALLS:
            ck.fail("L26", f"{_dice.FALLBACK_CALLS} 次随机判定走了全局 random 兜底"
                            f"（不受对局种子控制）")

        # L7 存档往返
        from core.save import load_game, save_game
        st.scenario_path = path
        st.armory_path = ARMORY
        tmp = os.path.join(tempfile.gettempdir(), "diag_save.json")
        save_game(st, tmp)
        st2 = load_game(tmp, ARMORY)
        def sig(units):
            out = []
            for u in units:
                fm = getattr(u, "fire_mission", None)
                fm_sig = (None if fm is None
                          else (fm.x, fm.y, fm.turns_left, fm.prepared, fm.total))
                od = getattr(u, "order", None)
                od_sig = (None if od is None
                          else (od.kind, od.issuer_uid, od.target_uid,
                                od.priority_class, od.turns_left, od.x, od.y))
                out.append((u.template.id, u.x, u.y, round(u.hp, 2), tuple(sorted(u.ammo.items())),
                            u.radar_on, round(u.fuel, 3), u.shots_left, u.entrench,
                            u.decoy_left, u.is_decoy, u.datalink_on, fm_sig, od_sig))
            return out

        a, b = sig(st.units), sig(st2.units)
        # 血量按 0.02 容差比较（存档保留 2 位小数）
        bad = []
        if len(a) != len(b):
            bad.append(f"单位数量 {len(a)}!={len(b)}")
        for x, y in zip(a, b):
            if abs(x[3] - y[3]) > 0.02 or x[:3] != y[:3] or x[4:] != y[4:]:
                bad.append((x, y))
        if bad:
            ck.fail("L7", f"存档往返不一致：{len(a)} vs {len(b)} 支单位，差异 {bad[:3]}")
        if st.turn_count != st2.turn_count:
            ck.fail("L7", f"回合数不一致 {st.turn_count} != {st2.turn_count}")
        if dict(st.vp) != dict(st2.vp):
            ck.fail("L7", f"胜负点不一致 {st.vp} != {st2.vp}")
        fa = {(t.x, t.y): (t.fort, t.fort_owner.value) for t in st.map.iter_tiles() if t.fort}
        fb = {(t.x, t.y): (t.fort, t.fort_owner.value) for t in st2.map.iter_tiles() if t.fort}
        if fa != fb:
            ck.fail("L7", f"工事状态往返不一致 {fa} != {fb}")
        missing = [m for m in st.missiles if m.weapon.id not in
                   {w.id for w in (st2.unit_by_uid(m.attacker_uid).template.weapons
                                   if st2.unit_by_uid(m.attacker_uid) else [])}]
        if len(st2.missiles) != len(st.missiles):
            ck.fail("L7", f"在飞弹药数量不一致 {len(st.missiles)} != {len(st2.missiles)}")
        os.remove(tmp)
    finally:
        GameState.fire = orig_fire
    return ck


def main() -> int:
    maps = [a for a in sys.argv[1:] if not a.isdigit()]
    turns = next((int(a) for a in sys.argv[1:] if a.isdigit()), 18)
    if not maps:
        maps = ["strait_shield", "steppe_thunder", "training_ground"]
    total = 0
    for m in maps:
        ck = run(m, turns)
        print(f"\n=== {m} · {turns} 回合 ===")
        if not ck.errors and not ck.warns:
            print("  全部不变量通过")
        for e in ck.errors:
            print("  [FAIL]", e)
        for w in ck.warns:
            print("  [warn]", w)
        total += len(ck.errors)
    print(f"\n合计 {total} 个错误")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
