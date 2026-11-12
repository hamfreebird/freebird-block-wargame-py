"""性能基准：量化渲染、探测、寻路各阶段的耗时。

用法：python tools/perf.py [map_id]
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
from core.loader import load_scenario  # noqa: E402
from core.paths import data_root  # noqa: E402


def _find_map(map_id: str) -> str:
    d = os.path.join(data_root(), "data", "maps")
    cand = os.path.join(d, map_id + ".json")
    if os.path.isfile(cand):
        return cand
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".json"):
            return os.path.join(d, fn)
    raise SystemExit("找不到任何地图")


def main() -> None:
    map_id = sys.argv[1] if len(sys.argv) > 1 else "strait_confrontation"
    armory = os.path.join(data_root(), "data", "units", "armory.json")
    t0 = time.perf_counter()
    if map_id in ("big256", "big", "256"):
        # 合成 256×256：不往仓库里塞一张 4MB 的地图 JSON，见 tools/synth_map.py
        from tools import synth_map
        scenario = synth_map.build(256)
        label = "big256（合成）"
    else:
        path = _find_map(map_id)
        scenario = load_scenario(path, armory)
        label = os.path.basename(path)
    print(f"地图 {label}  {scenario.map.width}×{scenario.map.height}  "
          f"单位 {len(scenario.units)}  载入 {time.perf_counter() - t0:.2f}s")

    pygame.init()
    screen = pygame.display.set_mode((1600, 900))

    from ui.mapview import MapView
    from core.engine import GameState
    from core.enums import Side

    state = GameState(scenario, Side.BLUE)
    view = MapView(pygame.Rect(0, 0, 1600, 900), state)

    # -- 地形构建 --
    t0 = time.perf_counter()
    view.draw(screen)
    t_terrain = time.perf_counter() - t0
    chunk_px = sum(c.get_width() * c.get_height() * 4 for c in view._chunks.values())
    print(f"首帧(含建块)  {t_terrain * 1000:8.1f} ms   缓存 {len(view._chunks)} 块 "
          f"{chunk_px / 1e6:.1f} MB")
    # -- 渲染 --
    state.refresh_detection()
    frames = 12
    t0 = time.perf_counter()
    for _ in range(frames):
        view.draw(screen)
    t_frame = (time.perf_counter() - t0) / frames
    print(f"整帧渲染      {t_frame * 1000:8.1f} ms   ({1 / t_frame:.0f} fps)")

    # 分解：只画地形 blit
    t0 = time.perf_counter()
    for _ in range(frames):
        view._draw_terrain(screen)
    print(f"  仅地形绘制 {(time.perf_counter() - t0) / frames * 1000:8.1f} ms")

    # 分解：单位绘制
    t0 = time.perf_counter()
    for _ in range(frames):
        view._draw_units(screen)
    print(f"  仅单位绘制 {(time.perf_counter() - t0) / frames * 1000:8.1f} ms")

    # 分解：目标点/工事/设施（都是全图扫描）
    t0 = time.perf_counter()
    for _ in range(frames):
        view._draw_objectives(screen)
    print(f"  仅目标点     {(time.perf_counter() - t0) / frames * 1000:8.1f} ms")

    t0 = time.perf_counter()
    for _ in range(frames):
        view._draw_facilities(screen)
    print(f"  仅设施       {(time.perf_counter() - t0) / frames * 1000:8.1f} ms")

    t0 = time.perf_counter()
    for _ in range(frames):
        view._draw_forts(screen)
    print(f"  仅工事       {(time.perf_counter() - t0) / frames * 1000:8.1f} ms")

    t0 = time.perf_counter()
    for _ in range(frames):
        view._draw_environment(screen)
    print(f"  仅环境层     {(time.perf_counter() - t0) / frames * 1000:8.1f} ms")

    t0 = time.perf_counter()
    for _ in range(frames):
        view._draw_fx(screen)
    print(f"  仅特效层     {(time.perf_counter() - t0) / frames * 1000:8.1f} ms")

    t0 = time.perf_counter()
    for _ in range(frames):
        view._draw_anim(screen)
    print(f"  仅动画层     {(time.perf_counter() - t0) / frames * 1000:8.1f} ms")

    # -- 探测 --
    t0 = time.perf_counter()
    for _ in range(3):
        rules.build_detection_map(state)
    t_det = (time.perf_counter() - t0) / 3
    print(f"探测全量重算  {t_det * 1000:8.1f} ms")

    # -- 寻路 --
    u = next((x for x in state.units if x.side is Side.BLUE), None)
    if u is not None:
        t0 = time.perf_counter()
        rules.reachable_tiles(state, u)
        print(f"可达集解算    {(time.perf_counter() - t0) * 1000:8.1f} ms")

    _ai_phase(state)

    pygame.quit()


def _ai_phase(state) -> None:
    """跑一回合双方 AI，打印相位耗时、单步分位与距离场缓存情况。

    为什么不设阈值：共享 CI 机器上同一种子能差 20%，设阈值等于养一个
    flaky 测试。回归断言交给 tools/test_perf.py 的渲染 / 探测那几条
    （那些稳定得多）；这里只把「AI 到底多慢、缓存有没有爆」变成常驻数字。
    """
    from core import pathfind
    from core.ai import AIPlanner

    if state.map.width > 160:
        print("AI 相位       跳过（大地图请用 python tools/perf.py big256 单独测）")
        return

    calls = [0]
    evict = [0]
    orig = pathfind._build_field

    def wrapped(st, unit, goal, limit, ignore_units):
        calls[0] += 1
        n0 = len(getattr(st, "_field_cache", {}))
        r = orig(st, unit, goal, limit, ignore_units)
        if len(getattr(st, "_field_cache", {})) < n0:
            evict[0] += 1
        return r

    pathfind._build_field = wrapped
    steps: list[float] = []
    turns: list[float] = []
    try:
        # 跑两回合：第一回合含全部「首次建场」，第二回合才是稳态。
        # 只报第一回合会把 p99 的成因（冷启动）误读成「每步都慢」。
        for _ in range(2):
            t_turn = time.perf_counter()
            for side in (state.player_side, state.player_side.foe):
                state.phase = "player" if side is state.player_side else "ai"
                planner = AIPlanner(state, side)
                guard = 0
                while planner.pending() and guard < 900 and not state.over:
                    guard += 1
                    ts = time.perf_counter()
                    act = planner.step(state)
                    steps.append(time.perf_counter() - ts)
                    if act is None:
                        break
                    state.apply_ai_action(act)
                state.resolve_missiles()
                state.refresh_detection()
            turns.append(time.perf_counter() - t_turn)
            state._advance_turn()
    finally:
        pathfind._build_field = orig

    steps.sort()
    n = len(steps)
    print(f"AI 两回合      {turns[0] * 1000:7.0f} / {turns[1] * 1000:7.0f} ms"
          f"（首回合含冷启动，{n} 步动作）")
    print(f"  单步 p50/p90/p99  {steps[n // 2] * 1000:.0f} / "
          f"{steps[int(n * 0.9)] * 1000:.0f} / {steps[int(n * 0.99)] * 1000:.0f} ms"
          f"（p99 基本是首次建场）")
    print(f"  距离场查询 {calls[0]} 次 · 缓存 {len(state._field_cache)} 条 · "
          f"淘汰 {evict[0]} 次")


if __name__ == "__main__":
    main()
