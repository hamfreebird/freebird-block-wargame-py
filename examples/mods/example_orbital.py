"""示例模组：近地轨道支援（v2.0 模组接口演示）。

这个模组演示了模组接口的三项能力：

1. **注册新武器**——机载激光吊舱（近程高伤、弹仓有限）；
2. **注册新兵种**——概念激光攻击机（携带激光吊舱）；
3. **订阅规则钩子**——每回合给概念机补充能量，以及激光武器的
   穿透加成。

模组放在**用户数据目录**的 ``mods/`` 下即可启用（每个 ``.py``
即一个模组）；安装目录的 ``data/mods/`` 里的模组也会自动加载。

本文件是**示例**，放在 ``examples/mods/`` 下不会自动生效——
想试用就把它复制到用户数据目录的 ``mods/`` 里。

模组文件只需导出一个 ``register(api)`` 函数。``api`` 上可用的方法：

===================== ==================================================
``api.add_weapon(d)`` 注册一件武器（字段与武器库 JSON 一致）
``api.add_unit(d)``   注册一个兵种（字段与武器库 JSON 一致）
``api.on(evt, fn)``   订阅规则钩子
``api.log(msg)``      在加载日志里写一行
===================== ==================================================

可用钩子：

============================ ==================================
``before_fire``             ``(state, attacker, target, weapon)``
``after_fire``              ``(state, attacker, target, weapon, result)``
``modify_damage``           ``(state, attacker, target, weapon, damage)``
``turn_start``              ``(state, turn)``
``unit_destroyed``          ``(state, unit)``
============================ ==================================
"""
from __future__ import annotations

#: 本模组注册的兵种 id——钩子里靠它识别「自己的」单位
CONCEPT_UNIT_ID = "concept_laser"


def register(api) -> None:
    """模组入口：把内容与钩子注册进游戏。"""

    # ------------------------------------------------------------------ #
    # 1. 新武器：机载激光吊舱
    # ------------------------------------------------------------------ #
    api.add_weapon({
        "id": "orbital_laser",
        "name": "机载激光吊舱",
        "kind": "atgm",
        "range_min": 0.3,
        "range_max": 3.2,
        "pk": 0.72,
        "damage": 140,
        "ammo": 4,
        "guidance": "optical",
        "targets": ["land", "infra", "sea"],
        "speed": 12.0,
        "real_range_km": "60 km",
        "max_salvo": 2,
    })

    # ------------------------------------------------------------------ #
    # 2. 新兵种：概念激光攻击机
    # ------------------------------------------------------------------ #
    api.add_unit({
        "id": CONCEPT_UNIT_ID,
        "name": "概念激光攻击机",
        "short": "LASER",
        "class": "uav",
        "domain": "air",
        "hp": 40,
        "mp": 7.0,
        "armor": 0.0,
        "stealth": 0.45,
        "evasion": 0.12,
        "max_altitude": "low",
        "sensors": ["optical_uav", "radar_apg81"],
        "weapons": [{"ref": "orbital_laser", "ammo": 6}],
        "cost": 340,
        "notes": "模组示例：以轨道激光为设计蓝本的无人攻击平台",
        "sortie_shots": 2,
        "echelon": "squadron",
    })

    # ------------------------------------------------------------------ #
    # 3. 钩子
    # ------------------------------------------------------------------ #
    api.on("turn_start", _recharge_energy)
    api.on("modify_damage", _laser_penetration)

    api.log("近地轨道支援：已注册「机载激光吊舱」与「概念激光攻击机」")


def _recharge_energy(state, turn: int) -> None:
    """每回合为概念机补充能量。

    设定上概念机的能量来自外置轨道平台，因此不像普通弹药那样稀缺。
    """
    for u in state.units:
        if not u.alive or u.template.id != CONCEPT_UNIT_ID:
            continue
        wid = "orbital_laser"
        cap = 0
        for w in u.template.weapons:
            if w.ref == wid:
                cap = w.ammo
                break
        if cap and u.ammo.get(wid, 0) < cap:
            u.ammo[wid] = cap
            state.log_message(u.side,
                             f"{u.name} 轨道平台补能完毕，激光挂弹恢复至 {cap} 发",
                             "system")


def _laser_penetration(state, attacker, target, weapon, damage):
    """激光对轻装甲目标伤害加成。

    返回新的伤害值；返回 ``None`` 表示不修改。
    """
    if weapon is None or getattr(weapon, "id", "") != "orbital_laser":
        return None
    armor = float(getattr(target, "template", None).armor) if target else 0.0
    if armor >= 0.3:
        return None
    # 轻装甲目标：激光直接烧穿，伤害 ×1.35
    return float(damage) * 1.35
