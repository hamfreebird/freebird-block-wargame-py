"""生成内置图标包：把矢量兵种图形烘焙成 PNG。

用途有两个：

1. **给社区一个模板**——想自己做图标包的人可以照着这个包的格式改；
2. **验证图标包机制**——内置包的存在证明「换一套图标包即可换风格」。

烘焙方式：用与运行时完全相同的绘制代码把矢量图形画到一张
透明背景的白色画布上，保存为 PNG。运行时按阵营着色，
因此一套素材能同时用于红蓝双方。

用法::

    python tools/make_icon_pack.py
"""
from __future__ import annotations

import json
import os
import sys

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

from core.enums import UnitClass  # noqa: E402
from core.paths import data_root  # noqa: E402

SIZE = 64
PACK_ID = "default"
PACK_NAME = "内置线描图标"


def _vector_icon(surf: pygame.Surface, rect: pygame.Rect, cls: UnitClass) -> None:
    """把某个兵种的矢量图形画到 surf 上（白色）。

    直接复用渲染层的绘制函数——保证「工具生成的图标」与
    「游戏里画的图标」是同一套图形，不会分叉。
    """
    from ui.mapview import draw_vector_class_icon

    draw_vector_class_icon(surf, rect, cls, (255, 255, 255))


def main() -> int:
    pygame.init()
    out_dir = os.path.join(data_root(), "data", "icon_packs", PACK_ID)
    os.makedirs(out_dir, exist_ok=True)

    icons: dict[str, str] = {}
    for cls in UnitClass:
        surf = pygame.Surface((SIZE, SIZE), pygame.SRCALPHA)
        surf.fill((0, 0, 0, 0))
        _vector_icon(surf, pygame.Rect(0, 0, SIZE, SIZE), cls)
        fn = f"{cls.value}.png"
        pygame.image.save(surf, os.path.join(out_dir, fn))
        icons[cls.value] = fn

    manifest = {
        "id": PACK_ID,
        "name": PACK_NAME,
        "author": "hamfreebird",
        "version": 1,
        "note": "内置线描图标——由矢量图形烘焙，可作为自制图标包的模板",
        "icons": icons,
    }
    with open(os.path.join(out_dir, "pack.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"已生成内置图标包：{out_dir}")
    print(f"  {len(icons)} 个兵种图标，尺寸 {SIZE}×{SIZE}")
    pygame.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
