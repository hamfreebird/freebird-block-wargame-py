"""为 armory 补充美方特色兵种。

大型想定（128×128「台海对峙」）需要双方各有 20 种以上兵种。
美方原有 18 种专属兵种，缺武装直升机、反潜直升机、运输机、
舰载防空与两栖支援舰——这些恰好是「空中支援地面」战术的关键件，
与 v1.5 的同格共存机制相呼应。

用法::

    python tools/extend_armory.py        # 幂等：已存在则跳过
"""
from __future__ import annotations

import json
import os
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARMORY = os.path.join(ROOT, "data", "units", "armory.json")

# --------------------------------------------------------------------------- #
# 新增武器
# --------------------------------------------------------------------------- #
NEW_WEAPONS: list[dict] = [
    {
        "id": "agm114", "name": "AGM-114B 地狱火反坦克导弹", "kind": "atgm",
        "range_min": 0.5, "range_max": 8.0, "pk": 0.82, "damage": 95, "ammo": 8,
        "guidance": "laser", "targets": ["land", "infra"], "needs_los": False,
        "speed": 12.0, "real_range_km": "8 km（激光驾束）",
        "note": "AH-64D 的主火力，可猎杀装甲集群", "max_salvo": 2,
    },
    {
        "id": "hyd70", "name": "M240 70mm 火箭弹巢", "kind": "mlrs",
        "range_min": 0.3, "range_max": 4.5, "pk": 0.6, "damage": 40, "ammo": 38,
        "guidance": "optical", "targets": ["land", "infra"], "needs_los": False,
        "speed": 9.0, "real_range_km": "4.5 km（火箭巢）",
        "note": "覆盖面广的压制火力，对阵地与工事有效", "max_salvo": 4,
    },
    {
        "id": "mk_torpedo", "name": "Mk 54 鱼雷", "kind": "torpedo",
        "range_min": 0.2, "range_max": 3.0, "pk": 0.75, "damage": 110, "ammo": 4,
        "guidance": "sonar", "targets": ["sea", "sub"], "needs_los": False,
        "speed": 5.0, "real_range_km": "3 km（声呐制导）",
        "note": "反潜直升机的主要杀伤手段", "max_salvo": 2,
    },
    {
        "id": "essm", "name": "SM-6 ESSM 中程防空导弹", "kind": "sam",
        "range_min": 0.4, "range_max": 16.0, "pk": 0.8, "damage": 70, "ammo": 16,
        "guidance": "radar_active", "targets": ["air"], "needs_los": False,
        "speed": 22.0, "real_range_km": "160 km（实际射程远超图上格数）",
        "note": "既打巡航导弹也能打弹道导弹，兼作反导", "max_salvo": 4,
    },
    {
        "id": "harpoon", "name": "AGM-84 鱼叉反舰导弹", "kind": "land_attack",
        "range_min": 0.5, "range_max": 9.0, "pk": 0.78, "damage": 105, "ammo": 4,
        "guidance": "radar_active", "targets": ["sea"], "needs_los": False,
        "speed": 14.0, "real_range_km": "240 km（舰载 / 直升机 / 无人机）",
        "note": "MQ-9 挂载鱼叉即成反舰平台", "max_salvo": 2,
    },
    {
        "id": "shorad_gun", "name": "M242 25mm 链式炮", "kind": "gun",
        "range_min": 0.2, "range_max": 1.6, "pk": 0.45, "damage": 22, "ammo": 500,
        "guidance": "optical", "targets": ["land", "infra", "air"], "needs_los": True,
        "speed": 8.0, "real_range_km": "2 km（有效 1.5 km）",
        "note": "近程防空反步兵的神枪，泼洒弹幕", "max_salvo": 6,
    },
]

# --------------------------------------------------------------------------- #
# 新增单位（美方）
# --------------------------------------------------------------------------- #
NEW_UNITS: list[dict] = [
    {
        "id": "ah64", "name": "AH-64D 武装直升机", "short": "AH-64D",
        "class": "attack_heli", "domain": "air", "hp": 70, "mp": 4.0,
        "armor": 0.25, "stealth": 0.18, "evasion": 0.34,
        "sensors": ["ir_pod", "ir_irst", "radar_apg81"],
        "weapons": [{"ref": "agm114", "ammo": 8}, {"ref": "hyd70", "ammo": 38}],
        "can_capture": False, "cost": 620,
        "notes": "低空贴地突击，雷达特征小；可与地面部队同格协同",
        "echelon": "company",
    },
    {
        "id": "s70", "name": "MH-60R 海鸦反潜直升机", "short": "MH-60R",
        "class": "heli", "domain": "air", "hp": 60, "mp": 3.8,
        "armor": 0.2, "stealth": 0.2, "evasion": 0.3,
        "sensors": ["sonar_towed", "radar_apg81"],
        "weapons": [{"ref": "mk_torpedo", "ammo": 4}],
        "can_capture": False, "cost": 520,
        "notes": "吊放声呐搜索潜艇，可长期悬停在同一片海域",
        "echelon": "company",
    },
    {
        "id": "ch53", "name": "CH-53K 重型运输直升机", "short": "CH-53K",
        "class": "transport", "domain": "air", "hp": 85, "mp": 3.0,
        "armor": 0.28, "stealth": 0.12, "evasion": 0.2,
        "sensors": ["radar_apg81"],
        "weapons": [],
        "can_capture": False, "cost": 480,
        "notes": "重型运输平台，可把地面部队快速投送到前沿",
        "echelon": "company",
    },
    {
        "id": "f16c", "name": "F-16C Block 40 战斗机", "short": "F-16C",
        "class": "fighter", "domain": "air", "hp": 85, "mp": 4.2,
        "armor": 0.3, "stealth": 0.1, "evasion": 0.4,
        "sensors": ["radar_apg70", "ir_irst"],
        "weapons": [{"ref": "aim9x", "ammo": 6}],
        "can_capture": False, "cost": 460,
        "notes": "多用途战斗机，兼顾制空与对地", "echelon": "squadron",
    },
    {
        "id": "f18e", "name": "F/A-18E 超级大黄蜂", "short": "F/A-18E",
        "class": "fighter", "domain": "air", "hp": 90, "mp": 4.0,
        "armor": 0.32, "stealth": 0.08, "evasion": 0.38,
        "sensors": ["radar_apg81", "ir_pod"],
        "weapons": [{"ref": "aim9x", "ammo": 6}, {"ref": "agm88", "ammo": 2}],
        "can_capture": False, "cost": 500,
        "notes": "兼具对空与反辐射能力，可猎杀开机雷达的防空系统",
        "echelon": "squadron",
    },
    {
        "id": "c130", "name": "C-130J 大力神运输机", "short": "C-130J",
        "class": "transport", "domain": "air", "hp": 95, "mp": 2.8,
        "armor": 0.3, "stealth": 0.08, "evasion": 0.22,
        "sensors": ["radar_apg81"],
        "weapons": [],
        "can_capture": False, "cost": 540,
        "notes": "战略空运平台，把战役纵深的部队投送到战场",
        "echelon": "squadron",
    },
    {
        "id": "llsn", "name": "圣安东尼奥级两栖支援舰", "short": "LSS",
        "class": "amphib", "domain": "sea", "hp": 150, "mp": 2.6,
        "armor": 0.34, "stealth": 0.22, "evasion": 0.18,
        "sensors": ["radar_aegis", "sonar_hull"],
        "weapons": [{"ref": "essm", "ammo": 12}, {"ref": "shorad_gun", "ammo": 400}],
        "can_capture": False, "cost": 620,
        "notes": "两栖支援舰，兼有防空与近程火力，还能装载登陆部队",
        "echelon": "flotilla",
    },
    {
        "id": "m142_pz", "name": "M142 HIMARS 远程炮兵连", "short": "HIMARS",
        "class": "mlrs", "domain": "land", "hp": 78, "mp": 3.0,
        "armor": 0.18, "stealth": 0.12, "evasion": 0.2,
        "sensors": ["ir_thermal"],
        "weapons": [{"ref": "gmmlrs", "ammo": 6}, {"ref": "prsm", "ammo": 2}],
        "can_capture": True, "cost": 300,
        "notes": "模块化装填，可换 GMLRS 精确制导或 ATACMS 弹道弹",
        "echelon": "battalion",
    },
]


def main() -> int:
    with open(ARMORY, "r", encoding="utf-8") as f:
        doc = json.load(f)

    have_w = {w["id"] for w in doc["weapons"]}
    have_u = {u["id"] for u in doc["units"]}

    added_w = 0
    for w in NEW_WEAPONS:
        if w["id"] in have_w:
            continue
        doc["weapons"].append(w)
        added_w += 1

    added_u = 0
    for u in NEW_UNITS:
        if u["id"] in have_u:
            continue
        doc["units"].append(u)
        added_u += 1

    # MQ-9 增加鱼叉反舰能力（无人机挂载反舰导弹）
    for u in doc["units"]:
        if u["id"] == "mq9" and not any(
                w["ref"] == "harpoon" for w in u.get("weapons", [])):
            u["weapons"].append({"ref": "harpoon", "ammo": 2})
            u["notes"] = "察打一体无人机；挂载鱼叉后具备反舰能力"

    tmp = ARMORY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ARMORY)

    print(f"新增武器 {added_w} 个、单位 {added_u} 个")
    print(f"武器库现有 {len(doc['weapons'])} 武器 / {len(doc['units'])} 兵种")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
