"""数据加载器：读取可编辑的武器库 JSON 与地图/想定 JSON。

设计目标：
1. 数据文件全部为人类可读、可手工编辑的 JSON；
2. 加载失败时给出精确到字段的错误定位，避免「黑屏 + 无提示」；
3. 武器/传感器支持目录复用（catalog + ref），避免重复定义。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .enums import (
    Altitude, Detection, Domain, Facility, Guidance, ObjectiveKind, SensorKind,
    Side, TERRAIN_CHARS, Terrain, UnitClass, WeaponKind,
)
from .model import Objective, Sensor, Tile, Unit, UnitTemplate, Weapon


class LoadError(Exception):
    """数据文件格式错误。"""


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _req(d: dict, key: str, where: str) -> Any:
    if key not in d:
        raise LoadError(f"{where}: 缺少必填字段 '{key}'")
    return d[key]


def _enum(cls, value: str, where: str):
    try:
        return cls(value)
    except ValueError:
        options = ", ".join(m.value for m in cls)
        raise LoadError(f"{where}: 非法取值 '{value}'，可选值: {options}") from None


def _read_json(path: str) -> dict:
    if not os.path.isfile(path):
        raise LoadError(f"文件不存在: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise LoadError(f"{os.path.basename(path)} JSON 语法错误: 第 {exc.lineno} 行 {exc.msg}") from None


# --------------------------------------------------------------------------- #
# 武器库
# --------------------------------------------------------------------------- #
@dataclass
class Armory:
    """武器库：全部单位模板，按 id 索引。"""

    templates: dict[str, UnitTemplate] = field(default_factory=dict)
    weapons: dict[str, Weapon] = field(default_factory=dict)
    sensors: dict[str, Sensor] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def get(self, tid: str) -> UnitTemplate:
        if tid not in self.templates:
            raise LoadError(f"未定义的兵种模板: '{tid}'")
        return self.templates[tid]

    def by_domain(self, domain: Domain) -> list[UnitTemplate]:
        return [t for t in self.templates.values() if t.domain is domain]

    def all(self) -> list[UnitTemplate]:
        return list(self.templates.values())


def _parse_sensor(d: dict, where: str) -> Sensor:
    return Sensor(
        kind=_enum(SensorKind, _req(d, "kind", where), where + ".kind"),
        range_max=float(_req(d, "range", where)),
        strength=float(d.get("strength", 1.0)),
        counter_stealth=float(d.get("counter_stealth", 0.0)),
        note=d.get("note", ""),
    )


def _parse_weapon(d: dict, where: str) -> Weapon:
    targets_raw = _req(d, "targets", where)
    if isinstance(targets_raw, str):
        targets_raw = [targets_raw]
    targets = tuple(_enum(Domain, t, where + ".targets") for t in targets_raw)
    rmax = d.get("range", d.get("range_max"))
    if rmax is None:
        raise LoadError(f"{where}: 缺少必填字段 'range'（或 'range_max'）")
    kind = _enum(WeaponKind, _req(d, "kind", where), where + ".kind")
    salvo = int(d.get("salvo", 1))
    default_facility = kind.value in ("artillery", "mlrs", "land_attack", "bomb", "arm", "asm", "gun")
    return Weapon(
        id=str(_req(d, "id", where)),
        name=str(_req(d, "name", where)),
        kind=kind,
        range_min=float(d.get("range_min", 0.0)),
        range_max=float(rmax),
        pk=float(_req(d, "pk", where)),
        damage=float(_req(d, "damage", where)),
        ammo=int(d.get("ammo", 1)),
        guidance=_enum(Guidance, d.get("guidance", "optical"), where + ".guidance"),
        targets=targets,
        real_range_km=str(d.get("real_range_km", "")),
        speed=float(d.get("speed", 1.0)),
        # 只有飞行弹药可被拦截：枪炮弹、火箭筒、航弹默认不可拦截
        interceptable=bool(d.get("interceptable",
                                  kind.is_missile or kind is WeaponKind.BOMB)),
        evasion=float(d.get("evasion", 0.0)),
        hypersonic=bool(d.get("hypersonic", False)),
        needs_los=bool(d.get("needs_los", False)),
        min_detection=_enum(Detection, d.get("min_detection", "tracked"), where + ".min_detection"),
        note=d.get("note", ""),
        salvo=max(1, salvo),
        max_salvo=max(1, int(d.get("max_salvo", salvo))),
        vs_facility=bool(d.get("vs_facility", default_facility)),
        asat=bool(d.get("asat", Domain.SPACE in targets)),
        stealth=float(d.get("stealth", 0.0)),
        ciws=bool(d.get("ciws", kind is WeaponKind.CIWS)),
        anti_fort=int(d.get("anti_fort",
                            1 if kind in (WeaponKind.ARTILLERY, WeaponKind.MLRS,
                                          WeaponKind.BOMB) else 0)),
        strategic=bool(d.get("strategic", False)),
        ballistic=bool(d.get("ballistic", False)),
        midcourse=bool(d.get("midcourse", False)),
        smoke=bool(d.get("smoke", False)),
    )


def _parse_unit(d: dict, index: int, weapons: dict[str, Weapon], sensors: dict[str, Sensor]) -> UnitTemplate:
    where = f"units[{index}]"
    uid = str(_req(d, "id", where))
    where = f"units[{index}]({uid})"

    sens: list[Sensor] = []
    for j, s in enumerate(d.get("sensors", [])):
        if isinstance(s, str):
            if s not in sensors:
                raise LoadError(f"{where}.sensors[{j}]: 未定义的传感器 '{s}'")
            sens.append(sensors[s])
        else:
            sens.append(_parse_sensor(s, f"{where}.sensors[{j}]"))

    wps: list[Weapon] = []
    for j, w in enumerate(d.get("weapons", [])):
        if isinstance(w, str):
            ref = w
            override: dict = {}
        else:
            ref = _req(w, "ref", f"{where}.weapons[{j}]")
            override = dict(w)
        if ref not in weapons:
            raise LoadError(f"{where}.weapons[{j}]: 未定义的武器 '{ref}'")
        base = weapons[ref]
        wps.append(Weapon(**{**base.__dict__, **{k: v for k, v in override.items() if k in base.__dict__}}))

    uclass = _enum(UnitClass, _req(d, "class", where), where + ".class")
    domain = _enum(Domain, _req(d, "domain", where), where + ".domain")
    return UnitTemplate(
        id=uid,
        name=str(_req(d, "name", where)),
        short=str(d.get("short", uid[:4].upper()))[:6],
        unit_class=uclass,
        domain=domain,
        hp=float(_req(d, "hp", where)),
        mp=float(_req(d, "mp", where)),
        armor=float(d.get("armor", 0.0)),
        stealth=float(d.get("stealth", 0.0)),
        evasion=float(d.get("evasion", 0.0)),
        sensors=sens,
        weapons=wps,
        jam_radius=float(d.get("jam_radius", 0.0)),
        jam_strength=float(d.get("jam_strength", 0.0)),
        jam_bands=tuple(str(b) for b in d.get("jam_bands", ())),
        jam_band_weights={str(k): float(v)
                          for k, v in (d.get("jam_band_weights", {}) or {}).items()},
        interceptors=int(d.get("interceptors", 0)),
        intercept_pk=float(d.get("intercept_pk", 0.0)),
        can_capture=bool(d.get("can_capture", False)),
        amphibious=bool(d.get("amphibious", False)),
        is_radar_emitter=bool(d.get("is_radar_emitter", any(s.kind is SensorKind.RADAR for s in sens))),
        max_altitude=_enum(Altitude, d.get("max_altitude", "ground"), where + ".max_altitude"),
        notes=d.get("notes", ""),
        cost=int(d.get("cost", 0)),
        sortie_shots=max(1, int(d.get("sortie_shots", 1))),
        datalink=bool(d.get("datalink", True)),
        decoy_charges=max(0, int(d.get("decoy_charges", 0))),
        entrench_bonus=float(d.get("entrench_bonus", 0.0)),
        is_infantry=bool(d.get("is_infantry", uclass is UnitClass.INFANTRY)),
        one_shot_kill=bool(d.get("one_shot_kill", domain is Domain.AIR)),
        can_fortify=bool(d.get("can_fortify", uclass is UnitClass.INFANTRY)),
        fort_power=max(1, int(d.get("fort_power", 1))),
        echelon=str(d.get("echelon", "platoon")),
        command_radius=float(d.get("command_radius", 0.0)),
        command_range=float(d.get("command_range", 0.0)),
    )


def load_armory(path: str) -> Armory:
    raw = _read_json(path)
    where = os.path.basename(path)

    weapons: dict[str, Weapon] = {}
    for w in raw.get("weapons", []):
        wd = _parse_weapon(w, f"{where}.weapons")
        weapons[wd.id] = wd

    sensors: dict[str, Sensor] = {}
    for i, s in enumerate(raw.get("sensors", [])):
        sw = f"{where}.sensors[{i}]"
        sid = str(_req(s, "id", sw))
        if sid in sensors:
            raise LoadError(f"{sw}: 传感器 id 重复 '{sid}'")
        sensors[sid] = _parse_sensor(s, sw)

    templates: dict[str, UnitTemplate] = {}
    for i, u in enumerate(_req(raw, "units", where)):
        t = _parse_unit(u, i, weapons, sensors)
        if t.id in templates:
            raise LoadError(f"{where}: 兵种 id 重复 '{t.id}'")
        templates[t.id] = t

    return Armory(templates=templates, weapons=weapons, sensors=sensors, meta=raw.get("meta", {}))


# --------------------------------------------------------------------------- #
# 地图
# --------------------------------------------------------------------------- #
@dataclass
class MapData:
    id: str
    name: str
    width: int
    height: int
    tile_km: float
    tiles: dict[tuple[int, int], Tile]
    objectives: list[Objective]
    meta: dict[str, Any] = field(default_factory=dict)
    landmass: dict[tuple[int, int], int] = field(default_factory=dict)

    def at(self, x: int, y: int) -> Tile | None:
        return self.tiles.get((x, y))

    def in_bounds(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def same_landmass(self, a: tuple[int, int], b: tuple[int, int]) -> bool:
        la = self.landmass.get(a)
        lb = self.landmass.get(b)
        return la is not None and la == lb

    def iter_tiles(self):
        for y in range(self.height):
            for x in range(self.width):
                yield self.tiles[(x, y)]


def _compute_landmass(tiles: dict[tuple[int, int], Tile], w: int, h: int) -> dict[tuple[int, int], int]:
    """连通地块编号：陆上单位只能在同一陆块内机动（除非具备两栖能力）。"""
    label: dict[tuple[int, int], int] = {}
    cur = 0
    for y in range(h):
        for x in range(w):
            if (x, y) in label:
                continue
            if tiles[(x, y)].terrain is Terrain.WATER:
                continue
            cur += 1
            stack = [(x, y)]
            label[(x, y)] = cur
            while stack:
                cx, cy = stack.pop()
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1),
                               (-1, -1), (1, -1), (-1, 1), (1, 1)):
                    nx, ny = cx + dx, cy + dy
                    if not (0 <= nx < w and 0 <= ny < h) or (nx, ny) in label:
                        continue
                    if tiles[(nx, ny)].terrain is Terrain.WATER:
                        continue
                    label[(nx, ny)] = cur
                    stack.append((nx, ny))
    return label


@dataclass
class Scenario:
    map: MapData
    armory: Armory
    units: list[Unit]
    turn_limit: int
    side_blue_objectives: list[Objective]
    raw: dict[str, Any] = field(default_factory=dict)


def _parse_rows(raw: dict, key: str, w: int, h: int, where: str) -> list[str]:
    rows = _req(raw, key, where)
    if not isinstance(rows, list) or len(rows) != h:
        raise LoadError(f"{where}.{key}: 需要 {h} 行，实际 {len(rows) if isinstance(rows, list) else '非数组'}")
    for i, r in enumerate(rows):
        if not isinstance(r, str) or len(r) != w:
            raise LoadError(f"{where}.{key}[{i}]: 需要长度 {w} 的字符串，实际 {len(r) if isinstance(r, str) else '非字符串'}")
    return rows


def load_map(path: str) -> MapData:
    raw = _read_json(path)
    where = os.path.basename(path)

    w, h = _req(raw, "size", where)
    if not (4 <= w <= 96 and 4 <= h <= 96):
        raise LoadError(f"{where}.size: 地图尺寸应在 4..96 之间")

    terrain_rows = _parse_rows(raw, "terrain", w, h, where)
    elev_cfg = raw.get("elevation", {})
    if isinstance(elev_cfg, dict) and "rows" in elev_cfg:
        elev_rows = _parse_rows(elev_cfg, "rows", w, h, where + ".elevation")
    else:
        elev_rows = None

    default_elev = {
        Terrain.WATER: 0, Terrain.COAST: 20, Terrain.PLAIN: 90, Terrain.FOREST: 160,
        Terrain.HILLS: 380, Terrain.MOUNTAIN: 1100, Terrain.URBAN: 120,
        Terrain.DESERT: 220, Terrain.MARSH: 30,
    }

    tiles: dict[tuple[int, int], Tile] = {}
    for y in range(h):
        for x in range(w):
            ch = terrain_rows[y][x]
            if ch not in TERRAIN_CHARS:
                raise LoadError(f"{where}.terrain[{y}][{x}]: 未知地形符号 '{ch}'")
            terrain = TERRAIN_CHARS[ch]
            if elev_rows:
                ec = elev_rows[y][x]
                if not ec.isdigit():
                    raise LoadError(f"{where}.elevation[{y}][{x}]: 需为 0-9 数字，实际 '{ec}'")
                elev = int(ec) * 300
            else:
                elev = default_elev[terrain]
            tiles[(x, y)] = Tile(x=x, y=y, terrain=terrain, elevation=elev)

    # 设施与归属
    for i, f in enumerate(raw.get("facilities", [])):
        fw = f"{where}.facilities[{i}]"
        x, y = _req(f, "x", fw), _req(f, "y", fw)
        if not (0 <= x < w and 0 <= y < h):
            raise LoadError(f"{fw}: 坐标越界 ({x},{y})")
        t = tiles[(x, y)]
        t.facility = _enum(Facility, _req(f, "facility", fw), fw + ".facility")
        t.owner = _enum(Side, f.get("owner", "NEUTRAL"), fw + ".owner")
        t.damage = int(f.get("damage", 0))
        t.facility_max_hp = float(f.get("hp", 100.0))
        t.facility_hp = float(f.get("hp_current", t.facility_max_hp * (1.0 - t.damage / 100.0)))

    # 野战工事
    for i, f in enumerate(raw.get("forts", [])):
        fw = f"{where}.forts[{i}]"
        x, y = int(_req(f, "x", fw)), int(_req(f, "y", fw))
        if not (0 <= x < w and 0 <= y < h):
            raise LoadError(f"{fw}: 坐标越界 ({x},{y})")
        t = tiles[(x, y)]
        if t.terrain is Terrain.WATER:
            raise LoadError(f"{fw}: 工事不能设在海域 ({x},{y})")
        t.fort = max(0, min(3, int(f.get("level", 1))))
        t.fort_owner = _enum(Side, f.get("owner", "NEUTRAL"), fw + ".owner")

    # 目标点
    objectives: list[Objective] = []
    for i, o in enumerate(raw.get("objectives", [])):
        ow = f"{where}.objectives[{i}]"
        x, y = int(_req(o, "x", ow)), int(_req(o, "y", ow))
        if not (0 <= x < w and 0 <= y < h):
            raise LoadError(f"{ow}: 坐标越界 ({x},{y})")
        t = tiles[(x, y)]
        t.objective = True
        t.objective_name = o.get("name", f"目标{i + 1}")
        t.victory_points = int(o.get("value", 1))
        objectives.append(Objective(
            x=x, y=y, name=t.objective_name,
            kind=_enum(ObjectiveKind, o.get("kind", "capture"), ow + ".kind"),
            side=_enum(Side, o.get("side", "NEUTRAL"), ow + ".side"),
            value=t.victory_points,
            holder=_enum(Side, o.get("owner", "NEUTRAL"), ow + ".owner"),
        ))
        t.owner = objectives[-1].holder

    return MapData(
        id=raw.get("id", os.path.basename(path)),
        name=raw.get("name", "未命名地图"),
        width=w, height=h,
        tile_km=float(raw.get("tile_km", 12)),
        tiles=tiles,
        objectives=objectives,
        meta=raw,
        landmass=_compute_landmass(tiles, w, h),
    )


def load_scenario(map_path: str, armory_path: str) -> Scenario:
    armory = load_armory(armory_path)
    md = load_map(map_path)
    raw = md.meta

    units: list[Unit] = []
    deployed: set[tuple[int, int]] = set()
    air_stack: dict[tuple[int, int], list[str]] = {}
    for side_key, entries in raw.get("deployments", {}).items():
        side = _enum(Side, side_key, "deployments")
        for i, e in enumerate(entries):
            where = f"deployments.{side_key}[{i}]"
            tid = _req(e, "template", where)
            if tid not in armory.templates:
                raise LoadError(f"{where}: 未定义的兵种模板 '{tid}'")
            x, y = int(_req(e, "x", where)), int(_req(e, "y", where))
            if not md.in_bounds(x, y):
                raise LoadError(f"{where}: 坐标越界 ({x},{y})")
            tpl = armory.templates[tid]
            if (x, y) in deployed:
                # 与部署规则保持一致：航空兵可停驻同一机场（停机坪容量 4 架）
                tile = md.tiles[(x, y)]
                same_air = (tpl.domain is Domain.AIR
                            and tile.facility is Facility.AIRBASE
                            and tile.owner is side and len(air_stack.get((x, y), ())) < 4)
                if not same_air:
                    raise LoadError(f"{where}: 部署位置冲突 ({x},{y})，同格不可叠加部署")
            if tpl.domain in (Domain.LAND, Domain.INFRA) and md.tiles[(x, y)].terrain is Terrain.WATER:
                raise LoadError(f"{where}: {tpl.name} 为陆上单位，不可部署于海域 ({x},{y})")
            if tpl.domain is Domain.AIR:
                air_stack.setdefault((x, y), []).append(tid)
            deployed.add((x, y))
            u = Unit(template=tpl, side=side, x=x, y=y)
            if "name" in e:
                u.name = e["name"]
            if "altitude" in e:
                u.altitude = _enum(Altitude, e["altitude"], where + ".altitude")
            units.append(u)

    return Scenario(
        map=md, armory=armory, units=units,
        turn_limit=int(raw.get("turn_limit", 20)),
        side_blue_objectives=[o for o in md.objectives if o.side is Side.BLUE],
        raw=raw,
    )


def list_maps(directory: str) -> list[str]:
    if not os.path.isdir(directory):
        return []
    return sorted(
        os.path.join(directory, f)
        for f in os.listdir(directory)
        if f.lower().endswith(".json")
    )


__all__ = [
    "LoadError", "Armory", "MapData", "Scenario",
    "load_armory", "load_map", "load_scenario", "list_maps",
]
