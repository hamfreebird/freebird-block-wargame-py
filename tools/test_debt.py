"""v3.3.1 技术债守卫：四条「自动失效」机制的回归测试。

这四条债原本都是**约定**，不是机制：

1. 未传 rng 的随机判定会静默退回全局 ``random``（不受对局种子控制，
   且失配时表现为「同种子两次跑出不同结果」，极难查）；
2. ``unit.x/y`` 改了必须手动调 ``invalidate_pos_index()``；
3. 目标点 / 工事 / 设施变了必须手动递增 ``state.map_epoch``；
4. 探测缓存的指纹没覆盖模板（模组换雷达后沿用旧结论）。

v3.3.1 把它们都改成了「写入即留痕」：``core.model`` 的 ``pos_epoch()`` /
``tile_epoch()`` 会在相关字段被写入时递增，索引访问时比对版本号自动重建；
``core.dice`` 给兜底加了计数与严格模式；探测指纹补上了模板。

这个脚本守的就是这四条——**如果哪天有人把机制改回约定，这里会红。**
断言都刻意「不调用任何失效函数」，因为那正是被测的东西。

用法：python tools/test_debt.py
"""
from __future__ import annotations

import os
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("BLOCKWARGAME_NO_AUDIO", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pygame  # noqa: E402

from core import detect, dice  # noqa: E402
from core.engine import GameState  # noqa: E402
from core.enums import Facility, Side  # noqa: E402
from core.loader import load_armory, load_scenario  # noqa: E402
from core.model import Unit, pos_epoch, tile_epoch  # noqa: E402
from core.rng import GameRng  # noqa: E402
from ui.mapview import MapView  # noqa: E402

ARMORY = os.path.join(ROOT, "data", "units", "armory.json")
ARM = load_armory(ARMORY)
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILS.append(msg)


def new_state(map_name: str = "training_ground") -> GameState:
    path = os.path.join(ROOT, "data", "maps", f"{map_name}.json")
    return GameState(load_scenario(path, ARMORY), Side.BLUE)


def first_empty_tile(st: GameState) -> tuple[int, int]:
    """找一个没有任何单位的格子。"""
    for y in range(st.map.height):
        for x in range(st.map.width):
            if not st.units_at(x, y):
                return (x, y)
    raise AssertionError("地图上找不到空格——想定异常")


# --------------------------------------------------------------------------- #
# [1] 骰子兜底：可观测 + 可拒绝
# --------------------------------------------------------------------------- #
def test_dice_fallback() -> None:
    print("[1] 随机判定：兜底可观测、可拒绝")
    dice.set_strict(False)
    dice.reset_counters()

    dice.roll(0.5)
    check(dice.FALLBACK_CALLS == 1, "不传 rng 的 roll 会被计数（不再静默）")
    dice.jitter(10.0, 0.1)
    check(dice.FALLBACK_CALLS == 2, "不传 rng 的 jitter 同样计数")

    dice.set_strict(True)
    try:
        dice.roll(0.5)
        check(False, "严格模式下未传 rng 应当抛错")
    except RuntimeError as exc:
        check("rng" in str(exc), "严格模式抛 RuntimeError 并点明缺 rng")
    check(dice.FALLBACK_CALLS == 2, "被拒绝的调用不计数（不是静默兜底）")
    dice.set_strict(False)

    # 传了 rng 就完全不走兜底，且同种子逐项一致
    dice.reset_counters()
    r1, r2 = GameRng(101), GameRng(101)
    a = [dice.roll(0.5, r1) for _ in range(20)]
    b = [dice.roll(0.5, r2) for _ in range(20)]
    check(dice.FALLBACK_CALLS == 0, "传了 rng 的调用不计入兜底")
    check(a == b, "同种子的随机判定逐项一致")


# --------------------------------------------------------------------------- #
# [2] 位置索引：改坐标即自校验
# --------------------------------------------------------------------------- #
def test_pos_index() -> None:
    print("[2] 位置索引：改坐标 / 置摧毁后自动重建")
    st = new_state()
    u = next(u for u in st.units if u.alive)
    old = (u.x, u.y)
    st.units_at(*old)                     # 先把索引建起来
    epoch_before = pos_epoch()

    spot = first_empty_tile(st)
    u.x, u.y = spot                       # 故意不调用 invalidate_pos_index()
    check(pos_epoch() > epoch_before, "写坐标会让位置版本号递增")
    check(u in st.units_at(*spot), "改坐标后索引自动重建（未调 invalidate）")
    check(all(v is not u for v in st.units_at(*old)), "旧格不再返回这个单位")

    u.destroyed = True
    check(all(v is not u for v in st.units_at(*spot)), "直接置 destroyed 后索引不再返回它")

    # 版本号是「写入即留痕」，所以凭空造一个单位也会让它变
    before = pos_epoch()
    next(x for x in st.units).hp += 0.0   # 不相关字段不应影响位置索引
    check(pos_epoch() == before, "无关字段的写入不会误触发位置索引重建")

    # 增援 / 诱饵 / 想定事件都是「先造好再 append」的：位置没变过，
    # 版本号抓不到，索引得靠**单位数量**判废
    st3 = new_state()
    spot3 = first_empty_tile(st3)         # 这一次查询就把索引建好了
    late = Unit(template=next(iter(ARM.templates.values())), side=Side.RED,
                x=spot3[0], y=spot3[1], uid=st3.alloc_uid())
    st3.units.append(late)                # 只 append，不动坐标
    check(late in st3.units_at(*spot3), "追加进 units 的单位不必改坐标也能被索引发现")


# --------------------------------------------------------------------------- #
# [3] 地块索引：改目标点 / 工事 / 设施即自校验
# --------------------------------------------------------------------------- #
def test_tile_index() -> None:
    print("[3] 地块索引：改目标点 / 工事 / 设施后自动重建")
    st = new_state()
    view = MapView(pygame.Rect(0, 0, 800, 600), st)
    obj0 = len(view._objective_tiles())
    fort0 = len(view._fort_tiles())
    fac0 = len(view._facility_tiles())

    spot = None
    for y in range(st.map.height):
        for x in range(st.map.width):
            t = st.map.at(x, y)
            if not t.objective and t.fort == 0 and t.facility is Facility.NONE:
                spot = t
                break
        if spot is not None:
            break
    if spot is None:
        raise AssertionError("地图上找不到干净地块——想定异常")

    epoch_before = st.map_epoch
    spot.objective = True
    spot.objective_name = "守卫测试点"
    check(len(view._objective_tiles()) == obj0 + 1,
          "直接改 tile.objective 后索引自动重建")
    check(st.map_epoch == epoch_before, "……而且没有依赖 map_epoch（约定已废）")

    spot.fort = 2
    spot.fort_owner = Side.RED
    check(len(view._fort_tiles()) == fort0 + 1, "工事归属写入后索引自动重建")

    spot.facility = Facility.AIRBASE
    check(len(view._facility_tiles()) == fac0 + 1, "设施类型写入后索引自动重建")

    # 拿掉目标点也要能被发现（只增不减是最容易漏的方向）
    spot.objective = False
    check(len(view._objective_tiles()) == obj0, "目标点被拿掉后索引同样重建")
    check(tile_epoch() > 0, "地块版本号可用")


# --------------------------------------------------------------------------- #
# [4] 探测指纹：覆盖模板
# --------------------------------------------------------------------------- #
def test_detect_template() -> None:
    print("[4] 探测缓存：模板被替换后世代号递增")
    st = new_state()
    st.refresh_detection()
    target = next(u for u in st.units
                  if u.alive and u.template.sensors and not u.is_decoy)
    fp0 = detect._unit_fingerprint(target)
    gen0 = target.detect_gen

    other = ARM.templates["j20"]
    if other is target.template:
        other = ARM.templates["f16c"]
    check(other.stealth != target.template.stealth,
          "换用的模板确实有不同的隐身系数（否则这条断言无意义）")

    target.template = other              # 模组常见做法：整套换模板
    check(detect._unit_fingerprint(target) != fp0,
          "模板替换后单位指纹改变")
    detect._sync_generations(st)
    check(target.detect_gen > gen0, "模板替换后探测世代号递增（缓存自动作废）")


def main() -> int:
    print("=== 技术债守卫自检（v3.3.1）===")
    test_dice_fallback()
    test_pos_index()
    test_tile_index()
    test_detect_template()
    print()
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败：")
        for f in FAILS:
            print("   -", f)
        return 1
    print("✓ 四条自动失效机制均在位")
    return 0


if __name__ == "__main__":
    sys.exit(main())
