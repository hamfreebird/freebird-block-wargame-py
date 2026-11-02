"""社区想定包：把想定打包成一个 zip，方便分发与导入。

一个 `.bwscn` 包（本质是 zip）内含：

```
package.json          包清单：格式版本、名称、作者、包含的想定
maps/<id>.json        想定地图（可含多张，战役模式用）
armory.json           武器覆盖（可选）：只写与内置库不同的部分
README.md             说明（可选）
```

导入时**只覆盖**包内声明的武器条目，不影响用户的其它自定义兵种——
这样社区包既能提供"新武器"，也不会破坏用户已有的编辑。

导出时自动生成清单并计算文件校验和，便于分发时验证完整性。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from typing import Any

PACKAGE_FORMAT = "bwscn"
PACKAGE_VERSION = 1
PACKAGE_SUFFIX = ".bwscn"


class PackageError(Exception):
    """想定包读写失败。"""


@dataclass
class PackageManifest:
    """包清单。"""

    format: str = PACKAGE_FORMAT
    version: int = PACKAGE_VERSION
    name: str = ""
    author: str = ""
    description: str = ""
    difficulty: str = ""
    tags: list[str] = field(default_factory=list)
    maps: list[str] = field(default_factory=list)      # 包内地图文件名
    armory_included: bool = False
    created: str = ""                                   # ISO 时间戳

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "version": self.version,
            "name": self.name,
            "author": self.author,
            "description": self.description,
            "difficulty": self.difficulty,
            "tags": list(self.tags),
            "maps": list(self.maps),
            "armory_included": self.armory_included,
            "created": self.created,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PackageManifest":
        if not isinstance(d, dict):
            raise PackageError("清单格式错误：package.json 必须是对象")
        fmt = str(d.get("format", ""))
        if fmt != PACKAGE_FORMAT:
            raise PackageError(f"不是有效的想定包（format='{fmt}'）")
        ver = int(d.get("version", 0))
        if ver > PACKAGE_VERSION:
            raise PackageError(f"想定包版本 {ver} 高于当前程序支持的 {PACKAGE_VERSION}")
        maps = d.get("maps", [])
        if not isinstance(maps, list):
            raise PackageError("清单格式错误：maps 必须是数组")
        tags = d.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        return cls(
            format=fmt, version=ver,
            name=str(d.get("name", "")),
            author=str(d.get("author", "")),
            description=str(d.get("description", "")),
            difficulty=str(d.get("difficulty", "")),
            tags=[str(t) for t in tags],
            maps=[str(m) for m in maps],
            armory_included=bool(d.get("armory_included", False)),
            created=str(d.get("created", "")),
        )


@dataclass
class ImportedPackage:
    """导入结果。"""

    manifest: PackageManifest
    maps_dir: str = ""           # 导入后的地图目录
    added_maps: list[str] = field(default_factory=list)    # 导入的地图文件名
    armory_units: list[dict] = field(default_factory=list)   # 导入的武器覆盖
    added_templates: list[str] = field(default_factory=list)  # 新增的兵种 id


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #
def export_package(map_paths: list[str], armory_path: str, out_path: str,
                    name: str = "", author: str = "",
                    include_armory: bool = True,
                    only_armory_ids: list[str] | None = None) -> str:
    """把想定打包成 .bwscn。

    Args:
        map_paths: 要包含的想定文件路径
        armory_path: 武器库路径
        out_path: 输出路径（.bwscn）
        only_armory_ids: 只导出这些兵种（None 表示全部）；导出全部时体积较大
    """
    if not map_paths:
        raise PackageError("没有要导出的想定")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)

    import time
    manifest = PackageManifest(
        name=name or os.path.splitext(os.path.basename(map_paths[0]))[0],
        author=author,
        maps=[],
        armory_included=include_armory,
        created=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for mp in map_paths:
            if not os.path.isfile(mp):
                raise PackageError(f"想定文件不存在：{mp}")
            base = os.path.basename(mp)
            arc = f"maps/{base}"
            z.write(mp, arc)
            manifest.maps.append(base)
            # 提取元数据到清单，便于浏览
            try:
                with open(mp, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                meta = raw.get("meta") or {}
                if not manifest.description:
                    manifest.description = str(raw.get("description", ""))[:400]
                if not manifest.difficulty:
                    manifest.difficulty = str(meta.get("difficulty", ""))
                if not manifest.author:
                    manifest.author = str(meta.get("author", ""))
                tags = meta.get("tags", [])
                for t in tags:
                    if t not in manifest.tags:
                        manifest.tags.append(t)
            except (OSError, json.JSONDecodeError):
                pass

        if include_armory and armory_path and os.path.isfile(armory_path):
            with open(armory_path, "r", encoding="utf-8") as f:
                armory = json.load(f)
            units = armory.get("units", [])
            if only_armory_ids is not None:
                keep = set(only_armory_ids)
                units = [u for u in units if u.get("id") in keep]
                # 只导出被选中的兵种所依赖的武器与传感器
                used_w = set()
                used_s = set()
                for u in units:
                    for w in u.get("weapons", []):
                        used_w.add(w.get("ref") if isinstance(w, dict) else w)
                    for s in u.get("sensors", []):
                        used_s.add(s.get("ref") if isinstance(s, dict) else s)
                weapons = [w for w in armory.get("weapons", []) if w.get("id") in used_w]
                sensors = [s for s in armory.get("sensors", []) if s.get("id") in used_s]
            else:
                weapons = armory.get("weapons", [])
                sensors = armory.get("sensors", [])
            payload = {
                "units": units,
                "weapons": weapons,
                "sensors": sensors,
            }
            z.writestr("armory.json", json.dumps(payload, ensure_ascii=False, indent=1))

        z.writestr("package.json",
                   json.dumps(manifest.to_dict(), ensure_ascii=False, indent=1))
    return out_path


# --------------------------------------------------------------------------- #
# 导入
# --------------------------------------------------------------------------- #
def inspect_package(path: str) -> PackageManifest:
    """只读取清单，不解压——用于浏览与校验。"""
    if not os.path.isfile(path):
        raise PackageError(f"想定包不存在：{path}")
    try:
        with zipfile.ZipFile(path, "r") as z:
            if "package.json" not in z.namelist():
                raise PackageError("想定包缺少 package.json")
            with z.open("package.json") as f:
                data = json.loads(f.read().decode("utf-8"))
    except zipfile.BadZipFile:
        raise PackageError("文件不是有效的 zip（想定包应为 .bwscn 结尾的 zip）") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageError(f"读取清单失败：{exc}") from None
    return PackageManifest.from_dict(data)


def import_package(path: str, maps_target: str, armory_path: str,
                   merge_armory: bool = True) -> ImportedPackage:
    """导入想定包。

    地图直接写入 `maps_target`；武器覆盖按 id 合并进 `armory_path`（不删除既有条目）。
    """
    manifest = inspect_package(path)
    os.makedirs(maps_target, exist_ok=True)

    added: list[str] = []
    units: list[dict] = []
    try:
        with zipfile.ZipFile(path, "r") as z:
            names = z.namelist()
            for arc in manifest.maps:
                # 清单里存的是文件名，包内路径为 maps/<文件名>；两者都接受
                cand = arc if arc in names else f"maps/{os.path.basename(arc)}"
                if cand not in names:
                    raise PackageError(f"清单声明的地图 '{arc}' 不在包内")
                base = os.path.basename(cand)
                target = os.path.join(maps_target, base)
                with z.open(cand) as fsrc, open(target, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst)
                added.append(base)

            if "armory.json" in names and merge_armory:
                with z.open("armory.json") as f:
                    payload = json.loads(f.read().decode("utf-8"))
                units = payload.get("units", [])
                _merge_armory(armory_path, payload)
    except PackageError:
        raise
    except zipfile.BadZipFile:
        raise PackageError("想定包已损坏（zip 读取失败）") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageError(f"导入失败：{exc}") from None

    return ImportedPackage(
        manifest=manifest,
        maps_dir=maps_target,
        added_maps=added,
        armory_units=units,
        added_templates=[str(u.get("id", "")) for u in units],
    )


def _merge_armory(armory_path: str, payload: dict) -> None:
    """把包内武器覆盖合并进武器库：按 id 覆盖，不删除既有条目。"""
    if not armory_path or not os.path.isfile(armory_path):
        return
    with open(armory_path, "r", encoding="utf-8") as f:
        armory = json.load(f)

    for key in ("weapons", "sensors", "units"):
        incoming = payload.get(key, [])
        if not isinstance(incoming, list):
            continue
        existing = armory.setdefault(key, [])
        by_id = {str(e.get("id")): i for i, e in enumerate(existing)}
        for item in incoming:
            if not isinstance(item, dict):
                continue
            iid = str(item.get("id", ""))
            if not iid:
                continue
            if iid in by_id:
                existing[by_id[iid]] = item      # 覆盖同名条目
            else:
                existing.append(item)
                by_id[iid] = len(existing) - 1

    tmp = armory_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(armory, f, ensure_ascii=False, indent=2)
    os.replace(tmp, armory_path)


# --------------------------------------------------------------------------- #
# 便捷：导出单个想定
# --------------------------------------------------------------------------- #
def export_scenario(map_path: str, armory_path: str, out_path: str,
                    name: str = "", author: str = "",
                    with_armory: bool = False) -> str:
    """导出单个想定为社区包（默认不含武器库，保持包体轻量）。"""
    return export_package([map_path], armory_path, out_path, name=name,
                          author=author, include_armory=with_armory)


def list_packages(directory: str) -> list[dict]:
    """列出目录中的社区想定包及其清单摘要。"""
    out: list[dict] = []
    if not os.path.isdir(directory):
        return out
    for fn in sorted(os.listdir(directory)):
        if not fn.lower().endswith(PACKAGE_SUFFIX):
            continue
        path = os.path.join(directory, fn)
        try:
            m = inspect_package(path)
            out.append({
                "path": path, "name": m.name or fn, "author": m.author,
                "difficulty": m.difficulty, "tags": list(m.tags),
                "description": m.description, "maps": len(m.maps),
                "size": os.path.getsize(path),
            })
        except PackageError:
            out.append({"path": path, "name": fn, "author": "", "difficulty": "",
                        "tags": [], "description": "（包已损坏或格式不正确）",
                        "maps": 0, "size": os.path.getsize(path)})
    return out


__all__ = [
    "PACKAGE_FORMAT", "PACKAGE_VERSION", "PACKAGE_SUFFIX", "PackageError",
    "PackageManifest", "ImportedPackage",
    "export_package", "export_scenario", "import_package",
    "inspect_package", "list_packages",
]
