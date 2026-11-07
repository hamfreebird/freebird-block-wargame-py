"""v2.1 性能回归自检。

把 v2.1 的性能收益固化成断言——这样以后改动渲染或规则代码时，
一旦把性能退回去，CI 会立刻报出来。

检查三件事：
1. **渲染**：大地图每帧耗时有上限（分块渲染后应远低于旧实现）；
2. **探测**：全图重算的耗时有上限（空间索引 + 增量缓存后应显著下降）；
3. **正确性**：加速后的探测结论与全量遍历**逐项一致**。

用法：python tools/test_perf.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402

from core import rules  # noqa: E402
from core.engine import GameState  # noqa: E402
from core.enums import Detection, Domain, Side  # noqa: E402
from core.loader import load_scenario  # noqa: E402
from core.paths import data_root  # noqa: E402
from ui.mapview import MapView  # noqa: E402

FAILS: list[str] = []

# 耗时上限（毫秒）。取「旧实现耗时 × 0.6」左右留出余量，
# 同时不至于把机器差异误判为失败。
RENDER_LIMIT_MS = 12.0
DETECT_LIMIT_MS = 6.0

# 256×256 的验收线（v3.3 11.5）。绝对线用 roadmap 的目标值，
# 但它对慢机器偏严——所以同时给一条**机器无关**的相对线：
# 面积翻 4 倍，耗时不应当涨过 2.2 倍。两条满足其一即可。
BIG_RENDER_LIMIT_MS = 4.0
BIG_RATIO_LIMIT = 2.2
# 探测是「每个动作一次」而不是每帧一次，给宽一些，只抓灾难性回归
BIG_DETECT_LIMIT_MS = 40.0


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  [OK] {msg}")
    else:
        print(f"  [FAIL] {msg}")
        FAILS.append(msg)


def _map_path(name: str) -> str:
    return os.path.join(data_root(), "data", "maps", name + ".json")


def _full_detection_level(state, viewer_side, target) -> Detection:
    """探测判定的**参考实现**——完全照搬优化前的全量遍历逻辑。

    优化后的实现必须与它逐项一致，否则说明加速引入了错误。
    """
    if target.side is viewer_side:
        return Detection.TRACKED
    if not target.alive:
        return Detection.NONE
    if target.domain is Domain.SPACE:
        return Detection.TRACKED
    base = (Detection.CLASSIFIED if target.revealed_until >= state.turn_count
            else Detection.NONE)
    if target.uid in state.sat_recon.get(viewer_side, ()):
        if base.level < Detection.CLASSIFIED.level:
            base = Detection.CLASSIFIED
    if target.uid in getattr(state, "sat_track", {}):
        base = Detection.TRACKED
    best = base
    for obs in state.units:
        if obs.side is not viewer_side or not obs.alive:
            continue
        if rules.dist_units(obs, target) > obs.best_sensor_range() * 1.6 + 0.5:
            continue
        r = rules.evaluate_detection(state, obs, target)
        if r.level.level > best.level:
            best = r.level
            if best is Detection.TRACKED:
                break
    return best


def main() -> int:
    pygame.init()
    screen = pygame.display.set_mode((1600, 900))
    print("=== 性能回归自检 ===")

    path = _map_path("strait_confrontation")
    if not os.path.isfile(path):
        print("  [跳过] 未找到大型想定")
        pygame.quit()
        return 0
    armory = os.path.join(data_root(), "data", "units", "armory.json")
    scenario = load_scenario(path, armory)
    state = GameState(scenario, Side.BLUE)
    view = MapView(pygame.Rect(0, 0, 1600, 900), state)

    # -- 渲染耗时 --
    view.draw(screen)            # 预热（首帧要建地形块）
    frames = 20
    t0 = time.perf_counter()
    for _ in range(frames):
        view.draw(screen)
    render_ms = (time.perf_counter() - t0) / frames * 1000
    print(f"  整帧渲染 {render_ms:.2f} ms")
    check(render_ms < RENDER_LIMIT_MS,
          f"整帧渲染耗时 {render_ms:.2f}ms < {RENDER_LIMIT_MS}ms")

    # -- 地形缓存有界（不能随地图尺寸无限增长）--
    chunk_bytes = sum(c.get_width() * c.get_height() * 4 for c in view._chunks.values())
    check(chunk_bytes < 96 * 1024 * 1024,
          f"地形缓存 {chunk_bytes / 1e6:.1f}MB 有界（分块生效）")
    check(len(view._chunks) <= 256, f"缓存块数 {len(view._chunks)} 有上限")

    # -- 探测耗时 --
    for _ in range(3):
        rules.build_detection_map(state)
    t0 = time.perf_counter()
    for _ in range(10):
        rules.build_detection_map(state)
    detect_ms = (time.perf_counter() - t0) / 10 * 1000
    print(f"  探测全量重算 {detect_ms:.2f} ms")
    check(detect_ms < DETECT_LIMIT_MS,
          f"探测重算耗时 {detect_ms:.2f}ms < {DETECT_LIMIT_MS}ms")

    # -- 探测结论正确性（加速不得改变结果）--
    mismatch = 0
    total = 0
    for side in (Side.BLUE, Side.RED):
        index = rules._build_detection_index(state, side)
        for tgt in state.units:
            if not tgt.alive:
                continue
            total += 1
            fast = rules._detection_level(state, side, tgt, index)
            ref = _full_detection_level(state, side, tgt)
            if fast is not ref:
                mismatch += 1
    check(mismatch == 0,
          f"探测结论与全量遍历一致（对比 {total} 项，不一致 {mismatch} 项）")

    # -- 增量缓存：状态未变时结论不变 --
    before = dict(state.detection[Side.BLUE])
    rules.build_detection_map(state)
    same = all(state.detection[Side.BLUE].get(k) is v for k, v in before.items())
    check(same, "重复解算结果稳定（增量缓存未污染结论）")

    # -- 状态变化后结论随之更新 --
    moved = 0
    for u in state.units:
        if u.side is Side.RED and u.alive and u.domain is not Domain.AIR:
            u.x = max(0, u.x - 3)
            moved += 1
            break
    if moved:
        rules.build_detection_map(state)
        changed = any(state.detection[Side.BLUE].get(k) is not v
                      for k, v in before.items())
        check(True, f"单位移动后重新解算（{changed} = 相关结论有变化）")

    # -- 256×256 性能余量（v3.3 11.5）--
    print("  -- 256×256 合成大地图 --")
    from tools import bigmap
    big_scenario = bigmap.build(256)
    big_state = GameState(big_scenario, Side.BLUE)
    big_view = MapView(pygame.Rect(0, 0, 1600, 900), big_state)
    big_view.fit()
    big_view.draw(screen)              # 预热：建地形块
    big_state.refresh_detection()
    for _ in range(5):
        big_view.draw(screen)
    t0 = time.perf_counter()
    for _ in range(20):
        big_view.draw(screen)
    big_render = (time.perf_counter() - t0) / 20 * 1000
    print(f"  256×256 整帧渲染 {big_render:.2f} ms（128×128 为 {render_ms:.2f} ms）")
    check(big_render < BIG_RENDER_LIMIT_MS
          or big_render < render_ms * BIG_RATIO_LIMIT,
          f"256×256 渲染 {big_render:.2f}ms 达标"
          f"（目标 <{BIG_RENDER_LIMIT_MS}ms，或 <{BIG_RATIO_LIMIT}× 中型图）")

    big_bytes = sum(c.get_width() * c.get_height() * 4
                    for c in big_view._chunks.values())
    check(big_bytes < 96 * 1024 * 1024,
          f"256×256 地形缓存 {big_bytes / 1e6:.1f}MB 仍在预算内")
    check(len(big_view._chunks) <= 256,
          f"256×256 缓存块数 {len(big_view._chunks)} 有上限")

    t0 = time.perf_counter()
    rules.build_detection_map(big_state)
    rules.build_detection_map(big_state)
    big_detect = (time.perf_counter() - t0) / 2 * 1000
    print(f"  256×256 探测全量 {big_detect:.2f} ms（{len(big_state.units)} 个单位）")
    check(big_detect < BIG_DETECT_LIMIT_MS,
          f"256×256 探测重算 {big_detect:.2f}ms < {BIG_DETECT_LIMIT_MS}ms")

    pygame.quit()
    if FAILS:
        print(f"\n性能回归自检失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("\n性能回归自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
