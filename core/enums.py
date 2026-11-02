"""枚举与常量定义。

方块兵棋 · Block Wargame
所有枚举继承 StrEnum 基类，便于 JSON 序列化与字典查找。
"""
from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """字符串枚举：既可与原始字符串互操作，也可直接 JSON 序列化。"""

    __str__ = str.__str__

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{type(self).__name__}.{self.name}>"


# --------------------------------------------------------------------------- #
# 阵营
# --------------------------------------------------------------------------- #
class Side(StrEnum):
    BLUE = "BLUE"
    RED = "RED"
    NEUTRAL = "NEUTRAL"

    @property
    def label(self) -> str:
        return {"BLUE": "蓝方", "RED": "红方", "NEUTRAL": "中立"}[self.value]

    @property
    def foe(self) -> "Side":
        return Side.RED if self is Side.BLUE else Side.BLUE


# --------------------------------------------------------------------------- #
# 作战域
# --------------------------------------------------------------------------- #
class Domain(StrEnum):
    LAND = "land"       # 地面部队
    AIR = "air"         # 空中平台
    SEA = "sea"         # 水面舰艇
    SUB = "sub"         # 潜艇
    INFRA = "infra"     # 固定设施（雷达站、机场、指挥部）
    SPACE = "space"     # 天基系统

    @property
    def label(self) -> str:
        return {
            "land": "陆上", "air": "空中", "sea": "水面",
            "sub": "水下", "infra": "设施", "space": "天基",
        }[self.value]


class Altitude(StrEnum):
    """空中平台的高度层——低空突防可压缩敌方雷达探测距离。"""

    GROUND = "ground"
    LOW = "low"
    MED = "med"
    HIGH = "high"

    @property
    def label(self) -> str:
        return {"ground": "地面", "low": "低空", "med": "中空", "high": "高空"}[self.value]

    @property
    def radar_factor(self) -> float:
        """该高度层被雷达探测的距离系数（越低越难被发现）。"""
        return {"ground": 1.0, "low": 0.62, "med": 0.85, "high": 1.18}[self.value]


# --------------------------------------------------------------------------- #
# 地形
# --------------------------------------------------------------------------- #
class Terrain(StrEnum):
    WATER = "water"
    COAST = "coast"        # 滩涂 / 两栖登陆地带
    PLAIN = "plain"
    FOREST = "forest"
    HILLS = "hills"
    MOUNTAIN = "mountain"
    URBAN = "urban"
    DESERT = "desert"
    MARSH = "marsh"

    @property
    def label(self) -> str:
        return {
            "water": "海域", "coast": "滩涂", "plain": "平原", "forest": "林地",
            "hills": "丘陵", "mountain": "山地", "urban": "城区",
            "desert": "荒漠", "marsh": "沼泽",
        }[self.value]

    @property
    def move_cost(self) -> float:
        """地面单位进入该地形的机动点消耗。"""
        return {
            "water": 99.0, "coast": 2.0, "plain": 1.0, "forest": 2.0,
            "hills": 2.0, "mountain": 3.5, "urban": 1.5,
            "desert": 1.2, "marsh": 2.5,
        }[self.value]

    @property
    def cover(self) -> float:
        """地形对地面目标的遮蔽系数（降低被探测距离）。"""
        return {
            "water": 1.0, "coast": 0.95, "plain": 1.0, "forest": 0.72,
            "hills": 0.8, "mountain": 0.65, "urban": 0.6,
            "desert": 1.0, "marsh": 0.85,
        }[self.value]

    @property
    def armor_bonus(self) -> float:
        """地形提供的防护加成（降低被命中后的伤害）。"""
        return {
            "water": 0.0, "coast": 0.0, "plain": 0.0, "forest": 0.12,
            "hills": 0.18, "mountain": 0.25, "urban": 0.30,
            "desert": 0.02, "marsh": 0.05,
        }[self.value]


TERRAIN_CHARS: dict[str, Terrain] = {
    "w": Terrain.WATER, "c": Terrain.COAST, "p": Terrain.PLAIN,
    "f": Terrain.FOREST, "h": Terrain.HILLS, "m": Terrain.MOUNTAIN,
    "u": Terrain.URBAN, "d": Terrain.DESERT, "s": Terrain.MARSH,
}


# --------------------------------------------------------------------------- #
# 探测等级
# --------------------------------------------------------------------------- #
class Detection(StrEnum):
    NONE = "none"              # 完全未知
    CONTACT = "contact"        # 有接触（仅知道该格存在目标）
    CLASSIFIED = "classified"  # 已识别（知道兵种与阵营）
    TRACKED = "tracked"        # 已跟踪（可装订射击诸元）

    @property
    def level(self) -> int:
        return {"none": 0, "contact": 1, "classified": 2, "tracked": 3}[self.value]

    @property
    def label(self) -> str:
        return {"none": "未发现", "contact": "接触", "classified": "识别", "tracked": "跟踪"}[self.value]


# --------------------------------------------------------------------------- #
# 武器
# --------------------------------------------------------------------------- #
class WeaponKind(StrEnum):
    GUN = "gun"                 # 直瞄火炮 / 舰炮
    ARTILLERY = "artillery"     # 间瞄火炮
    MLRS = "mlrs"               # 多管火箭炮
    ATGM = "atgm"               # 反坦克导弹
    SAM = "sam"                 # 防空导弹
    AAM = "aam"                 # 空空导弹
    ASM = "asm"                 # 反舰导弹
    LAND_ATTACK = "land_attack" # 对地导弹（巡航 / 弹道 / 高超）
    ARM = "arm"                 # 反辐射导弹
    TORPEDO = "torpedo"
    BOMB = "bomb"
    DEPTH_CHARGE = "depth_charge"
    CIWS = "ciws"               # 近防武器系统：专门拦截来袭弹药

    @property
    def label(self) -> str:
        return {
            "gun": "直瞄火炮", "artillery": "间瞄火力", "mlrs": "火箭炮",
            "atgm": "反坦克导弹", "sam": "防空导弹", "aam": "空空导弹",
            "asm": "反舰导弹", "land_attack": "对地打击", "arm": "反辐射导弹",
            "torpedo": "鱼雷", "bomb": "航空炸弹", "depth_charge": "深水炸弹",
            "ciws": "近防拦截",
        }[self.value]

    @property
    def is_missile(self) -> bool:
        return self.value in {"atgm", "sam", "aam", "asm", "land_attack", "arm", "torpedo"}


class Guidance(StrEnum):
    OPTICAL = "optical"
    IR = "ir"
    RADAR_SEMI = "radar_semi"       # 半主动雷达（需照射）
    RADAR_ACTIVE = "radar_active"   # 主动雷达导引头
    GPS_INS = "gps_ins"             # 卫星/惯导
    ANTI_RADIATION = "anti_radiation"
    PASSIVE_RF = "passive_rf"
    LASER = "laser"
    WIRE = "wire"
    SONAR = "sonar"

    @property
    def label(self) -> str:
        return {
            "optical": "光学", "ir": "红外", "radar_semi": "半主动雷达",
            "radar_active": "主动雷达", "gps_ins": "卫星/惯导",
            "anti_radiation": "被动雷达寻的", "passive_rf": "射频被动",
            "laser": "激光半主动", "wire": "有线制导", "sonar": "声呐",
        }[self.value]

    @property
    def vulnerable_to_ew(self) -> bool:
        """是否受电子干扰影响。"""
        return self.value in {"radar_semi", "radar_active", "gps_ins", "anti_radiation", "passive_rf"}


class SensorKind(StrEnum):
    RADAR = "radar"
    ESM = "esm"          # 电子支援侦察（被动，可远距发现辐射源）
    IR = "ir"
    OPTICAL = "optical"
    SONAR = "sonar"
    SAT = "sat"          # 天基侦察

    @property
    def label(self) -> str:
        return {
            "radar": "雷达", "esm": "电子侦察", "ir": "红外",
            "optical": "光学", "sonar": "声呐", "sat": "天基侦察",
        }[self.value]

    @property
    def is_active(self) -> bool:
        """主动辐射传感器——开机即暴露自身。"""
        return self.value == "radar"


class UnitClass(StrEnum):
    """兵种分类，仅用于图标与文案。"""

    MBT = "mbt"
    IFV = "ifv"
    INFANTRY = "infantry"
    ARTY = "arty"
    MLRS = "mlrs"
    SAM = "sam"
    AAA = "aaa"
    EW = "ew"
    RADAR = "radar"
    HQ = "hq"
    DESTROYER = "destroyer"
    FRIGATE = "frigate"
    CARRIER = "carrier"
    SUB = "sub"
    FIGHTER = "fighter"
    BOMBER = "bomber"
    AEW = "aew"
    UAV = "uav"
    TANKER = "tanker"
    SATELLITE = "satellite"
    ENGINEER = "engineer"
    HELI = "heli"              # 直升机（通用）
    ATTACK_HELI = "attack_heli"   # 武装直升机
    TRANSPORT = "transport"    # 运输机 / 运输直升机
    AMPHIB = "amphib"          # 两栖支援舰

    @property
    def label(self) -> str:
        return {
            "mbt": "主战坦克", "ifv": "步战车", "infantry": "步兵",
            "arty": "自行火炮", "mlrs": "火箭炮", "sam": "防空导弹",
            "aaa": "高炮", "ew": "电子战", "radar": "雷达站", "hq": "指挥所",
            "destroyer": "驱逐舰", "frigate": "护卫舰", "carrier": "航母",
            "sub": "潜艇", "fighter": "战斗机", "bomber": "轰炸机",
            "aew": "预警机", "uav": "无人机", "tanker": "加油机",
            "satellite": "卫星", "engineer": "工兵",
            "heli": "直升机", "attack_heli": "武装直升机",
            "transport": "运输机", "amphib": "两栖支援舰",
        }[self.value]


class Facility(StrEnum):
    """地块上附着的固定设施。"""

    NONE = "none"
    AIRBASE = "airbase"
    PORT = "port"
    RADAR_SITE = "radar_site"
    DEPOT = "depot"
    SAM_SITE = "sam_site"
    HQ = "hq"
    CITY = "city"

    @property
    def label(self) -> str:
        return {
            "none": "", "airbase": "机场", "port": "港口", "radar_site": "雷达站",
            "depot": "后勤枢纽", "sam_site": "防空阵地", "hq": "指挥部", "city": "城市",
        }[self.value]


# --------------------------------------------------------------------------- #
# 战场环境（v1.2）
# --------------------------------------------------------------------------- #
class TimeOfDay(StrEnum):
    """时段。昼/夜改变光学与红外的相对优势。"""

    DAWN = "dawn"      # 拂晓
    DAY = "day"        # 白天
    DUSK = "dusk"      # 黄昏
    NIGHT = "night"    # 夜间

    @property
    def label(self) -> str:
        return {"dawn": "拂晓", "day": "白天", "dusk": "黄昏", "night": "夜间"}[self.value]


class Weather(StrEnum):
    """天候。削弱雷达与光电侦察；不改变红外。"""

    CLEAR = "clear"
    RAIN = "rain"
    FOG = "fog"
    SNOW = "snow"

    @property
    def label(self) -> str:
        return {"clear": "晴", "rain": "雨", "fog": "雾", "snow": "雪"}[self.value]


class JamBand(StrEnum):
    """电子干扰频段。分频段生效后，干扰不再是单一乘数。"""

    RADAR = "radar"      # 雷达探测频段
    COMM = "comm"        # 通信/数据链频段
    NAV = "nav"          # 卫星导航频段

    @property
    def label(self) -> str:
        return {"radar": "雷达", "comm": "通信", "nav": "导航"}[self.value]


class Echelon(StrEnum):
    """部队层级。连级无指挥权，营级可协调，旅级可跨单位下达指令。"""

    PLATOON = "platoon"   # 连
    COMPANY = "company"   # 营
    BRIGADE = "brigade"   # 旅

    @property
    def label(self) -> str:
        return {"platoon": "连级", "company": "营级", "brigade": "旅级"}[self.value]

    @property
    def rank(self) -> int:
        return {"platoon": 0, "company": 1, "brigade": 2}[self.value]


class OrderKind(StrEnum):
    """指挥指令类型。"""

    FOCUS_FIRE = "focus_fire"        # 集中火力：指定目标，全 subordinate 优先打它
    PRIORITY_TARGET = "priority"     # 优先目标：优先打击某类目标（如防空阵地）
    HOLD = "hold"                    # 固守：不得脱离指挥所控制区
    ASSAULT = "assault"              # 强攻：集中力量冲击指定方向

    @property
    def label(self) -> str:
        return {
            "focus_fire": "集中火力",
            "priority": "优先目标",
            "hold": "固守",
            "assault": "强攻",
        }[self.value]


class ObjectiveKind(StrEnum):
    CAPTURE = "capture"     # 占领指定格
    HOLD = "hold"           # 守住指定格
    DESTROY = "destroy"     # 歼灭指定数量敌军
    SURVIVE = "survive"     # 存活至回合结束


__all__ = [
    "StrEnum", "Side", "Domain", "Altitude", "Terrain", "TERRAIN_CHARS",
    "Detection", "WeaponKind", "Guidance", "SensorKind", "UnitClass",
    "Facility", "ObjectiveKind",
    "TimeOfDay", "Weather", "JamBand", "Echelon", "OrderKind",
]
