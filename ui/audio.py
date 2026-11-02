"""音效系统：程序化合成战斗音效。

为什么不用外部音频文件
----------------------
外部 `.wav` 素材会带来分发、体积、许可证与「资源丢失」的麻烦。
本作的音效全部用**波形合成**——用数学函数直接生成采样：

* 无需任何素材文件，打包后开箱即用；
* 体积为零（几百字节代码 vs 几百 KB 音频）；
* 参数可调（频率、包络、噪声占比），便于做变体；
* 声音风格统一，不会出现风格割裂。

音效清单
--------
| 名称 | 触发时机 | 合成方式 |
|---|---|---|
| `detect` | 探测到新目标 | 短促正弦双音（上行） |
| `fire` | 开火 | 噪声 + 下滑正弦 |
| `intercept` | 拦截成功 | 高频方波双脉冲 |
| `explode` | 命中 / 击毁 | 低频噪声爆裂 + 衰减 |
| `alarm` | 敌方进入射程 | 交替方波警报 |
| `click` | 界面点击 | 极短脉冲 |
| `move` | 单位机动 | 快速上滑 |
| `victory` | 战斗胜利 | 上行三音琶音 |
| `defeat` | 战斗失败 | 下行三音琶音 |

混音器不可用时（无声卡 / CI 环境），整个模块静默降级——绝不抛异常。
"""
from __future__ import annotations

import math
import os
import random

import pygame

SAMPLE_RATE = 22050

# 音效时长（秒）——公开为 SOUND_DUR，便于自检脚本核对波形长度
SOUND_DUR = {
    "detect": 0.10,
    "fire": 0.22,
    "intercept": 0.16,
    "explode": 0.45,
    "alarm": 0.42,
    "click": 0.045,
    "move": 0.12,
    "victory": 0.70,
    "defeat": 0.80,
}

ALL_SOUNDS = tuple(SOUND_DUR.keys())


def _audio_disabled_by_env() -> bool:
    """环境变量是否禁止播放音频。

    `BLOCKWARGAME_NO_AUDIO=1` —— 用于 CI 容器与无头环境：
    配合 `SDL_AUDIODRIVER=dummy` 使用，SDL 不会去访问真实声卡
    （否则会刷出大量 ALSA 报错）。

    **只禁播放，不禁合成**：音效的波形合成与 Sound 构造仍会完整执行，
    这样 CI 依然能验证音效逻辑，只是不真正出声。
    """
    val = os.environ.get("BLOCKWARGAME_NO_AUDIO", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _envelope(i: int, n: int, attack: float = 0.02, release: float = 0.6) -> float:
    """包络函数：快速起音 + 指数衰减。"""
    t = i / max(1, n)
    a = attack
    if t < a:
        return t / a
    return math.exp(-release * 10.0 * (t - a) / max(1e-6, 1.0 - a))


def _sine(freq: float, t: float) -> float:
    return math.sin(math.tau * freq * t)


def _square(freq: float, t: float) -> float:
    return 1.0 if math.sin(math.tau * freq * t) >= 0 else -1.0


def _render(name: str) -> pygame.Sound | None:
    """合成单个音效。"""
    data = render_wave(name)
    if data is None:
        return None
    try:
        return pygame.mixer.Sound(buffer=data)
    except Exception:
        return None


def render_wave(name: str) -> bytes | None:
    """把音效名合成为 16 位单声道 PCM 数据。

    **不依赖混音器**——CI 容器与无声卡机器上也能验证合成逻辑本身。
    返回 None 表示音效名未知。
    """
    buf = _synth(name)
    if buf is None:
        return None
    # 归一化并转成 16 位整数
    peak = max((abs(v) for v in buf), default=1.0)
    if peak > 0.0:
        buf = [v / peak for v in buf]
    return b"".join(int(max(-1.0, min(1.0, v)) * 22000).to_bytes(2, "little", signed=True)
                    for v in buf)


def _synth(name: str) -> list[float] | None:
    """按名字合成波形（归一化前的浮点采样）。"""
    dur = SOUND_DUR.get(name)
    if dur is None:
        return None
    n = int(SAMPLE_RATE * dur)
    if n <= 0:
        return None

    buf = [0.0] * n
    rng = random.Random(hash(name) & 0xFFFF)

    if name == "detect":
        # 上行双音：轻微的「滴」声
        for i in range(n):
            t = i / SAMPLE_RATE
            f = 880.0 if t < dur * 0.5 else 1320.0
            buf[i] = _sine(f, t) * _envelope(i, n, 0.05, 1.6) * 0.35

    elif name == "fire":
        # 噪声爆发 + 下滑正弦：炮口冲击波
        for i in range(n):
            t = i / SAMPLE_RATE
            f = 320.0 - 200.0 * (i / n)
            noise = rng.uniform(-1.0, 1.0)
            buf[i] = (noise * 0.55 + _sine(f, t) * 0.45) * _envelope(i, n, 0.005, 1.2) * 0.5

    elif name == "intercept":
        # 高频方波双脉冲：雷达锁定被截获
        for i in range(n):
            t = i / SAMPLE_RATE
            gate = 1.0 if (t < dur * 0.35 or (dur * 0.5 < t < dur * 0.85)) else 0.0
            buf[i] = _square(2100.0, t) * gate * _envelope(i, n, 0.01, 2.0) * 0.22

    elif name == "explode":
        # 低频噪声爆裂 + 隆隆尾音
        last = 0.0
        for i in range(n):
            t = i / SAMPLE_RATE
            noise = rng.uniform(-1.0, 1.0)
            # 一阶低通让噪声更接近实爆
            last = last * 0.72 + noise * 0.28
            rumble = _sine(58.0 - 24.0 * (i / n), t) * 0.5
            buf[i] = (last * 0.75 + rumble) * _envelope(i, n, 0.004, 0.9) * 0.62

    elif name == "alarm":
        # 交替方波：敌方逼近警报
        for i in range(n):
            t = i / SAMPLE_RATE
            f = 740.0 if int(t / 0.09) % 2 == 0 else 560.0
            buf[i] = _square(f, t) * _envelope(i, n, 0.02, 0.5) * 0.2

    elif name == "click":
        # 极短点击
        for i in range(n):
            t = i / SAMPLE_RATE
            buf[i] = _square(1600.0, t) * _envelope(i, n, 0.005, 4.0) * 0.16

    elif name == "move":
        # 快速上滑：单位移动
        for i in range(n):
            t = i / SAMPLE_RATE
            f = 300.0 + 500.0 * (i / n)
            buf[i] = _sine(f, t) * _envelope(i, n, 0.02, 1.8) * 0.26

    elif name == "victory":
        # 上行三音琶音
        notes = (523.25, 659.25, 783.99)
        step = n // len(notes)
        for i in range(n):
            t = i / SAMPLE_RATE
            idx = min(len(notes) - 1, i // max(1, step))
            buf[i] = (_sine(notes[idx], t) * 0.7 + _sine(notes[idx] * 2, t) * 0.3) \
                * _envelope(i, n, 0.02, 0.55) * 0.34

    elif name == "defeat":
        # 下行三音琶音
        notes = (523.25, 392.00, 261.63)
        step = n // len(notes)
        for i in range(n):
            t = i / SAMPLE_RATE
            idx = min(len(notes) - 1, i // max(1, step))
            buf[i] = (_sine(notes[idx], t) * 0.7 + _sine(notes[idx] * 1.5, t) * 0.3) \
                * _envelope(i, n, 0.02, 0.5) * 0.32

    else:
        return None
    return buf


class AudioManager:
    """音效管理器：合成、缓存、播放、音量控制。

    混音器不可用时全部方法安全降级（不报错、不阻塞）。

    环境变量 `BLOCKWARGAME_NO_AUDIO=1` 可强制禁用音频——CI 容器与无头
    环境下 SDL 若去访问真实声卡，会刷出大量 ALSA 报错并拖慢测试。
    """

    def __init__(self, enabled: bool = True, volume: float = 0.6):
        self.enabled = enabled
        self.volume = max(0.0, min(1.0, volume))
        self._cache: dict[str, pygame.Sound] = {}
        self.available = False
        self.muted_by_env = _audio_disabled_by_env()
        self._init_mixer()

    def _init_mixer(self) -> None:
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=SAMPLE_RATE, size=-16, channels=1,
                                  buffer=512)
            self.available = pygame.mixer.get_init() is not None
        except Exception:
            self.available = False

    # -- 设置 -------------------------------------------------------------
    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def set_volume(self, volume: float) -> None:
        self.volume = max(0.0, min(1.0, float(volume)))
        self._apply_volume()

    def _apply_volume(self) -> None:
        for snd in self._cache.values():
            try:
                snd.set_volume(self.volume)
            except Exception:
                pass

    # -- 播放 -------------------------------------------------------------
    def play(self, name: str) -> None:
        """播放音效。未知名称、静音或混音器不可用时静默返回。

        环境变量禁止播放时（CI 无声卡环境）仍然合成并缓存 Sound——
        只是不真正调用播放，这样音效合成路径在 CI 里依然被完整覆盖。
        """
        if not self.enabled or not self.available:
            return
        snd = self._cache.get(name)
        if snd is None:
            snd = _render(name)
            if snd is None:
                return
            try:
                snd.set_volume(self.volume)
            except Exception:
                pass
            self._cache[name] = snd
        if self.muted_by_env:
            return
        try:
            snd.play()
        except Exception:
            pass

    def stop_all(self) -> None:
        try:
            pygame.mixer.stop()
        except Exception:
            pass

    def preload(self) -> int:
        """预合成全部音效（避免首次播放时的合成延迟）。返回成功数量。"""
        if not self.available:
            return 0
        ok = 0
        for name in ALL_SOUNDS:
            snd = self._cache.get(name)
            if snd is None:
                snd = _render(name)
                if snd is None:
                    continue
                try:
                    snd.set_volume(self.volume)
                except Exception:
                    pass
                self._cache[name] = snd
            ok += 1
        return ok

    @property
    def cached_count(self) -> int:
        return len(self._cache)


__all__ = ["AudioManager", "ALL_SOUNDS", "SAMPLE_RATE", "SOUND_DUR", "render_wave"]
