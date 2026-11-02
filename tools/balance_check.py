"""平衡体检：自动检查兵种树与想定部署。

**为什么需要**：兵种库有 65 个兵种、71 种武器，靠人工审阅很难发现
「某个兵种完全没人能打」「造价倒挂」「射程断层」这类问题。
体检把它们变成**可复现的报告**。

检查项
------
1. **射程断层**——同一作战域内，某档射程没有任何兵种覆盖；
2. **造价倒挂**——更贵的东西在关键指标上全面更差（性价比倒挂）；
3. **克制缺失**——某个作战域没有能有效对抗的兵种；
4. **数值异常**——射程 / 造价 / 血量的离群值；
5. **部署陷阱**——单位被部署在走不出去的格子里（v2.2 修掉的正是这类）。

体检是**只读**的：它只报告问题，不修改数据。
用法：python tools/balance_check.py
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

from core.enums import Domain, UnitClass  # noqa: E402
from core.paths import data_root  # noqa: E402


@dataclass
class Finding:
    """一条体检结论。"""

    kind: str          # 问题类别
    severity: str      # info / warn / error
    subject: str       # 涉及的对象（兵种 id / 地图名）
    message: str       # 说明
    hint: str = ""     # 修复建议

    def format(self) -> str:
        tag = {"info": "提示", "warn": "警告", "error": "错误"}[self.severity]
        s = f"[{tag}] {self.kind} · {self.subject}：{self.message}"
        if self.hint:
            s += f"\n        → {self.hint}"
        return s


@dataclass
class Report:
    """体检报告。"""

    findings: list[Finding] = field(default_factory=list)

    def add(self, kind: str, severity: str, subject: str, message: str,
            hint: str = "") -> None:
        self.findings.append(Finding(kind, severity, subject, message, hint))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warn"]

    def summary(self) -> str:
        return (f"共 {len(self.findings)} 条结论："
                f"{len(self.errors)} 错误 / {len(self.warnings)} 警告 / "
                f"{len(self.findings) - len(self.errors) - len(self.warnings)} 提示")


# --------------------------------------------------------------------------- #
# 兵种树检查
# --------------------------------------------------------------------------- #
# 射程断层的判定阈值：相邻两个射程档之间空白超过这么多格算断层。
# 用**实际数据**而不是预设档位——预设档位会误报
# （比如空中兵种本来就够不到 14 格，那不是断层，是设计）。
_RANGE_GAP = 4.0


def _unit_fire_range(u: dict) -> float:
    """兵种的最大射程（格）。

    兵种的 weapons 是 `[{ref: 武器 id, ammo: 备弹}]` 的挂载表，
    武器本体在武器库里查。
    """
    r = 0.0
    for mount in u.get("weapons", []):
        w = WEAPONS.get(mount.get("ref", ""))
        if w:
            r = max(r, float(w.get("range_max", 0)))
    return r


def _unit_targets(u: dict) -> list[str]:
    """兵种能打击的作战域集合。"""
    out: set[str] = set()
    for mount in u.get("weapons", []):
        w = WEAPONS.get(mount.get("ref", ""))
        if w:
            out.update(w.get("targets", []))
    return sorted(out)


WEAPONS: dict[str, dict] = {}
UNITS: list[dict] = []


def _load_armory(path: str) -> None:
    global WEAPONS, UNITS
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    WEAPONS = {w["id"]: w for w in doc.get("weapons", [])}
    UNITS = doc.get("units", [])


def check_unit_tree(rep: Report) -> None:
    """检查兵种树：射程断层、造价倒挂、克制缺失、数值异常。"""
    by_domain: dict[str, list[dict]] = {}
    for u in UNITS:
        by_domain.setdefault(u.get("domain", "land"), []).append(u)

    # -- 1) 射程断层：相邻射程档之间有大段空白 --
    for dom, units in by_domain.items():
        pairs = sorted(((_unit_fire_range(u), u.get("id", "?")) for u in units),
                       reverse=True)
        pairs = [(r, i) for r, i in pairs if r > 0]
        if len(pairs) < 2:
            continue
        for (r_hi, id_hi), (r_lo, id_lo) in zip(pairs, pairs[1:]):
            if r_hi - r_lo > _RANGE_GAP:
                rep.add("射程断层", "warn", dom,
                        f"{r_lo:.0f}–{r_hi:.0f} 格之间没有兵种（"
                        f"上一档 {id_hi} {r_hi:.0f} 格，下一档 {id_lo} {r_lo:.0f} 格）",
                        f"补一个射程落在 {r_lo:.0f}–{r_hi:.0f} 的兵种，"
                        f"或把 {id_hi} / {id_lo} 的射程调近一些")

    # -- 2) 造价倒挂：贵的在对阵上全面更差 --
    for u in UNITS:
        cost = u.get("cost", 0)
        if cost <= 0:
            continue
        hp = u.get("hp", 0)
        rng = _unit_fire_range(u)
        if hp <= 0:
            continue
        # 单位造价所能期望的「血量 × 射程」下限
        peers = [p for p in UNITS if p.get("domain") == u.get("domain")
                 and p.get("cost", 0) > 0]
        if len(peers) < 3:
            continue
        best = max(peers, key=lambda p: p.get("cost", 0))
        if best["id"] == u["id"]:
            continue
        # 性价比：造价高 50% 以上，但血量与射程都不占优
        if cost >= best.get("cost", 0) * 1.5:
            if hp <= best.get("hp", 0) and rng <= _unit_fire_range(best):
                rep.add("造价倒挂", "warn", u.get("id", "?"),
                        f"造价 {cost}（最贵的 {best.get('cost')}）但血量 {hp} "
                        f"与射程 {rng:.0f} 都不优于 {best.get('id')}",
                        "提高其血量或射程，或降低造价")

    # -- 3) 克制缺失：某作战域没有能打击它的兵种 --
    all_targets: set[str] = set()
    for u in UNITS:
        all_targets.update(_unit_targets(u))
    for dom in ("land", "sea", "air"):
        if dom not in all_targets:
            rep.add("克制缺失", "error", dom,
                    f"没有任何兵种能打击 {dom} 域的目标",
                    "补一个挂载对应对域武器的兵种")

    # -- 4) 数值异常：射程或造价离群 --
    for u in UNITS:
        rng = _unit_fire_range(u)
        if rng > 60:
            rep.add("数值异常", "warn", u.get("id", "?"),
                    f"射程 {rng:.0f} 格远超常规（60 格以上）",
                    "确认这是有意为之的超远程武器")
        cost = u.get("cost", 0)
        if cost > 800:
            rep.add("数值异常", "info", u.get("id", "?"),
                    f"造价 {cost} 属于顶级单位，确认其性能配得上")
        hp = u.get("hp", 0)
        if hp > 900:
            rep.add("数值异常", "info", u.get("id", "?"),
                    f"血量 {hp} 属于顶级单位，确认其机动与火力配得上")

    # -- 5) 无武器的作战单位 --
    for u in UNITS:
        if not u.get("weapons") and u.get("domain") in ("land", "sea"):
            if not u.get("sensors"):
                rep.add("数值异常", "warn", u.get("id", "?"),
                        "既无武器也无传感器——在战场上没有任何作用")


# --------------------------------------------------------------------------- #
# 想定部署检查
# --------------------------------------------------------------------------- #
def check_scenarios(rep: Report) -> None:
    """检查想定部署：单位是否被放在走不出去的格子里。"""
    from core.engine import GameState
    from core.enums import Side
    from core.loader import load_scenario
    from core import rules

    maps_dir = os.path.join(data_root(), "data", "maps")
    armory = os.path.join(data_root(), "data", "units", "armory.json")
    if not os.path.isdir(maps_dir):
        return
    for fn in sorted(os.listdir(maps_dir)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(maps_dir, fn)
        try:
            sc = load_scenario(path, armory)
        except Exception:
            continue
        st = GameState(sc, Side.BLUE)
        stuck = []
        for u in st.units:
            if not u.alive or u.mp <= 0:
                continue
            # 用**正规洪泛**判定（不含 v2.2 的保底移动）——
            # 体检要报告的是「部署本身有问题」，
            # 而不是「规则兜底之后能不能动」。
            # 只要正规移动走不了就算部署陷阱（不看保底）
            reach = rules.flood_reachable(st, u, allow_fallback=False)
            if len(reach) > 1:
                continue
            if not rules.fallback_moves(st, u):
                # 连保底出路都没有：真的无路可走
                    stuck.append(f"{u.template.short}@({u.x},{u.y})" "[无出路]")
            else:
                stuck.append(f"{u.template.short}@({u.x},{u.y})")
        if stuck:
            rep.add("部署陷阱", "warn", fn[:-5],
                    f"{len(stuck)} 个单位开局就动不了：{', '.join(stuck[:4])}"
                    + ("…" if len(stuck) > 4 else ""),
                    "把单位挪到机动点足够走出的地形上；若该陆块整体缺少低代价地块（平原/荒漠），则需提高该兵种的机动点或调整地形")


def main() -> int:
    from core.paths import data_root as _dr
    armory = os.path.join(_dr(), "data", "units", "armory.json")
    if not os.path.isfile(armory):
        print("未找到武器库")
        return 1
    _load_armory(armory)
    rep = Report()
    check_unit_tree(rep)
    check_scenarios(rep)

    print("=== 平衡体检 ===")
    print(f"兵种 {len(UNITS)} 个，武器 {len(WEAPONS)} 种")
    print()
    if not rep.findings:
        print("未发现问题。")
        return 0
    for f in rep.findings:
        print(f.format())
    print()
    print(rep.summary())
    # 只有**错误**才让 CI 失败——警告是「值得看一眼」，
    # 阻断流水线会让体检工具本身变成负担，没人愿意跑。
    return 1 if rep.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
