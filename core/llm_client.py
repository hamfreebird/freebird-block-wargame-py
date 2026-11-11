"""本地大模型运行时：托管一个 ``llama-server`` 进程，用 HTTP 调它。

为什么不是 ``llama-cpp-python``：LFM3 是新架构，PyPI 上的预编译轮子
（0.2.x）带的是 2024 年的 llama.cpp，不认识 ``lfm3`` 这个 arch；
而本机从源码编译需要 MSVC 工具链。官方发布的 Windows CPU 包是自带的
可执行文件，25MB，解压即用——比让玩家装编译环境现实得多。

为什么不是「进程内推理」：1.7B Q4 模型常驻约 1.4GB 内存，推理时吃满
CPU。放进独立进程有三个好处：
1. 推理崩了不会带走游戏进程；
2. 关掉游戏时一个 ``terminate`` 就干净收场，不用担心线程卡在解码里；
3. 玩家想自己起一个 llama-server（或用别的兼容端点）时，填个 URL 就能接上。

本模块只负责「把服务跑起来 / 发一条请求 / 收摊」，不含任何兵棋知识。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import paths

#: 没有可用的运行时/模型时的统一状态
STATUS_OFF = "off"
STATUS_BOOTING = "booting"
STATUS_READY = "ready"
STATUS_ERROR = "error"

#: 候选模型文件名（按优先级）。玩家的模型文件名带哈希，只能按前缀找。
_MODEL_HINTS = ("lfm3", "lfm", "model")

_SERVER_NAMES = ("llama-server.exe", "llama-server")


class LLMError(RuntimeError):
    """运行时不可用或请求失败。调用方应当降级，而不是把异常抛给玩家。"""


def _free_port() -> int:
    """拿一个空闲端口。

    先绑 0 再关掉存在极小的竞争窗口，但对「本机单人游戏」够用；
    真要严谨就得让 llama-server 支持 port=0 并回报实际端口，它没有。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


#: Windows Job Object 的「最后一个句柄关闭时杀掉组内所有进程」标志。
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


def _kill_with_parent(pid: int):
    """把子进程放进一个「随父进程消亡」的 Job Object（仅 Windows）。

    **为什么需要它**：正常退出时 `App.run()` 会调 `stop()`，但如果是崩溃、
    任务管理器结束进程、或者任何没走到那行代码的退出，`llama-server`
    会继续占着 1.4GB 内存——玩家只会看到「关了游戏内存没降」。
    放进 Job Object 之后，父进程一消失，操作系统直接回收整组进程，
    与我们有没有写完清理代码无关。

    失败（例如本进程已被别的启动器放进某个 Job）时静默返回 None：
    宁可退化到「可能残留」，也不要因为清理机制装不上就拒绝启动。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                        ("WriteOperationCount", ctypes.c_ulonglong),
                        ("OtherOperationCount", ctypes.c_ulonglong),
                        ("ReadTransferCount", ctypes.c_ulonglong),
                        ("WriteTransferCount", ctypes.c_ulonglong),
                        ("OtherTransferCount", ctypes.c_ulonglong)]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
                job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return None
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE 足够做归属，不用 ALL_ACCESS
        handle = kernel32.OpenProcess(0x0100 | 0x0001, False, pid)
        if not handle:
            kernel32.CloseHandle(job)
            return None
        ok = kernel32.AssignProcessToJobObject(job, handle)
        kernel32.CloseHandle(handle)
        if not ok:
            kernel32.CloseHandle(job)
            return None
        return job          # 句柄必须由调用方持有：句柄一关，组内进程就被杀
    except Exception:        # noqa: BLE001 - 清理机制装不上不该阻断功能
        return None


def _candidate_dirs() -> list[str]:
    """按优先级列出可能存放 ``ai/`` 的目录。

    覆盖三种形态：源码运行（项目根）、冻结后的可执行文件（exe 同级）、
    zipapp（数据被释放到用户目录，模型则要求玩家放在启动目录旁边）。
    """
    out: list[str] = []
    env = os.environ.get("BLOCKWARGAME_LLM_DIR", "").strip()
    if env:
        out.append(os.path.abspath(os.path.expanduser(env)))
    out.append(os.path.join(paths.PROJECT_ROOT, "ai"))
    if getattr(sys, "frozen", False):
        out.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "ai"))
    out.append(os.path.join(os.getcwd(), "ai"))
    # 去重但保持顺序
    seen: set[str] = set()
    uniq: list[str] = []
    for d in out:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def discover_model(preferred: str = "") -> str:
    """找一个可用的 GGUF 模型文件；找不到返回空串。

    ``preferred`` 可以是文件，也可以是目录（玩家在配置里指了模型目录）。
    """
    if preferred and os.path.isfile(preferred):
        return preferred
    dirs = list(_candidate_dirs())
    if preferred and os.path.isdir(preferred):
        dirs.insert(0, preferred)
    for d in dirs:
        if not os.path.isdir(d):
            continue
        try:
            names = [n for n in os.listdir(d) if n.lower().endswith(".gguf")]
        except OSError:
            continue
        if not names:
            continue
        names.sort(key=lambda n: (not n.lower().startswith("lfm"), n))
        return os.path.join(d, names[0])
    return ""


def discover_server(preferred: str = "") -> str:
    """找 ``llama-server`` 可执行文件；找不到返回空串。"""
    if preferred and os.path.isfile(preferred):
        return preferred
    dirs = list(_candidate_dirs())
    if preferred and os.path.isdir(preferred):
        dirs.insert(0, preferred)
    for d in dirs:
        # 用元组而不是 "runtime/llama" 这种带斜杠的字符串：
        # 在 Windows 上拼出来的路径会混用两种分隔符，虽然能用，
        # 但和调用方手里的路径字符串对不上（测试与日志都会因此困惑）。
        for sub in (("runtime", "llama"), ("runtime",), ("llama",), ()):
            base = os.path.join(d, *sub) if sub else d
            for name in _SERVER_NAMES:
                p = os.path.join(base, name)
                if os.path.isfile(p):
                    return p
    # 最后看 PATH（有的玩家用 scoop/choco 装过 llama.cpp）
    from shutil import which
    for name in _SERVER_NAMES:
        p = which(name)
        if p:
            return p
    return ""


@dataclass
class LLMConfig:
    """运行时参数。

    ``server_url`` 非空时**不启动子进程**，直接连玩家已有的服务——
    省内存，也方便想把模型跑在另一台机器上的人。
    """

    model_path: str = ""
    server_path: str = ""
    server_url: str = ""
    threads: int = max(2, (os.cpu_count() or 4) - 4)
    context: int = 2048
    temperature: float = 0.15
    max_tokens: int = 96
    #: 单条请求的总超时（含排队）。1.7B 在 6 线程上大约 9 秒出 40 个 token，
    #: 留足余量，但也不能无限等——超时就该退回启发式，别让玩家干等。
    request_timeout: float = 45.0
    boot_timeout: float = 90.0


class LLMClient:
    """``llama-server`` 的薄封装：起进程、探活、发请求、收摊。"""

    def __init__(self, cfg: LLMConfig | None = None, log_path: str = ""):
        self.cfg = cfg or LLMConfig()
        self.status = STATUS_OFF
        self.last_error = ""
        self.model_path = self.cfg.model_path
        self.server_path = self.cfg.server_path
        self.port = 0
        self._proc: subprocess.Popen | None = None
        # RLock 而不是 Lock：start() 在异常路径里会调用 _kill()，
        # 那时锁还在自己手上——普通 Lock 在这里会自锁死。
        self._lock = threading.RLock()
        #: 关游戏时置位，让还在等健康检查的线程立刻收手
        self._stopping = threading.Event()
        #: Windows Job Object 句柄（见 `_kill_with_parent`）；非 Windows 为 None
        self._job = None
        self._log_path = log_path or os.path.join(paths.user_writable_dir(),
                                                  "llm-server.log")
        self._log_fh = None

    # ------------------------------------------------------------------ #
    # 可用性
    # ------------------------------------------------------------------ #
    def probe(self) -> bool:
        """只做静态检查（文件在不在），不启动进程。设置面板用它决定是否可选项。"""
        if self.cfg.server_url:
            return True
        self.model_path = discover_model(self.cfg.model_path)
        self.server_path = discover_server(self.cfg.server_path)
        return bool(self.model_path and self.server_path)

    def describe(self) -> str:
        """给界面看的一句话状态。"""
        if self.cfg.server_url:
            return f"外部服务 {self.cfg.server_url}"
        model = os.path.basename(self.model_path) if self.model_path else "（未找到模型）"
        return f"{model} · {self.cfg.threads} 线程"

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> bool:
        """启动（或连上）服务。幂等；失败返回 False 并把原因记进 ``last_error``。"""
        # 用非阻塞抢锁而不是 `with`：另一个线程正在起服务时，调用方
        # （主线程的 prefetch）应当立刻返回「还没好」，而不是排队等 90 秒。
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self.status == STATUS_READY:
                return True
            if self.status == STATUS_BOOTING:
                return False
            self.status = STATUS_BOOTING
            self._stopping.clear()
            self.last_error = ""
            try:
                if self.cfg.server_url:
                    self._base = self.cfg.server_url.rstrip("/")
                else:
                    self._spawn()
            except LLMError as exc:
                self.last_error = str(exc)
                self.status = STATUS_ERROR
                self._kill()
                return False
            except Exception as exc:                 # noqa: BLE001 - 兜底：绝不让 UI 崩
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.status = STATUS_ERROR
                self._kill()
                return False
        finally:
            self._lock.release()

        # 等就绪放在锁外：起服务要几秒到几十秒，握着锁等会让「关游戏」
        # 卡在 stop() 上——玩家点了退出却要等模型加载完，这不能接受。
        try:
            self._wait_health(self.cfg.boot_timeout)
        except LLMError as exc:
            self.last_error = str(exc)
            self.status = STATUS_ERROR
            self._kill()
            return False
        self.status = STATUS_READY
        return True

    def _spawn(self) -> None:
        self.model_path = discover_model(self.cfg.model_path)
        self.server_path = discover_server(self.cfg.server_path)
        if not self.model_path:
            raise LLMError("未找到 GGUF 模型（应放在 ai/ 目录下）")
        if not self.server_path:
            raise LLMError("未找到 llama-server 可执行文件（应放在 ai/runtime/llama/）")

        self.port = _free_port()
        self._base = f"http://127.0.0.1:{self.port}"
        args = [
            self.server_path,
            "-m", self.model_path,
            "-c", str(self.cfg.context),
            "-t", str(self.cfg.threads),
            "--port", str(self.port),
            "--host", "127.0.0.1",
            "--jinja",                 # 用模型自带的 chat template
            "--no-webui",              # 不需要网页界面，少开一个端口
        ]
        try:
            os.makedirs(os.path.dirname(self._log_path), exist_ok=True)
            self._log_fh = open(self._log_path, "w", encoding="utf-8", errors="replace")
        except OSError:
            self._log_fh = None
        # CREATE_NO_WINDOW：否则 Windows 上会弹出一个黑框，玩家会以为程序出错
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self._proc = subprocess.Popen(
                args, stdout=self._log_fh or subprocess.DEVNULL,
                stderr=subprocess.STDOUT if self._log_fh else subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, creationflags=flags,
            )
        except OSError as exc:
            raise LLMError(f"启动推理服务失败：{exc}") from None
        # 即使本进程被强杀，也让操作系统回收这个子进程
        self._job = _kill_with_parent(self._proc.pid)
        if self._stopping.is_set():        # 起进程的同时玩家已经退出
            self._kill()
            raise LLMError("启动过程被取消")

    def _wait_health(self, timeout: float) -> None:
        deadline = time.time() + max(5.0, timeout)
        while time.time() < deadline:
            if self._stopping.is_set():
                raise LLMError("启动过程被取消")
            if self._proc is not None and self._proc.poll() is not None:
                raise LLMError("推理服务启动后立即退出：" + self._log_tail())
            try:
                with urllib.request.urlopen(self._base + "/health", timeout=2) as r:
                    body = r.read(200)
                if b"ok" in body:
                    return
            except (urllib.error.URLError, OSError, TimeoutError):
                pass
            time.sleep(0.4)
        raise LLMError(f"推理服务 {timeout:.0f}s 内未就绪：" + self._log_tail())

    def _log_tail(self, n: int = 3) -> str:
        """取启动日志最后几行——服务起不来时，这是唯一能告诉玩家「为什么」的东西。"""
        try:
            with open(self._log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = [ln.strip() for ln in f.readlines() if ln.strip()]
            return " / ".join(lines[-n:])[:300]
        except OSError:
            return "（无日志）"

    def stop(self) -> None:
        # 先置位再夺锁：若另一线程正卡在健康检查里（锁已释放），
        # 它会立刻看到这个标志并退出循环。
        self._stopping.set()
        self._kill()
        self.status = STATUS_OFF

    def _kill(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            fh, self._log_fh = self._log_fh, None
            job, self._job = self._job, None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
        if job is not None:
            # 关掉 Job 句柄 = 组内进程一并结束。放在 terminate 之后是保险：
            # 万一 terminate 卡住，关句柄也能兜底。
            try:
                import ctypes
                ctypes.WinDLL("kernel32").CloseHandle(job)
            except Exception:              # noqa: BLE001
                pass
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    # 请求
    # ------------------------------------------------------------------ #
    def chat_json(self, system: str, user: str, schema: dict) -> dict:
        """发一条受 JSON Schema 约束的对话请求，返回解析后的对象。

        Schema 约束交给 llama.cpp 的语法采样（GBNF），而不是「让模型自己乖」——
        1.7B 的自由格式输出经常多一句解释、少一个引号，事后正则修补是无底洞。
        """
        if self.status != STATUS_READY:
            raise LLMError("推理服务未就绪")
        payload = {
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": self.cfg.temperature,
            "top_p": 0.9,
            "max_tokens": self.cfg.max_tokens,
            "response_format": {"type": "json_object", "schema": schema},
            # 这个模型的 chat template 默认开思考链，会把 token 预算全烧在
            # 推理块上、正文返回空串。兵棋这一层要的是「选一个决心」，不是长考。
            "chat_template_kwargs": {"enable_thinking": False},
        }
        req = urllib.request.Request(
            self._base + "/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.request_timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise LLMError(f"推理请求失败：HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise LLMError(f"推理请求超时或中断：{exc}") from None
        except json.JSONDecodeError:
            raise LLMError("推理服务返回的不是 JSON") from None
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError("推理结果结构异常") from None
        try:
            obj = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            raise LLMError("模型输出不是合法 JSON") from None
        if not isinstance(obj, dict):
            raise LLMError("模型输出不是 JSON 对象")
        return obj


__all__ = [
    "LLMClient", "LLMConfig", "LLMError", "STATUS_BOOTING", "STATUS_ERROR",
    "STATUS_OFF", "STATUS_READY", "discover_model", "discover_server",
]
