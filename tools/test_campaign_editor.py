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
from ui import theme as T
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
    # 必须在**各种边界状态**下都渲染一遍：空战役、空选择、浮层打开……
    # 之前只在「有 3 关」的状态下渲染，漏掉了空列表分支，
    # 结果是 `anchor="mid"` 这种非法锚点直到玩家手动开编辑器才暴露。
    ce.new_campaign()
    ce.camp.steps.clear()
    ce.step_index = 0
    ed.draw(screen)
    check(True, "空战役可渲染（无关卡提示）")

    ce.add_step()
    ed.draw(screen)
    check(True, "单关战役可渲染")

    ce.add_step()
    ce.add_step()
    ed.draw(screen)
    check(True, "多关战役可渲染（难度曲线）")

    # 战役选择浮层（空列表与非空列表都要画）
    ce._camp_picker_open = True
    ed.draw(screen)
    check(True, "战役选择浮层可渲染")

    # 地图选择浮层
    ce._camp_picker_open = False
    ce._map_picker_open = True
    ed.draw(screen)
    check(True, "地图选择浮层可渲染")
    ce._map_picker_open = False

    # 未选中任何关卡时的表单
    ce.camp.steps.clear()
    ce.step_index = 0
    ed.draw(screen)
    check(True, "未选中关卡时可渲染")

    # 锚点合法性：draw_text 必须拒绝非法锚点（否则运行时才炸）
    # 下面这处故意使用非法锚点 anchor="mid"，用来验证拒绝逻辑
    try:
        T.draw_text(screen, "测试", (10, 10), 12, anchor="mid")
        check(False, "非法锚点应当被拒绝")
    except ValueError:
        check(True, "非法锚点被明确拒绝（ValueError 而非 AttributeError）")

    # 曾经写错过的几个锚点：mid / midtop / midbottom / centerx —— 都要被拒绝
    bad_anchors = ["mid", "midtop", "midbottom", "centerx", "centery", "top", "bottom"]
    rejected = []
    for a in bad_anchors:
        try:
            T.draw_text(screen, "测试", (10, 10), 12, anchor=a)
            rejected.append(a)
        except ValueError:
            pass
    check(not rejected, f"历史错误锚点全部被拒绝（漏网：{rejected}）")

    # 所有合法锚点都应可用
    bad = []
    for a in sorted(T._VALID_ANCHORS):
        try:
            T.draw_text(screen, "测试", (100, 100), 12, anchor=a)
        except Exception as exc:
            bad.append(f"{a}:{exc}")
    check(not bad, f"全部合法锚点可用（问题：{bad}）")

    # 水平居中绘制：文字中心应对齐到给定 x
    w = T.text_width("测试", 12)
    r = T.draw_text_center_x(screen, "测试", 200, 50, 12)
    check(abs((r.left + r.right) // 2 - 200) <= 1,
          f"draw_text_center_x 水平居中（中心 {(r.left + r.right) // 2} vs 200）")
    check(r.top == 50, f"draw_text_center_x 顶端对齐（top={r.top}）")
    check(w > 0, "text_width 返回正数")

    pygame.quit()
    if FAILS:
        print(f"\n战役编辑器自检失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("\n战役编辑器自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
