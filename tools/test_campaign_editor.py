"""v2.1 专项自检：战役编辑器。

覆盖：
* 关卡的增删、移动、复制；
* 地图选择、难度与继承策略设置；
* 战役属性的编辑与保存/读回；
* 校验逻辑（缺地图、重复地图）；
* 继承策略对幸存部队的筛选效果；
* 难度对初始胜负点的影响。

用法：python tools/test_campaign_editor.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

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

from core.campaign import (CARRYOVER_ALL, CARRYOVER_NONE, CARRYOVER_VETERANS,
                           CampaignStep, apply_carryover_policy, load_campaign)
from core.enums import Side  # noqa: E402
from ui.editor import Editor  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  [OK] {msg}")
    else:
        print(f"  [FAIL] {msg}")
        FAILS.append(msg)


def main() -> int:
    pygame.init()
    screen = pygame.display.set_mode((1600, 900))
    ed = Editor(screen)
    ed.tab = "campaign"
    ce = ed.campaign_editor

    print("=== 战役编辑器自检 ===")

    # -- 关卡增删 --
    ce.new_campaign()
    check(len(ce.camp.steps) == 0, "新建战役没有关卡")
    ce.add_step()
    ce.add_step()
    ce.add_step()
    check(len(ce.camp.steps) == 3, "可添加关卡")
    check(ce.step_index == 2, "新增后选中最后一关")

    # -- 移动 --
    ce.move_step(-1)
    check(ce.step_index == 1, "关卡可上移")
    ce.move_step(1)
    check(ce.step_index == 2, "关卡可下移")
    ce.move_step(5)
    check(ce.step_index == 2, "越界移动被忽略")

    # -- 复制 --
    before = len(ce.camp.steps)
    ce.duplicate_step()
    check(len(ce.camp.steps) == before + 1, "关卡可复制")
    check("（副本）" in ce.camp.steps[ce.step_index].title, "副本标题带标记")

    # -- 删除 --
    ce.remove_step()
    check(len(ce.camp.steps) == before, "关卡可删除")
    ce.step_index = 0
    for _ in range(10):
        ce.remove_step()
    check(len(ce.camp.steps) == 1, "至少保留一关")

    # -- 难度与继承策略 --
    ce.step_index = 0
    ce._do_action("diff", 1)
    check(ce.current().difficulty == 3, "难度可调高")
    ce._do_action("diff", -1)
    ce._do_action("diff", -1)
    check(ce.current().difficulty == 1, "难度可调低（不低于 1）")
    ce._do_action("diff", -1)
    check(ce.current().difficulty == 1, "难度下限为 1")
    ce._do_action("carry", CARRYOVER_VETERANS)
    check(ce.current().carryover == CARRYOVER_VETERANS, "继承策略可切换")

    # -- 地图选择 --
    check(len(ce.map_choices) >= 1, "能列出可用地图")
    if ce.map_choices:
        mid = ce.map_choices[0][1]
        ce._do_action("map_pick_choose", mid)
        check(ce.current().map_id == mid, "地图可选择")

    # -- 属性编辑 --
    ce.inputs["camp_name"].value = "自检战役"
    ce.inputs["camp_desc"].value = "由测试创建"
    ce.inputs["camp_author"].value = "tester"
    ce.inputs["title"].value = "第一关"
    ce.inputs["brief"].value = "简报内容"
    ce.inputs["objective"].value = "守住阵地"
    ce._apply_inputs()
    check(ce.camp.name == "自检战役", "战役名可编辑")
    check(ce.camp.steps[0].title == "第一关", "关卡标题可编辑")
    check(ce.camp.steps[0].brief == "简报内容", "简报可编辑")
    check(ce.camp.steps[0].objective == "守住阵地", "任务目标可编辑")

    # -- 校验 --
    ce.camp.steps[0].map_id = ""
    ce.validate()
    check(bool(ce.error), "缺地图时校验报错")
    ce.camp.steps[0].map_id = "definitely_not_a_map"
    ce.validate()
    check(bool(ce.error), "地图不存在时校验报错")
    if len(ce.map_choices) >= 2:
        ce.camp.steps[0].map_id = ce.map_choices[0][1]
        ce.add_step()
        ce.camp.steps[1].map_id = ce.map_choices[0][1]
        ce.validate()
        check(bool(ce.error), "重复地图时校验报错")
        ce.remove_step()
    ce.camp.steps[0].map_id = ce.map_choices[0][1] if ce.map_choices else "training_ground"
    ce.validate()
    check(not ce.error, "合法战役校验通过")

    # -- 保存与读回 --
    with tempfile.TemporaryDirectory() as td:
        ce.camp.id = "selftest_campaign"
        ce.path = os.path.join(td, "selftest_campaign.json")
        ce.save_campaign()
        check(os.path.isfile(ce.path), "战役已保存")
        back = load_campaign(ce.path)
        check(back.name == "自检战役", "读回战役名一致")
        check(len(back.steps) == len(ce.camp.steps), "读回关卡数一致")
        check(back.steps[0].difficulty == ce.camp.steps[0].difficulty, "读回难度一致")
        check(back.steps[0].carryover == ce.camp.steps[0].carryover, "读回继承策略一致")
        raw = json.load(open(ce.path, encoding="utf-8"))
        check("difficulty" in raw["steps"][0], "存档含难度字段")
        check("carryover" in raw["steps"][0], "存档含继承策略字段")

    # -- 继承策略筛选 --
    survivors = [{"template": "m1a2", "count": 1},
                 {"template": "t90", "count": 5},
                 {"template": "lav", "count": 3}]
    check(len(apply_carryover_policy(survivors, CARRYOVER_ALL)) == 3, "全继承保留全部")
    check(len(apply_carryover_policy(survivors, CARRYOVER_NONE)) == 0, "不继承全部丢弃")
    vet = apply_carryover_policy(survivors, CARRYOVER_VETERANS)
    check(len(vet) == 2, "只继承精锐保留战损小的兵种")
    check(all(s["count"] >= 2 for s in vet), "精锐判定依据存活数量")

    # -- 绘制 --
    ed.draw(screen)
    check(True, "战役编辑器可渲染")

    pygame.quit()
    if FAILS:
        print(f"\n战役编辑器自检失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("\n战役编辑器自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
