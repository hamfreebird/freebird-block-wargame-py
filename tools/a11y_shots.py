"""生成无障碍验收截图：字号三档 × 关键界面，另附灰度版本。

9.6 的验收是「三档下主菜单 / 信息卡 / 交战面板 / 战报回顾均无重叠与截断」，
9.7 是「灰度截图里仍能分辨敌我与兵种类别」——这两条都得用眼睛看。
本工具把要看的画面一次性出图，不必每次手点进游戏再截。

用法：
    python tools/a11y_shots.py              # 三档全出
    python tools/a11y_shots.py large        # 只出某一档
输出到 <项目根>/.shots/（已在 .gitignore 内，不会进版本库）。
"""
from __future__ import annotations

import os
import sys

# Windows 控制台/管道默认编码（cp1252 等）无法输出中文，
# 统一把标准输出切到 UTF-8，缺失字符以替换符兜底，避免 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import shutil
import tempfile

# 截图会切换主题 / 字号并存配置，隔离到临时目录，别污染玩家真实数据
_USER_DIR = tempfile.mkdtemp(prefix="bw_shots_")
os.environ["BLOCKWARGAME_USER_DIR"] = _USER_DIR

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import pygame  # noqa: E402

from core.enums import Side  # noqa: E402
from ui import theme as T  # noqa: E402
from ui.app import App  # noqa: E402

OUT_DIR = os.path.join(_ROOT, ".shots")


def save(surf: pygame.Surface, name: str) -> None:
    pygame.image.save(surf, os.path.join(OUT_DIR, name + ".png"))


def grayscale(surf: pygame.Surface) -> pygame.Surface:
    """按亮度转灰度——色盲验收就是看这张图还能不能分出敌我。"""
    return pygame.transform.grayscale(surf)


def pick_engage_pair(app: App):
    """找一对「我方有弹 + 敌方已被识别」的单位，用来出交战面板。"""
    for u in app.state.units_of(Side.BLUE):
        if not (u.template.weapons and u.has_ammo()):
            continue
        for e in app.state.enemies_of(Side.BLUE):
            if e.alive and app.state.known_level(Side.BLUE, e).level >= 2:
                return u, e
    return None, None


def shoot_scale(scale_id: str) -> int:
    app = App()
    # 走设置面板里的同一条路径切档：那里会清棋子缓存并重排布局，
    # 比直接改 theme 更接近玩家实际操作。
    app._apply_font_scale(scale_id)
    app.draw()                            # 先画一帧，让几何都算出来

    # ---- 主菜单 ----
    app.mode = "menu"
    app.draw()
    save(app.screen, f"{scale_id}-menu")

    # ---- 对局：交战面板 ----
    app.start_tutorial()
    app._close_briefing()
    app.tab = "unit"
    sel, tgt = pick_engage_pair(app)
    if sel is None:
        print(f"  [{scale_id}] 找不到交战样本，跳过交战/信息卡")
    else:
        app.select_unit(sel)
        app.pending_target = tgt
        app.draw()
        save(app.screen, f"{scale_id}-engage")

        # ---- 信息卡（悬停在单位上时右侧浮出的小卡） ----
        app.pending_target = None
        app.draw()
        card_pos = (app.w - T.m(260), app.h // 2)
        app.inspect.draw(app.screen, sel, Side.BLUE,
                         app.state.known_level(Side.BLUE, sel), app.state, card_pos)
        save(app.screen, f"{scale_id}-card")

    # ---- 棋子近景（色盲验收用，普通与灰度各一张） ----
    # 形状标识默认只在色盲主题下开，这里强制打开——验收要看的正是它
    app._apply_shape_cues("on")
    mv = app.mapview
    mv.tile = 52                     # 放大到能看清角标与切角
    if sel is not None and tgt is not None:
        cx, cy = (sel.x + tgt.x) / 2, (sel.y + tgt.y) / 2
        mv.ox = mv.rect.width / 2 - (cx + 0.5) * mv.tile
        mv.oy = mv.rect.height / 2 - (cy + 0.5) * mv.tile
        mv._clamp()
    app.draw()
    crop = mv.rect.clip(app.screen.get_rect())
    tokens = app.screen.subsurface(crop).copy()
    save(tokens, f"{scale_id}-tokens")
    save(grayscale(tokens), f"{scale_id}-tokens-gray")

    # ---- 战报回顾 ----
    app.state.over = True
    app.state.winner = Side.BLUE
    app._game_over()
    app.draw()
    if app.debrief is not None:
        save(app.screen, f"{scale_id}-debrief")
    else:
        print(f"  [{scale_id}] 未生成战报数据，跳过回顾")

    # ---- 设置面板（字号 / 形状两行的落位） ----
    app.debrief = None
    app.mode = "game"
    app.open_settings()
    app.draw()
    save(app.screen, f"{scale_id}-settings")
    return 0


def main() -> int:
    wanted = sys.argv[1:] or list(T.font_scale_ids())
    bad = [w for w in wanted if w not in T.font_scale_ids()]
    if bad:
        print(f"未知档位：{', '.join(bad)}")
        print(f"可选：{', '.join(T.font_scale_ids())}")
        return 2

    os.makedirs(OUT_DIR, exist_ok=True)
    try:
        for scale_id in wanted:
            shoot_scale(scale_id)
            print(f"  [{scale_id}] 已出图")
    finally:
        shutil.rmtree(_USER_DIR, ignore_errors=True)

    print(f"截图目录：{OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
