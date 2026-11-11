"""下载 / 检查本地推理运行时（llama.cpp）。

为什么单独做个脚本：玩家要跑「大模型指挥官」，需要两样东西——模型
（1.1GB，自己放）和 llama.cpp 的 Windows/Linux 可执行文件（25MB，
官方发布包）。前者只能由玩家自己下载，后者可以被脚本代劳。

用法：
    python tools/get_llm_runtime.py           # 下载并解压
    python tools/get_llm_runtime.py --check   # 只检查现状
    python tools/get_llm_runtime.py --force   # 重新下载

版本是**钉死**的：换 llama.cpp 版本可能改变采样细节，而玩家的战报
对不上时，第一件要排除的就是「跑的是不是同一个运行时」。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import zipfile

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import llm_client  # noqa: E402

#: 钉死的构建号与下载地址。与 ai/README.md 写的必须是同一个。
BUILD = "b12277"
ASSET = f"llama-{BUILD}-bin-win-cpu-x64.zip"
URL = f"https://github.com/ggml-org/llama.cpp/releases/download/{BUILD}/{ASSET}"
#: 直连 GitHub 在国内经常只有几十 KB/s，备两个镜像按顺序试。
MIRRORS = ("https://gh-proxy.com/", "https://ghfast.top/")

DEST = os.path.join(ROOT, "ai", "runtime")
TARGET = os.path.join(DEST, "llama")


def _download(url: str, dst: str) -> bool:
    """下载到 dst；成功返回 True。用标准库，免得给这个可选功能加依赖。"""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "block-wargame-setup"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(dst, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total:
                    print(f"\r  下载中 {got / 1e6:5.1f} / {total / 1e6:.1f} MB", end="")
                else:
                    print(f"\r  下载中 {got / 1e6:5.1f} MB", end="")
        print()
        return True
    except Exception as exc:                    # noqa: BLE001 - 网络问题千奇百怪
        print(f"\n  失败：{type(exc).__name__}: {exc}")
        return False


def fetch(force: bool = False) -> int:
    if os.name != "nt":
        print("本脚本只自动下载 Windows x64 运行时。")
        print("Linux / macOS 请用发行版包管理器安装 llama.cpp，")
        print("再把 llama-server 放到 ai/runtime/llama/ 或 PATH 里。")
        return 1
    exe = os.path.join(TARGET, "llama-server.exe")
    if os.path.isfile(exe) and not force:
        print(f"运行时已存在：{exe}")
        print("要重新下载加 --force。")
        return 0

    os.makedirs(DEST, exist_ok=True)
    zip_path = os.path.join(DEST, ASSET)
    for prefix in ("", *MIRRORS):
        url = prefix + URL
        print(f"从 {prefix or 'github.com'} 下载 {ASSET} …")
        if _download(url, zip_path):
            break
    else:
        print("所有下载源都失败了。可手动下载后解压到 ai/runtime/llama/：")
        print(f"  {URL}")
        return 1

    try:
        with zipfile.ZipFile(zip_path) as z:
            if not any(n.endswith("llama-server.exe") for n in z.namelist()):
                print("压缩包里没有 llama-server.exe，可能下到了错误文件。")
                return 1
            os.makedirs(TARGET, exist_ok=True)
            z.extractall(TARGET)
    except (zipfile.BadZipFile, OSError) as exc:
        print(f"解压失败：{exc}")
        return 1
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass

    print(f"已解压到 {TARGET}")
    return 0


def check() -> int:
    model = llm_client.discover_model("")
    server = llm_client.discover_server("")
    print("=== 本地大模型资源 ===")
    if model:
        size = os.path.getsize(model) / 1e9
        print(f"  模型    OK  {model}（{size:.2f} GB）")
    else:
        print("  模型    缺失  把 .gguf 放到 ai/ 目录下（见 ai/README.md）")
    if server:
        print(f"  运行时  OK  {server}")
    else:
        print("  运行时  缺失  运行 python tools/get_llm_runtime.py 下载")
    if model and server:
        cfg = llm_client.LLMConfig()
        client = llm_client.LLMClient(cfg)
        print(f"  线程    {cfg.threads}（CPU {os.cpu_count()} 核）")
        print("\n两样都齐了：游戏内 设置 → AI 指挥官 → 大模型")
        return 0
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="准备本地推理运行时（llama.cpp）")
    ap.add_argument("--check", action="store_true", help="只检查，不下载")
    ap.add_argument("--force", action="store_true", help="已存在也重新下载")
    args = ap.parse_args()
    if args.check:
        return check()
    code = fetch(force=args.force)
    if code == 0:
        check()
    return code


if __name__ == "__main__":
    sys.exit(main())
