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
                out.append((u.template.id, u.x, u.y, round(u.hp, 2), tuple(sorted(u.ammo.items())),
                            u.radar_on, round(u.fuel, 3), u.shots_left, u.entrench,
                            u.decoy_left, u.is_decoy))
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
        maps = ["strait_shield", "steppe_thunder"]
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
