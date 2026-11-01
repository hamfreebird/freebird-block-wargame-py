"""开发期回归脚本：双 AI 推演并输出诊断信息。"""
import random
import sys

sys.path.insert(0, ".")

from core.ai import AIPlanner
from core.engine import GameState
from core.enums import Side
from core.loader import load_scenario


def play(st, side):
    p = AIPlanner(st, side)
    g = 0
    while p.pending() and g < 800:
        g += 1
        act = p.step(st)
        if act is None:
            break
        st.apply_ai_action(act)


def run(map_name, turns=16, seed=1234):
    random.seed(seed)
    sc = load_scenario(f"data/maps/{map_name}.json", "data/units/armory.json")
    st = GameState(sc, Side.BLUE)
    for _ in range(turns):
        if st.over:
            break
        st.phase = "player"
        play(st, Side.BLUE)
        st.resolve_missiles()
        st.refresh_detection()
        st.phase = "ai"
        play(st, Side.RED)
        st.resolve_missiles()
        st.refresh_detection()
        st._advance_turn()
    return st


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "steppe_thunder"
    turns = int(sys.argv[2]) if len(sys.argv) > 2 else 16
    st = run(name, turns)
    print(f"{name}: 蓝{len(st.units_of(Side.BLUE))} / 红{len(st.units_of(Side.RED))} "
          f"VP {st.vp[Side.BLUE]}:{st.vp[Side.RED]} 损 {st.casualties[Side.BLUE]}:{st.casualties[Side.RED]}")
    crash = [e for e in st.log if "坠毁" in e.text]
    print("坠毁:", len(crash))
    for e in crash:
        print("   ", e.turn, e.text)
    strikes = [e for e in st.log if "打击" in e.text and "结构" in e.text]
    print("设施打击:", len(strikes))
    for e in strikes[:6]:
        print("   ", e.turn, e.text[:100])
    asat = [e for e in st.log if "卫星" in e.text and ("反卫星" in e.text or "击毁" in e.text)]
    print("反卫星:", len(asat))
    for e in asat[:4]:
        print("   ", e.turn, e.text[:100])
    print("红方单位:", [(u.template.short, (u.x, u.y), round(u.hp), round(u.fuel, 2))
                        for u in st.units_of(Side.RED)])
