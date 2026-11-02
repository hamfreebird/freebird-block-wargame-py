"""战役模式：连续多张想定组成一个战役，共享部队与胜负点。

战役把若干张地图串成一条有叙事、有取舍的长线：

* **共享部队**：上一关活下来的单位会带到下一关——打残了就是打残了，
  下一关的初始兵力 = 该关固有部署 + 幸存单位；
* **共享胜负点**：每关的胜负点累计到战役总分，跨关比较；
* **进度存档**：战役进度独立存档，可以中途退出后继续。

战役定义放在 `data/campaigns/*.json`：

```jsonc
{
  "id": "eastern_front",
  "name": "东线推进",
  "description": "三阶段战役：突破 → 争夺 → 巩固",
  "author": "hamfreebird",
  "difficulty": "normal",
  "steps": [
    {"map": "steppe_thunder", "title": "第一阶段 · 突破",
     "brief": "红方装甲集群正在集结……"},
    {"map": "strait_shield", "title": "第二阶段 · 争夺",
     "brief": "夺下海峡是整个战役的关键……"}
  ]
}
```

`map` 可以是地图 id（对应 `data/maps/<id>.json`），也可以是文件名。
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

from .enums import Domain, Side, Terrain
from .model import Unit


class CampaignError(Exception):
    """战役定义或进度错误。"""


@dataclass
class CampaignStep:
    """战役中的一关。"""

    map_id: str = ""
    title: str = ""
    brief: str = ""
    objective: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"map": self.map_id, "title": self.title,
                "brief": self.brief, "objective": self.objective}


@dataclass
class CampaignDef:
    """战役定义。"""

    id: str = ""
    name: str = ""
    description: str = ""
    author: str = ""
    difficulty: str = ""
    steps: list[CampaignStep] = field(default_factory=list)
    path: str = ""

    @property
    def total_steps(self) -> int:
        return len(self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "author": self.author, "difficulty": self.difficulty,
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass
class CampaignProgress:
    """战役进度：跨关共享的状态。"""

    campaign_id: str = ""
    step_index: int = 0            # 当前进行到第几关（0 基）
    vp: dict[str, int] = field(default_factory=dict)   # 累计胜负点
    carryover: list[dict] = field(default_factory=list)  # 携带部队：[{template, count}]
    completed: list[str] = field(default_factory=list)   # 已通关的关卡 id
    log: list[str] = field(default_factory=list)         # 战役大事记

    @property
    def blue_vp(self) -> int:
        return self.vp.get("BLUE", 0)

    @property
    def red_vp(self) -> int:
        return self.vp.get("RED", 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "step_index": self.step_index,
            "vp": dict(self.vp),
            "carryover": list(self.carryover),
            "completed": list(self.completed),
            "log": list(self.log[-60:]),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CampaignProgress":
        vp = d.get("vp", {})
        if not isinstance(vp, dict):
            vp = {}
        carry = d.get("carryover", [])
        if not isinstance(carry, list):
            carry = []
        completed = d.get("completed", [])
        if not isinstance(completed, list):
            completed = []
        log = d.get("log", [])
        if not isinstance(log, list):
            log = []
        return cls(
            campaign_id=str(d.get("campaign_id", "")),
            step_index=int(d.get("step_index", 0)),
            vp={str(k): int(v) for k, v in vp.items()},
            carryover=[c for c in carry if isinstance(c, dict)],
            completed=[str(c) for c in completed],
            log=[str(x) for x in log],
        )


# --------------------------------------------------------------------------- #
# 战役定义读写
# --------------------------------------------------------------------------- #
def _campaigns_dir() -> str:
    from . import paths
    return os.path.join(paths.data_root(), "data", "campaigns")


def load_campaign(path: str) -> CampaignDef:
    """读取战役定义。"""
    if not os.path.isfile(path):
        raise CampaignError(f"战役文件不存在：{path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignError(f"读取战役失败：{exc}") from None
    if not isinstance(raw, dict):
        raise CampaignError("战役文件格式错误：根节点必须是对象")
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise CampaignError("战役文件缺少 steps 数组")
    steps: list[CampaignStep] = []
    for i, s in enumerate(steps_raw):
        if not isinstance(s, dict):
            raise CampaignError(f"steps[{i}] 必须是对象")
        map_id = str(s.get("map", ""))
        if not map_id:
            raise CampaignError(f"steps[{i}] 缺少 map 字段")
        steps.append(CampaignStep(
            map_id=map_id,
            title=str(s.get("title", f"第 {i + 1} 关")),
            brief=str(s.get("brief", "")),
            objective=str(s.get("objective", "")),
        ))
    return CampaignDef(
        id=str(raw.get("id", os.path.splitext(os.path.basename(path))[0])),
        name=str(raw.get("name", "未命名战役")),
        description=str(raw.get("description", "")),
        author=str(raw.get("author", "")),
        difficulty=str(raw.get("difficulty", "")),
        steps=steps,
        path=path,
    )


def list_campaigns(directory: str | None = None) -> list[CampaignDef]:
    """列出全部可用战役。"""
    d = directory or _campaigns_dir()
    out: list[CampaignDef] = []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.lower().endswith(".json"):
            continue
        try:
            out.append(load_campaign(os.path.join(d, fn)))
        except CampaignError:
            continue
    return out


def resolve_map_path(map_id: str, maps_dir: str) -> str:
    """把战役里的地图 id 解析为实际文件路径。"""
    cand = os.path.join(maps_dir, map_id)
    if os.path.isfile(cand):
        return cand
    cand = os.path.join(maps_dir, map_id + ".json")
    if os.path.isfile(cand):
        return cand
    raise CampaignError(f"战役引用的地图不存在：{map_id}")


# --------------------------------------------------------------------------- #
# 进度存档
# --------------------------------------------------------------------------- #
def progress_path(root: str, campaign_id: str) -> str:
    d = os.path.join(root, "saves")
    os.makedirs(d, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in campaign_id)
    return os.path.join(d, f"campaign_{safe}.json")


def save_progress(root: str, progress: CampaignProgress) -> str:
    """保存战役进度。"""
    path = progress_path(root, progress.campaign_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(progress.to_dict(), f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def load_progress(root: str, campaign_id: str) -> CampaignProgress | None:
    """读取战役进度；不存在返回 None。"""
    path = progress_path(root, campaign_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return CampaignProgress.from_dict(json.load(f))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def clear_progress(root: str, campaign_id: str) -> None:
    """清除战役进度。"""
    path = progress_path(root, campaign_id)
    if os.path.isfile(path):
        os.remove(path)


# --------------------------------------------------------------------------- #
# 跨关继承
# --------------------------------------------------------------------------- #
def collect_survivors(state, side: Side) -> list[dict]:
    """统计一关结束时该方幸存的单位（按兵种聚合）。

    只统计存活且仍可作战的单位；完全损毁的不带走。
    """
    counts: dict[str, int] = {}
    for u in state.units:
        if u.side is not side or not u.alive or u.is_decoy:
            continue
        # 航空兵需要母基地才能继续作战，不予继承
        if u.domain is Domain.AIR:
            continue
        counts[u.template.id] = counts.get(u.template.id, 0) + 1
    return [{"template": tid, "count": n} for tid, n in sorted(counts.items())]


def apply_carryover(state, carryover: list[dict], side: Side) -> list[str]:
    """把上一关的幸存单位加入本关初始部署。

    优先放在本关同兵种部署位置附近；该关没有该兵种时，
    放到己方部署区的空地上。返回实际加入的单位名列表。
    """
    if not carryover:
        return []
    md = state.map
    added: list[str] = []
    # 本关该方已有的部署位置（作为落点参考）
    anchors = [(u.x, u.y) for u in state.units if u.side is side and u.alive]
    for item in carryover:
        tid = str(item.get("template", ""))
        count = int(item.get("count", 0))
        if not tid or count <= 0:
            continue
        tpl = state.scenario.armory.templates.get(tid)
        if tpl is None:
            continue
        # 同兵种的既有部署位置优先
        same = [(u.x, u.y) for u in state.units
                if u.side is side and u.template.id == tid and u.alive]
        for _ in range(count):
            spot = _find_spot(state, tpl, same or anchors, side)
            if spot is None:
                break
            u = Unit(template=tpl, side=side, x=spot[0], y=spot[1])
            state.units.append(u)
            added.append(f"{u.name}×1")
            anchors.append(spot)
    return added


def _find_spot(state, tpl, anchors: list[tuple[int, int]], side: Side) -> tuple[int, int] | None:
    """在锚点附近找一个可部署的落点。"""
    md = state.map
    for ax, ay in anchors:
        for r in range(0, 4):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    nx, ny = ax + dx, ay + dy
                    if not md.in_bounds(nx, ny):
                        continue
                    if state.unit_at(nx, ny) is not None:
                        continue
                    t = md.at(nx, ny)
                    if t is None:
                        continue
                    if tpl.domain in (Domain.LAND, Domain.INFRA) \
                            and t.terrain is Terrain.WATER:
                        continue
                    if tpl.domain in (Domain.SEA, Domain.SUB) and not t.passable_sea:
                        continue
                    return (nx, ny)
    return None


__all__ = [
    "CampaignError", "CampaignStep", "CampaignDef", "CampaignProgress",
    "load_campaign", "list_campaigns", "resolve_map_path",
    "progress_path", "save_progress", "load_progress", "clear_progress",
    "collect_survivors", "apply_carryover",
]
