"""示例模组：机载激光吊舱。

演示 v3.0 官方 SDK 的完整用法。把它整个目录复制到
用户数据目录的 ``mods/`` 下就能用：

    <用户目录>/mods/example_laser/

模组不需要改游戏任何一行代码——所有内容都通过
``api`` 对象注册。
"""
from __future__ import annotations

#: 本模组的 ID——词条命名空间与 manifest 里的 id 一致
MOD_ID = "example_laser"


def register(api):
    """注册模组内容。

    ``api`` 是 :class:`core.mods.ModAPI` 实例——它只提供
    **公开 API**（见 ``core.mods.PUBLIC_API``），以 ``_`` 开头的方法
    是内部实现，随时可能变，不要用。
    """

    # -- 1. 传感器：激光照射器（近距高精度的"眼睛"）------------------
    api.add_sensor({
        "id": "laser_designator",
        "kind": "eo",              # 光电传感器
        "range": 6.0,
        "strength": 1.4,           # 识别精度高于普通光电
        "counter_stealth": 0.3,    # 对隐身目标有一定发现力
        "note": "激光照射器：短距高精度识别",
    })

    # -- 2. 武器：机载激光吊舱 -----------------------------------------
    api.add_weapon({
        "id": "laser_pod",
        "name": "机载激光吊舱",
        "kind": "laser",
        "domain": "air",           # 只能由空中单位挂载
        "min_range": 1.0,
        "range": 5.0,
        "pk": 0.72,                # 激光精度高：基础命中率高
        "damage": 12.0,
        "ammo": 6,                 # 挂载时的默认备弹
        "targets": ["land", "sea", "air"],
        "note": "定向能武器：对无装甲目标极强，对工事效果有限",
    })

    # -- 3. 兵种：概念激光攻击机 ---------------------------------------
    api.add_unit({
        "id": "laser_fighter",
        "name": "概念激光攻击机",
        "short": "激光机",
        "domain": "air",
        "class": "fighter",
        "hp": 90,
        "mp": 6.0,
        "cost": 240,
        "sensors": ["laser_designator"],
        "weapons": [{"ref": "laser_pod", "ammo": 6}],
        "ammo_type": "laser",
        "can_capture": False,
        "traits": ["stealth"],
    })

    # -- 4. 挂载预设：把激光挂到现有机体上 ----------------------------
    # 引用的是**已存在**的武器 id——模组可以给原有机体加新挂载
    api.add_mount({
        "id": "laser_pod_loadout",
        "name": "激光吊舱",
        "domain": "air",
        "desc": "给现有攻击机加装激光吊舱",
        "weapons": [{"ref": "laser_pod", "ammo": 6}],
    })

    # -- 5. 本地化：给模组自己的兵种配多语言名 --------------------------
    api.add_localization("zh", {
        f"{MOD_ID}.laser_fighter": "概念激光攻击机",
        f"{MOD_ID}.laser_pod": "机载激光吊舱",
    })
    api.add_localization("en", {
        f"{MOD_ID}.laser_fighter": "Concept Laser Fighter",
        f"{MOD_ID}.laser_pod": "Airborne Laser Pod",
    })

    # -- 6. 图标：为兵种指定图标（相对模组目录）----------------------
    api.add_icon("laser_fighter", "icons/laser_fighter.png")

    # -- 7. 规则钩子：激光打无装甲目标伤害更高 ------------------------
    def laser_bonus(state, attacker, target, weapon, damage):
        """激光对轻型目标加成，对装甲目标反而减益。"""
        if weapon is None or getattr(weapon, "kind", "") != "laser":
            return None
        cls = getattr(getattr(target, "template", None), "unit_class", None)
        name = getattr(cls, "name", "")
        if name in ("infantry", "light_armor"):
            return damage * 1.4
        if name in ("main_battle_tank", "fortress"):
            return damage * 0.6
        return None

    api.on("modify_damage", laser_bonus)

    # -- 8. 元数据：供其它模组或界面查询 ------------------------------
    api.set_metadata("category", "weapons")
    api.set_metadata("tags", ["laser", "concept"])

    api.log("示例模组已加载：机载激光吊舱 + 概念激光攻击机")
