"""规则层门面：按域拆分后，把各模块的公开符号重新导出。

**为什么保留门面而不是让调用方改 import**：

拆分前本模块 2091 行，探测 / 交战 / 拦截 / 寻路 / 补给五类职责挤在一起。
现在按域拆成九个模块，本文件只做 re-export：

``dice`` 骰子 · ``geo`` 几何与视线 · ``ew`` 电子战 · ``detect`` 探测链 ·
``adn`` 防空网 · ``combat`` 射击解算 · ``facility`` 设施打击 ·
``logistics`` 补给链 · ``pathfind`` 机动寻路

这样做的三个理由：

1. 调用点有五十多处、分散在 core / ui / tools，改 import 的收益远小于风险；
2. 门面给出一份**唯一的规则层名字清单**，``__all__`` 就是契约；
3. 逻辑都在各模块里，门面只有 import——不会重新长出业务代码。

**依赖方向**（箭头指向被依赖者，无环）::

    rules（门面）
      ├── pathfind → facility → combat → adn → detect → ew → geo → dice

``pathfind`` 依赖 ``facility`` 只是因为基地容量常量 ``AIRBASE_CAPACITY``
放在那边——它语义上属于设施，不属于寻路。

**跨模块调用走属性访问**：``ew.jam_strength_at`` / ``geo.has_los`` /
``dice.roll`` 这几处刻意不写成 ``from .ew import jam_strength_at``——
自检脚本要把它们换成确定性桩值，而补丁只能改定义处模块的属性。
"""
from __future__ import annotations

from .dice import (roll, jitter
)
from .geo import (NEIGHBORS_8, LOS_BLOCK_THRESHOLD, AIR_OBSERVER_HEIGHT,
    AIR_TARGET_HEIGHT, ORBITAL_ENGAGEMENT_DISTANCE, dist, dist_units,
    CROSSFIRE_KINDS, CROSSFIRE_DOMAINS, CROSSFIRE_SECTORS,
    CROSSFIRE_RADIUS, bearing_sector, _can_crossfire, crossfire_bonus,
    CLOSE_TRACK_RANGE, RANGE_TOLERANCE, _bresenham, has_los,
    _compute_los, _cache_of
)
from .ew import (DECOY_SIGNATURE, DECOY_LABELS, decoy_signature, jam_strength_at,
    JamField, jam_field, gps_integrity
)
from .detect import (DetectionResult, sensor_effective_range, evaluate_detection,
    detection_level, _detection_level, _env_key, _DIRECT_SCAN_LIMIT,
    DetectionIndex, _build_detection_index, _unit_fingerprint,
    _sync_generations, _candidate_observers, own_track,
    build_detection_map
)
from .adn import (FRAG_PARTIAL_CHANCE, PARTIAL_DAMAGE, MIDCOURSE_WINDOWS,
    BOOST_PK_SCALE, sam_for_intercept, ADNNode, AirDefenseNetwork,
    _channels_of, _adn_role, adn_summary_for,
    resolve_salvo_interception, midcourse_interceptors,
    interception_estimate
)
from .combat import (ShotProfile, salvo_limit, _fire_mission_bonus, shot_profile,
    CounterThreat, counter_threat_profile
)
from .facility import (FACILITY_SIZE, FacilityStrike, facility_strike_profile,
    service_tile, AIRBASE_CAPACITY
)
from .logistics import (SUPPLY_PROFILE, SupplySource, supply_sources, assign_supply,
    supply_source_for, supply_status, facility_service_ok
)
from .pathfind import (can_end_at, tile_move_cost, step_cost, find_path, fallback_moves,
    reachable_tiles, flood_reachable, path_cost, movement_profile,
    field_to_goal, _build_field, MAX_FIELD_LIMIT, NEAR_FIELD_LIMIT,
    _wide_field_limit, path_cost_to, is_reachable
)

# 下面这些是「拆分前就在 rules 命名空间里」的枚举与模型类型。
# 它们本不属于规则运算，但外部（尤其 tools/ 里的自检）已经习惯写成
# ``rules.WeaponKind`` / ``rules.Side``，所以照旧导出——门面的职责就是
# 「拆分前后对外一模一样」。
from . import counter as counter_rules
from .enums import (Detection, Domain, Facility, SensorKind, Side, Terrain,
                    UnitClass, WeaponKind)
from .env import environment_for
from .loader import SUB_CAPACITY
from .model import Tile, Unit, Weapon
from .rng import GameRng

__all__ = [
    "roll", "jitter", "NEIGHBORS_8", "LOS_BLOCK_THRESHOLD",
    "AIR_OBSERVER_HEIGHT", "AIR_TARGET_HEIGHT",
    "ORBITAL_ENGAGEMENT_DISTANCE", "dist", "dist_units",
    "CROSSFIRE_KINDS", "CROSSFIRE_DOMAINS", "CROSSFIRE_SECTORS",
    "CROSSFIRE_RADIUS", "bearing_sector", "crossfire_bonus",
    "CLOSE_TRACK_RANGE", "RANGE_TOLERANCE", "has_los",
    "DECOY_SIGNATURE", "DECOY_LABELS", "decoy_signature",
    "jam_strength_at", "JamField", "jam_field", "gps_integrity",
    "DetectionResult", "sensor_effective_range", "evaluate_detection",
    "detection_level", "DetectionIndex", "own_track",
    "build_detection_map", "FRAG_PARTIAL_CHANCE", "PARTIAL_DAMAGE",
    "MIDCOURSE_WINDOWS", "BOOST_PK_SCALE", "sam_for_intercept",
    "ADNNode", "AirDefenseNetwork", "adn_summary_for",
    "resolve_salvo_interception", "midcourse_interceptors",
    "interception_estimate", "ShotProfile", "salvo_limit",
    "shot_profile", "CounterThreat", "counter_threat_profile",
    "FACILITY_SIZE", "FacilityStrike", "facility_strike_profile",
    "service_tile", "AIRBASE_CAPACITY", "SUPPLY_PROFILE",
    "SupplySource", "supply_sources", "assign_supply",
    "supply_source_for", "supply_status", "facility_service_ok",
    "can_end_at", "tile_move_cost", "step_cost", "find_path",
    "fallback_moves", "reachable_tiles", "flood_reachable", "path_cost",
    "movement_profile", "field_to_goal", "MAX_FIELD_LIMIT",
    "NEAR_FIELD_LIMIT", "path_cost_to", "is_reachable"
]
