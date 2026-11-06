"""存档与读档。

存档为纯 JSON：记录想定路径、全部单位状态、地块可变状态（归属/烟幕/设施完好度）、
在飞弹药、胜负点、**对局随机流状态**与日志尾部。读档时重新从想定文件重建地形
与兵种模板，再套用存档状态。

两处容易踩的坑，改动前请先读：

1. **随机流必须整份存**（``rng``）。只存种子还原不了「已经消耗了 N 次随机数」
   之后的序列——不同分布消耗的随机数个数不同。少了这一块，读档后的每一次
   判定都换了条流，"同种子可重演"在读档边界就断了。
2. **任何字段错误都翻译成 SaveError**。``main.py`` 的顶层只有一个大 ``except``，
   ``ValueError`` / ``KeyError`` 冒泡上去会直接结束进程；坏档顶多该让"读档"失败。
"""
from __future__ import annotations

import json
import os
from typing import Any

from .engine import GameState, LogEntry
from .enums import Altitude, Facility, Side
from .loader import LoadError, load_scenario
from .model import FireMission, Missile, Order, Unit, ensure_uid_above

# v6：新增 "rng" 随机流状态。旧版程序读 v6 存档会被版本检查拦下——那是对的，
# 它无法忠实还原读档后的随机序列，不如明确拒绝。
SAVE_VERSION = 6


class SaveError(Exception):
    """存档读写失败。"""


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #
def _unit_dict(u: Unit) -> dict[str, Any]:
    return {
        "uid": u.uid,
        "template": u.template.id,
        "side": u.side.value,
        "x": u.x, "y": u.y,
        "hp": round(u.hp, 2),
        "mp": round(u.mp, 3),
        "ammo": dict(u.ammo),
        "altitude": u.altitude.value,
        "radar_on": u.radar_on,
        "jam_on": u.jam_on,
        "datalink_on": u.datalink_on,
        "fired": u.fired,
        "moved": u.moved,
        "destroyed": u.destroyed,
        "suppressed": u.suppressed,
        "fuel": round(u.fuel, 3),
        "spares": round(u.spares, 1),
        "supply_dry": u.supply_dry,
        "name": u.name,
        "revealed_until": u.revealed_until,
        "home_base": list(u.home_base) if u.home_base else None,
        "on_station": u.on_station,
        # 回合内状态：不保存会导致读档后「发射次数/工事/诱饵」被重置
        "shots_left": u.shots_left,
        "entrench": u.entrench,
        "decoy_left": u.decoy_left,
        "is_decoy": u.is_decoy,
        "decoy_kind": u.decoy_kind,
        "decoy_ttl": u.decoy_ttl,
        "mission": u.mission,
        "intercepts_used": u.intercepts_used,
        "fire_mission": (None if u.fire_mission is None else {
            "x": u.fire_mission.x, "y": u.fire_mission.y,
            "turns_left": u.fire_mission.turns_left,
            "prepared": u.fire_mission.prepared,
            "total": u.fire_mission.total,
            "intel_level": u.fire_mission.intel_level,
        }),
        "order": (None if u.order is None else {
            "kind": u.order.kind,
            "issuer_uid": u.order.issuer_uid,
            "target_uid": u.order.target_uid,
            "priority_class": u.order.priority_class,
            "turns_left": u.order.turns_left,
            "x": u.order.x, "y": u.order.y,
        }),
    }


def _tile_dict(t) -> dict[str, Any]:
    return {
        "x": t.x, "y": t.y,
        "owner": t.owner.value,
        "facility": t.facility.value,
        "facility_hp": round(t.facility_hp, 2),
        "facility_max_hp": round(t.facility_max_hp, 2),
        "smoke": t.smoke,
        "objective": t.objective,
        "objective_name": t.objective_name,
        "victory_points": t.victory_points,
        "damage": t.damage,
        "fort": t.fort,
        "fort_owner": t.fort_owner.value,
        "fort_progress": t.fort_progress,
        "fort_build": t.fort_build,
        "fort_rubble": t.fort_rubble,
    }


def _missile_dict(m: Missile) -> dict[str, Any]:
    return {
        "weapon": m.weapon.id,
        "attacker_uid": m.attacker_uid,
        "attacker_side": m.attacker_side.value,
        "target_uid": m.target_uid,
        "origin": list(m.origin),
        "target_pos": list(m.target_pos),
        "eta": m.eta,
        "salvo": m.salvo,
        "midcourse_windows": m.midcourse_windows,
        "label": m.label,
    }


def game_to_dict(state: GameState) -> dict[str, Any]:
    return {
        "version": SAVE_VERSION,
        "scenario": state.scenario_path,
        "armory": state.armory_path,
        "player_side": state.player_side.value,
        "turn": state.turn_count,
        "turn_limit": state.turn_limit,
        "phase": state.phase,
        "over": state.over,
        "winner": state.winner.value if state.winner else None,
        "win_reason": state.win_reason,
        "salvo": state.salvo_setting,
        # 随机流：种子 + 已消耗次数 + random 内部状态（见 core/rng.py）
        "rng": state.rng.getstate(),
        "vp": {s.value: v for s, v in state.vp.items()},
        "gps": {s.value: round(v, 3) for s, v in state.gps.items()},
        "casualties": {s.value: v for s, v in state.casualties.items()},
        "stats": {s.value: dict(v) for s, v in state.stats.items()},
        "vp_history": [list(t) for t in state.vp_history],
        "units": [_unit_dict(u) for u in state.units],
        "tiles": [_tile_dict(t) for t in state.map.iter_tiles()
                  if t.owner is not Side.NEUTRAL or t.smoke or t.facility is not Facility.NONE
                  or t.facility_hp < t.facility_max_hp or t.objective or t.fort > 0
                  or t.fort_owner is not Side.NEUTRAL or t.fort_build > 0 or t.fort_rubble],
        "missiles": [_missile_dict(m) for m in state.missiles],
        "events": [{"turn": e.turn, "type": e.type, "fired": e.fired}
                   for e in getattr(state, "events", [])],
        "log": [{"turn": e.turn, "side": e.side.value if e.side else None,
                 "text": e.text, "kind": e.kind} for e in state.log[-150:]],
    }


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def save_game(state: GameState, path: str) -> None:
    if not state.scenario_path:
        raise SaveError("当前对局缺少想定来源，无法存档")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    data = game_to_dict(state)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError as exc:
        raise SaveError(f"写入存档失败：{exc}") from None


def _seed_from_save(data: dict[str, Any]) -> int | None:
    """从存档里取种子；取不到就返回 ``None``（由 GameRng 自行生成）。

    v6 起种子在 ``rng.seed`` 里。字段脏了也不抛——真正的状态恢复由随后的
    ``setstate`` 负责，那里失败才判定存档损坏。
    """
    rng_state = data.get("rng")
    raw = rng_state.get("seed") if isinstance(rng_state, dict) else data.get("seed")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def load_game(path: str, default_armory: str = "") -> GameState:
    """读取存档。

    所有字段级错误（缺键、类型不对、枚举越界、索引越界）在这里统一翻译成
    :class:`SaveError`——界面只需处理一种异常，进程不会因为一个坏档退出。
    """
    if not os.path.isfile(path):
        raise SaveError(f"存档不存在：{path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise SaveError(f"读取存档失败：{exc}") from None
    if not isinstance(data, dict):
        raise SaveError("存档格式不正确：顶层应当是一个 JSON 对象")

    try:
        return _restore_game(data, default_armory)
    except SaveError:
        raise
    except OSError as exc:
        # 想定/武器库在读完存档后才被删掉或锁住：报清楚是哪一步失败
        raise SaveError(f"读取存档依赖的文件失败：{exc}") from None
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
        # 原始异常类型对玩家没有意义，附上字段线索更有用
        raise SaveError(f"存档内容损坏：{type(exc).__name__}: {exc}") from None


def _restore_game(data: dict[str, Any], default_armory: str) -> GameState:
    if int(data.get("version", 0)) > SAVE_VERSION:
        raise SaveError("存档版本高于当前程序，请升级后再读取")

    scenario_path = data.get("scenario", "")
    armory_path = data.get("armory") or default_armory
    if not scenario_path or not os.path.isfile(scenario_path):
        raise SaveError(f"存档引用的想定文件已不存在：{scenario_path}")

    try:
        scenario = load_scenario(scenario_path, armory_path)
    except LoadError as exc:
        raise SaveError(f"重建想定失败：{exc}") from None

    side = Side(data.get("player_side", "BLUE"))
    # 先用存档里的种子建流（v6 起有）；没有就取新种子——下一步 setstate
    # 会用完整状态覆盖它，这里只是避免中间步骤跑在一条无关的流上。
    state = GameState(scenario, side, seed=_seed_from_save(data))
    state.scenario_path = scenario_path
    state.armory_path = armory_path

    # 随机流状态：接着存档那一刻的序列继续走，否则「同种子可重演」在读档处断裂
    rng_state = data.get("rng")
    if isinstance(rng_state, dict):
        try:
            state.rng.setstate(rng_state)
        except (KeyError, TypeError, ValueError) as exc:
            raise SaveError(f"存档的随机流状态损坏：{exc}") from None
        state.seed = state.rng.seed

    # 1) 单位
    units: list[Unit] = []
    max_uid = 0
    for d in data.get("units", []):
        tid = d.get("template")
        if tid not in scenario.armory.templates:
            continue
        tpl = scenario.armory.templates[tid]
        u = Unit(template=tpl, side=Side(d.get("side", "BLUE")),
                 x=int(d["x"]), y=int(d["y"]))
        u.uid = int(d.get("uid", u.uid))
        max_uid = max(max_uid, u.uid)
        u.hp = float(d.get("hp", tpl.hp))
        u.mp = float(d.get("mp", tpl.mp))
        u.ammo = {w.id: int(d.get("ammo", {}).get(w.id, 0)) for w in tpl.weapons}
        u.altitude = Altitude(d.get("altitude", "ground"))
        u.radar_on = bool(d.get("radar_on", True))
        u.jam_on = bool(d.get("jam_on", False))
        u.datalink_on = bool(d.get("datalink_on", True))
        u.fired = bool(d.get("fired", False))
        u.moved = bool(d.get("moved", False))
        u.destroyed = bool(d.get("destroyed", False))
        u.suppressed = int(d.get("suppressed", 0))
        u.fuel = float(d.get("fuel", 1.0))
        u.spares = float(d.get("spares", tpl.hp * 0.5))
        u.supply_dry = int(d.get("supply_dry", 0))
        u.name = d.get("name") or tpl.name
        u.revealed_until = int(d.get("revealed_until", 0))
        hb = d.get("home_base")
        u.home_base = (int(hb[0]), int(hb[1])) if hb else None
        u.on_station = int(d.get("on_station", 0))
        u.is_decoy = bool(d.get("is_decoy", False))
        u.decoy_kind = str(d.get("decoy_kind", ""))
        u.decoy_ttl = int(d.get("decoy_ttl", 0))
        if u.is_decoy:
            # 诱饵不是真实编制：不恢复备弹与发射能力
            u.ammo = {}
            u.shots_left = 0
        else:
            u.shots_left = max(0, int(d.get("shots_left", tpl.sortie_shots)))
            u.decoy_left = max(0, int(d.get("decoy_left", tpl.decoy_charges)))
        u.entrench = max(0, int(d.get("entrench", 0)))
        u.mission = str(d.get("mission", "strike"))
        u.intercepts_used = max(0, int(d.get("intercepts_used", 0)))
        fm = d.get("fire_mission")
        if isinstance(fm, dict):
            u.fire_mission = FireMission(
                x=int(fm.get("x", 0)), y=int(fm.get("y", 0)),
                turns_left=max(0, int(fm.get("turns_left", 0))),
                prepared=bool(fm.get("prepared", False)),
                total=max(1, int(fm.get("total", 2))),
                intel_level=str(fm.get("intel_level", "contact")),
            )
        od = d.get("order")
        if isinstance(od, dict):
            u.order = Order(
                kind=str(od.get("kind", "focus_fire")),
                issuer_uid=int(od.get("issuer_uid", 0)),
                target_uid=int(od.get("target_uid", 0)),
                priority_class=str(od.get("priority_class", "")),
                turns_left=max(0, int(od.get("turns_left", 0))),
                x=int(od.get("x", 0)), y=int(od.get("y", 0)),
            )
        units.append(u)
    state.units = units
    ensure_uid_above(max_uid)
    # 恢复对局内 uid 分配器——读档后新增的增援单位不能与存档里的撞号
    if hasattr(state, "_uid_next"):
        state._uid_next = max_uid + 1

    # 2) 地块可变状态
    for d in data.get("tiles", []):
        t = state.map.at(int(d["x"]), int(d["y"]))
        if t is None:
            continue
        t.owner = Side(d.get("owner", "NEUTRAL"))
        t.facility = Facility(d.get("facility", t.facility.value))
        t.facility_hp = float(d.get("facility_hp", t.facility_hp))
        t.facility_max_hp = float(d.get("facility_max_hp", t.facility_max_hp))
        t.smoke = int(d.get("smoke", 0))
        t.objective = bool(d.get("objective", t.objective))
        t.objective_name = d.get("objective_name", t.objective_name)
        t.victory_points = int(d.get("victory_points", t.victory_points))
        t.damage = int(d.get("damage", 0))
        t.fort = max(0, min(3, int(d.get("fort", t.fort))))
        t.fort_owner = Side(d.get("fort_owner", t.fort_owner.value))
        t.fort_progress = int(d.get("fort_progress", 0))
        # v3.1 新增：缺失取默认，v6 旧档照常读（工时从 0 起算，废墟标记为假）
        t.fort_build = max(0, int(d.get("fort_build", 0)))
        t.fort_rubble = bool(d.get("fort_rubble", False))

    # 目标点归属同步
    for o in state.map.objectives:
        tile = state.map.at(o.x, o.y)
        if tile is not None:
            o.holder = tile.owner

    # 地块状态已从存档恢复，渲染层的地块索引需要重建
    state.map_epoch += 1

    # 3) 在飞弹药
    missiles: list[Missile] = []
    for d in data.get("missiles", []):
        attacker = state.unit_by_uid(int(d["attacker_uid"]))
        if attacker is None:
            continue
        w = attacker.weapon_by_id(d["weapon"])
        if w is None:
            continue
        missiles.append(Missile(
            weapon=w, attacker_uid=attacker.uid,
            attacker_side=Side(d.get("attacker_side", attacker.side.value)),
            target_uid=int(d["target_uid"]),
            origin=tuple(d.get("origin", (attacker.x, attacker.y))),
            target_pos=tuple(d.get("target_pos", (attacker.x, attacker.y))),
            eta=int(d.get("eta", 1)), salvo=int(d.get("salvo", 1)),
            midcourse_windows=int(d.get("midcourse_windows", 0)),
            label=d.get("label", w.name),
        ))
    state.missiles = missiles

    # 4b) 想定事件：恢复已触发状态，避免读档后事件重复执行
    fired_map = {int(d.get("turn", 0)): bool(d.get("fired", False))
                 for d in data.get("events", []) if isinstance(d, dict)}
    for ev in getattr(state, "events", []):
        if ev.turn in fired_map:
            ev.fired = fired_map[ev.turn]

    # 5) 全局计数与日志
    state.turn_count = int(data.get("turn", 1))
    state.turn_limit = int(data.get("turn_limit", state.turn_limit))
    state.phase = data.get("phase", "player")
    if state.phase == "ai":
        state.phase = "player"      # 存档不保存 AI 中间态
    state.over = bool(data.get("over", False))
    winner = data.get("winner")
    state.winner = Side(winner) if winner else None
    state.win_reason = data.get("win_reason", "")
    state.salvo_setting = int(data.get("salvo", 1))
    for s, v in data.get("vp", {}).items():
        state.vp[Side(s)] = int(v)
    for s, v in data.get("gps", {}).items():
        state.gps[Side(s)] = float(v)
    for s, v in data.get("casualties", {}).items():
        state.casualties[Side(s)] = int(v)
    for s, v in data.get("stats", {}).items():
        try:
            side = Side(s)
        except ValueError:
            continue
        if isinstance(v, dict):
            for k, n in v.items():
                if k in state.stats[side]:
                    state.stats[side][k] = int(n)
    state.vp_history = []
    for row in data.get("vp_history", []):
        if isinstance(row, (list, tuple)) and len(row) == 3:
            state.vp_history.append((int(row[0]), int(row[1]), int(row[2])))

    state.log = []
    for e in data.get("log", []):
        sd = e.get("side")
        state.log.append(LogEntry(int(e.get("turn", 1)), Side(sd) if sd else None,
                                  e.get("text", ""), e.get("kind", "info")))
    state.log_message(None, f"读取存档：第 {state.turn_count} 回合 · {state.map.name}", "system")

    state._ai_planner = None
    # AI 的作战决心不存档：它记的是「某支部队要去某格」，读档后单位 uid
    # 与位置都可能已变，沿用旧决心会让 AI 往空地上跑。清掉，让它重新判断。
    for _side in (Side.BLUE, Side.RED):
        state.ai_assign[_side].clear()
        state.ai_assign_turn[_side].clear()
    state.refresh_detection()
    return state


# --------------------------------------------------------------------------- #
def save_dir(root: str) -> str:
    d = os.path.join(root, "saves")
    os.makedirs(d, exist_ok=True)
    return d


def slot_path(root: str, slot: int) -> str:
    return os.path.join(save_dir(root), f"slot{slot}.json")


def list_slots(root: str) -> list[dict]:
    """列出存档槽位信息，供界面展示。"""
    out = []
    d = save_dir(root)
    for i in range(1, 6):
        p = os.path.join(d, f"slot{i}.json")
        if not os.path.isfile(p):
            out.append({"slot": i, "exists": False, "path": p})
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            out.append({
                "slot": i, "exists": True, "path": p,
                "turn": data.get("turn", 0),
                "scenario": os.path.basename(data.get("scenario", "")),
                "player": data.get("player_side", "BLUE"),
                "mtime": os.path.getmtime(p),
            })
        except Exception:
            out.append({"slot": i, "exists": False, "path": p})
    return out


__all__ = [
    "SaveError", "SAVE_VERSION", "game_to_dict", "save_game", "load_game",
    "save_dir", "slot_path", "list_slots",
]
