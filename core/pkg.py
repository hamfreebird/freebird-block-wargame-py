"""社区想定包：把想定打包成一个 zip，方便分发与导入。

一个 `.bwscn` 包（本质是 zip）内含：

```
package.json          包清单：格式版本、名称、作者、游戏版本、文件校验和
maps/<id>.json        想定地图（含部署、脚本事件、规则与元数据）
armory.json           武器覆盖（可选）：只写与内置库不同的部分
README.md             说明（可选）
```

导入时**只覆盖**包内声明的武器条目，不影响用户的其它自定义兵种——
这样社区包既能提供"新武器"，也不会破坏用户已有的编辑。

**格式版本 2（v3.2）**加了 `game_version` 与逐文件 sha256：

* `game_version` 回答「这个包是哪个版本做的」——导入时能明确告诉玩家
  是「包太新」还是「包偏旧」，而不是笼统一句「版本不符」；
* `sha256` 让「传坏了」与「本来就不对」区分得开：校验失败说明文件在
  传输中损坏，而不是内容有问题。

版本 1 的包仍然能读（没有这两项就跳过校验），导出时一律写版本 2。
"""
from __future__ import annotations

import hashlib
import json
import os
import zipfile
from dataclasses import dataclass, field
from typing import Any

from .version import GAME_VERSION, compat_note

PACKAGE_FORMAT = "bwscn"
PACKAGE_VERSION = 2
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
    #: 制作该包的程序版本（v2 起）。空 = 版本 1 的旧包，无法判断
    game_version: str = ""
    #: 逐文件校验和：[{name, role, sha256, size}]（v2 起）
    files: list[dict] = field(default_factory=list)

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
            "game_version": self.game_version,
            "files": [dict(f) for f in self.files],
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
            raise PackageError(
                f"想定包格式版本 {ver} 高于当前程序支持的 {PACKAGE_VERSION}，"
                f"请更新游戏后再导入")
        maps = d.get("maps", [])
        if not isinstance(maps, list):
            raise PackageError("清单格式错误：maps 必须是数组")
        tags = d.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        files = d.get("files", [])
        if not isinstance(files, list):
            files = []
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
            game_version=str(d.get("game_version", "")),
            files=[f for f in files if isinstance(f, dict)],
        )

    # -- 兼容性 ---------------------------------------------------------
    def compat_note(self) -> str:
        """人话版兼容性说明（同版本 / 旧格式包返回空串）。"""
        if not self.game_version:
            return ("该包由 v3.2 之前的版本制作，未记录游戏版本；"
                    "若导入后想定显示不正常，多半是版本差异所致")
        return compat_note(self.game_version)

    def checksum_of(self, name: str) -> str:
        """取某个包内文件的 sha256；没有记录时返回空串。"""
        for f in self.files:
            if str(f.get("name", "")) == name:
                return str(f.get("sha256", ""))
        return ""


@dataclass
class ImportedPackage:
    """导入结果。"""

    manifest: PackageManifest
    maps_dir: str = ""           # 导入后的地图目录
    added_maps: list[str] = field(default_factory=list)    # 导入的地图文件名
    armory_units: list[dict] = field(default_factory=list)   # 导入的武器覆盖
    added_templates: list[str] = field(default_factory=list)  # 新增的兵种 id
    #: 需要提示玩家的话（版本差异、覆盖了同名想定等）
    notes: list[str] = field(default_factory=list)
    #: 被覆盖的同名想定文件名
    replaced: list[str] = field(default_factory=list)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


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
        game_version=GAME_VERSION,
    )

    def _add(z: zipfile.ZipFile, arc: str, data: bytes, role: str) -> None:
        """写一个包内文件，并在清单里登记校验和。"""
        z.writestr(arc, data, compress_type=zipfile.ZIP_DEFLATED)
        manifest.files.append({
            "name": arc,
            "role": role,
            "sha256": sha256_bytes(data),
            "size": len(data),
        })

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for mp in map_paths:
            if not os.path.isfile(mp):
                raise PackageError(f"想定文件不存在：{mp}")
            base = os.path.basename(mp)
            arc = f"maps/{base}"
            with open(mp, "rb") as f:
                data = f.read()
            _add(z, arc, data, "map")
            manifest.maps.append(base)
            # 提取元数据到清单，便于浏览
            try:
                raw = json.loads(data.decode("utf-8"))
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
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
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
            _add(z, "armory.json",
                 json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"),
                 "armory")

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

    写入磁盘**之前**先校验校验和与 JSON 结构：传输中损坏的包应该在
    落地前被拦住，而不是先覆盖掉同名的好想定再报错。
    """
    manifest = inspect_package(path)
    os.makedirs(maps_target, exist_ok=True)

    added: list[str] = []
    replaced: list[str] = []
    units: list[dict] = []
    notes: list[str] = []
    try:
        with zipfile.ZipFile(path, "r") as z:
            names = z.namelist()
            # 先整体读进内存并校验，再统一落盘
            payloads: list[tuple[str, bytes]] = []
            for arc in manifest.maps:
                # 清单里存的是文件名，包内路径为 maps/<文件名>；两者都接受
                cand = arc if arc in names else f"maps/{os.path.basename(arc)}"
                if cand not in names:
                    raise PackageError(f"清单声明的地图 '{arc}' 不在包内")
                with z.open(cand) as f:
                    data = f.read()
                _verify(manifest, cand, data)
                _validate_map_blob(data, os.path.basename(cand))
                payloads.append((os.path.basename(cand), data))

            for base, data in payloads:
                target = os.path.join(maps_target, base)
                if os.path.isfile(target):
                    replaced.append(base)
                with open(target, "wb") as fdst:
                    fdst.write(data)
                added.append(base)

            if "armory.json" in names:
                with z.open("armory.json") as f:
                    raw = f.read()
                _verify(manifest, "armory.json", raw)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise PackageError(f"包内 armory.json 不是合法 JSON：{exc}") from None
                if not isinstance(payload, dict):
                    raise PackageError("包内 armory.json 格式错误：应为对象")
                units = payload.get("units", [])
                if merge_armory:
                    _merge_armory(armory_path, payload)
    except PackageError:
        raise
    except zipfile.BadZipFile:
        raise PackageError("想定包已损坏（zip 读取失败）") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageError(f"导入失败：{exc}") from None

    note = manifest.compat_note()
    if note:
        notes.append(note)
    if replaced:
        notes.append("覆盖了同名想定：" + "、".join(replaced))
    if units and armory_path and os.path.isfile(armory_path) and not merge_armory:
        notes.append("包内含武器覆盖，但本次未合并（只导入地图）")

    return ImportedPackage(
        manifest=manifest,
        maps_dir=maps_target,
        added_maps=added,
        armory_units=units,
        added_templates=[str(u.get("id", "")) for u in units],
        notes=notes,
        replaced=replaced,
    )


def _verify(manifest: PackageManifest, arc: str, data: bytes) -> None:
    """校验包内文件与清单记录的 sha256 是否一致。

    旧格式包（版本 1）没有记录校验和，跳过——不能因为「没有指纹」
    就把老包判成坏包。
    """
    expect = manifest.checksum_of(arc)
    if not expect:
        return
    actual = sha256_bytes(data)
    if actual != expect:
        raise PackageError(
            f"包内文件 {arc} 校验和不符（期望 {expect[:12]}…，实际 {actual[:12]}…）——"
            f"文件可能在下发/传输中损坏，请重新获取")


def _validate_map_blob(data: bytes, name: str) -> None:
    """落盘前做最小结构校验：能解析、是个带地图与部署的想定。

    深度校验（可达性、目标点、装备引用）仍由 `main.py --check` 负责——
    这里只挡住「显然不是想定」的文件，避免把垃圾写进想定目录。
    """
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageError(f"包内想定 {name} 不是合法 JSON：{exc}") from None
    if not isinstance(raw, dict):
        raise PackageError(f"包内想定 {name} 格式错误：顶层应为对象")
    size = raw.get("size")
    if not (isinstance(size, list) and len(size) == 2):
        raise PackageError(f"包内想定 {name} 缺少 size 字段（应为 [宽, 高]）")
    if not isinstance(raw.get("deployments"), dict):
        raise PackageError(f"包内想定 {name} 缺少 deployments 字段")


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
    "inspect_package", "list_packages", "sha256_bytes", "sha256_file",
]
