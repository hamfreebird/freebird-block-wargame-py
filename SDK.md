# 方块兵棋 · 扩展开发手册（SDK API 参考）

> 适用版本：**v3.2.0** ｜ SDK API 版本：**`core.mods.API_VERSION` = 1.1.0**
>
> 本文面向三类读者：**想定作者**（只写 JSON）、**模组作者**（写 Python）、
> **引擎改造者**（改核心代码）。三条路线的稳定性承诺不同，见 §1.2。

---

## 目录

- [1. 先读这一页](#1-先读这一页)
  - [1.1 三条扩展路线](#11-三条扩展路线)
  - [1.2 稳定性承诺](#12-稳定性承诺)
  - [1.3 版本号一览](#13-版本号一览)
- [2. 数据模型参考](#2-数据模型参考)
  - [2.1 武器库结构](#21-武器库结构)
  - [2.2 武器字段](#22-武器字段)
  - [2.3 传感器字段](#23-传感器字段)
  - [2.4 兵种字段](#24-兵种字段)
  - [2.5 枚举取值](#25-枚举取值)
- [3. 想定文件格式](#3-想定文件格式)
  - [3.1 顶层字段](#31-顶层字段)
  - [3.2 地形与高程](#32-地形与高程)
  - [3.3 部署](#33-部署)
  - [3.4 目标点与胜利条件](#34-目标点与胜利条件)
  - [3.5 元数据](#35-元数据)
  - [3.6 教学脚本](#36-教学脚本)
- [4. 声明式规则](#4-声明式规则)
  - [4.1 事件 events](#41-事件-events)
  - [4.2 规则 rules：条件](#42-规则-rules条件)
  - [4.3 规则 rules：效果](#43-规则-rules效果)
- [5. 模组（Python）](#5-模组python)
  - [5.1 最小模组](#51-最小模组)
  - [5.2 mod.json 清单](#52-modjson-清单)
  - [5.3 注册接口 ModAPI](#53-注册接口-modapi)
  - [5.4 规则钩子](#54-规则钩子)
  - [5.5 本地化注入](#55-本地化注入)
  - [5.6 依赖与版本匹配](#56-依赖与版本匹配)
  - [5.7 错误隔离与调试](#57-错误隔离与调试)
- [6. 命令流与回放 API](#6-命令流与回放-api)
  - [6.1 Command 与 CommandLog](#61-command-与-commandlog)
  - [6.2 命令执行器](#62-命令执行器)
  - [6.3 ReplayFile](#63-replayfile)
  - [6.4 Replayer](#64-replayer)
  - [6.5 观战 SpectatorMatch](#65-观战-spectatormatch)
- [7. 分享格式](#7-分享格式)
  - [7.1 想定包 .bwscn](#71-想定包-bwscn)
  - [7.2 回放 .bwrep](#72-回放-bwrep)
- [8. 界面层接口](#8-界面层接口)
  - [8.1 主题与度量函数](#81-主题与度量函数)
  - [8.2 本地化 tr()](#82-本地化-tr)
  - [8.3 单位图标包](#83-单位图标包)
- [9. 示例与自检](#9-示例与自检)
- [10. 核心模块地图](#10-核心模块地图)
  - [10.1 规则层的九个模块](#101-规则层的九个模块)
  - [10.2 加新机制该放哪](#102-加新机制该放哪)
  - [10.3 两条必须守住的约定](#103-两条必须守住的约定)
  - [10.4 门面的约束](#104-门面的约束)
- [附录 A. 常见错误排查](#附录-a-常见错误排查)
- [附录 B. 版本历史](#附录-b-版本历史)

---

## 1. 先读这一页

### 1.1 三条扩展路线

| 想做什么 | 走哪条路 | 要不要写代码 | 承诺级别 |
|---|---|---|---|
| 改数值 / 加装备 / 加兵种 | 编辑 `armory.json` 或放模组调 `api.add_weapon` | 否 / 少量 | **数据格式**稳定 |
| 新战役 / 新剧本 / 条件规则 | 写想定 JSON 的 `events` / `rules` | 否 | **数据格式**稳定 |
| 加机制、改结算 | 写 Python 模组 | 是 | **SDK API** 稳定 |
| 改引擎内部 | fork 本仓库 | 是 | 无承诺 |

**优先用数据，不要用代码。** 一行 JSON 能做到的事，不要写成模组——
模组要吃 Python 版本、要处理加载顺序、出错时玩家看到的是「模组加载失败」，
而 JSON 写错了 `python main.py --check` 会直接告诉你是第几行第几个字段。

### 1.2 稳定性承诺

`core.mods` 是 SDK 的稳定层，三条承诺：

1. **API 版本号**——`core.mods.API_VERSION` 随不兼容变更递增。
   模组在 `mod.json` 里声明 `"api": ">=1.0.0"`，范围不匹配时**拒绝加载并说明原因**，
   而不是等运行时炸。
2. **公开 API 白名单**——`core.mods.PUBLIC_API` 列出可用的方法名。
   以 `_` 开头的一律是内部实现，可随时变。
3. **弃用有预告**——`core.mods.DEPRECATED` 记录弃用项与移除版本，
   会先警告若干版本再删除。

不在承诺范围内的东西：`core.engine.GameState` 的内部字段、`ui` 包的绘制细节、
任何以下划线开头的方法。**读**它们可以，**写**它们要自己承担升级成本。

### 1.3 版本号一览

每个可交换格式都有自己的版本号，含义不同，不要混：

| 常量 | 当前值 | 管什么 | 升级时的行为 |
|---|---|---|---|
| `core.version.GAME_VERSION` | `3.2.0` | 程序版本 | 提示「文件由更新/更早版本制作」 |
| `core.mods.API_VERSION` | `1.1.0` | 模组 SDK 接口 | 范围不匹配 → 拒绝加载 |
| `core.mod_manifest.MANIFEST_SCHEMA` | `1` | `mod.json` 结构 | 预留 |
| `core.pkg.PACKAGE_VERSION` | `2` | 想定包 `.bwscn` | 高于程序支持 → 拒绝导入 |
| `core.replay.REPLAY_VERSION` | `2` | 回放 `.bwrep` | 高于程序支持 → 拒绝播放 |
| `core.save.SAVE_VERSION` | `6` | 存档 | 低于当前版逐版迁移（`core.save.MIGRATIONS`），高于当前版拒绝 |
| `core.config.CONFIG_VERSION` | 见源码 | 用户配置 | 结构变化时迁移 |

**通用规则：读旧文件必须成功，读新文件必须报错。**
后者比前者重要——猜着读一个未来格式，比直接说「请更新游戏」更糟。

---

## 2. 数据模型参考

### 2.1 武器库结构

```jsonc
{
  "weapons": [ /* 见 2.2 */ ],
  "sensors": [ /* 见 2.3 */ ],
  "units":   [ /* 见 2.4 */ ]
}
```

兵种通过 `ref` 引用武器与传感器，所以三者写在同一个文件里（或同一个模组里），
加载顺序由加载器保证：先武器 / 传感器，再兵种。

自检：`python main.py --check` 会校验引用是否都存在、字段是否合法。

### 2.2 武器字段

`core.loader._parse_weapon` 是唯一权威。**必填**字段缺失会直接拒绝加载。

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `id` | str | ✔ | — | 全局唯一 |
| `name` | str | ✔ | — | 显示名 |
| `kind` | enum | ✔ | — | 见 `WeaponKind` |
| `pk` | float | ✔ | — | 单发命中率 0..1 |
| `damage` | float | ✔ | — | 单发毁伤 |
| `targets` | list[enum] | ✔ | — | 可打的域，见 `Domain` |
| `range` | float | ✔ | — | 最大射程（格）。也可写 `range_max` |
| `range_min` | float | | 0 | 最小射程 |
| `ammo` | int | | 1 | 备弹 |
| `salvo` | int | | 1 | 默认齐射发数 |
| `max_salvo` | int | | `salvo` | 齐射上限 |
| `guidance` | enum | | optical | 见 `Guidance` |
| `speed` | float | | 1.0 | 飞行速度（格 / 回合），影响拦截次数 |
| `interceptable` | bool | | 弹类为真 | 可被防空拦截 |
| `evasion` | float | | 0 | 突防修正 |
| `hypersonic` | bool | | false | 高超音速（压缩拦截窗口） |
| `needs_los` | bool | | false | 需要通视 |
| `min_detection` | enum | | tracked | 开火所需的最低探测等级 |
| `vs_facility` | bool | | 按 kind 推 | 可打击设施 |
| `asat` | bool | | `space ∈ targets` | 反卫星 |
| `stealth` | float | | 0 | 低可探测性 |
| `ciws` | bool | | kind 为 ciws | 近防武器 |
| `anti_fort` | int | | 按 kind 推 | 对工事的破坏力 |
| `strategic` | bool | | false | 战略武器（不计常规交战） |
| `ballistic` | bool | | false | 弹道飞行（可被中段拦截） |
| `midcourse` | bool | | false | 具备中段拦截能力 |
| `smoke` | bool | | false | 烟幕弹（不造成伤害） |
| `real_range_km` | str | | "" | 展示用真实射程 |
| `note` | str | | "" | 展示用说明 |

最小可用武器：

```json
{
  "id": "demo_cannon",
  "name": "演示 30mm 机炮",
  "kind": "gun",
  "targets": ["land", "air"],
  "range": 1.5,
  "pk": 0.45,
  "damage": 12,
  "ammo": 8,
  "salvo": 2
}
```

### 2.3 传感器字段

`core.loader._parse_sensor`。

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `kind` | enum | ✔ | — | 见 `SensorKind` |
| `range` | float | ✔ | — | 探测距离（格） |
| `strength` | float | | 1.0 | 探测强度（对抗干扰） |
| `counter_stealth` | float | | 0.0 | 反隐身能力 |
| `note` | str | | "" | 说明 |

### 2.4 兵种字段

`core.loader._parse_unit`。

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `id` / `name` | str | ✔ | — | 标识与显示名 |
| `class` | enum | ✔ | — | 见 `UnitClass`（25 种） |
| `domain` | enum | ✔ | — | 见 `Domain` |
| `hp` / `mp` | float | ✔ | — | 耐久与每回合机动点 |
| `short` | str | | id 前 4 位 | 棋子缩写（≤6 字符） |
| `armor` | float | | 0 | 装甲 |
| `stealth` / `evasion` | float | | 0 | 隐蔽 / 规避 |
| `weapons` | list | | [] | 字符串引用或 `{"ref": id, ...覆盖}` |
| `sensors` | list | | [] | 同上 |
| `jam_radius` / `jam_strength` | float | | 0 | 干扰半径 / 强度 |
| `jam_bands` | list[str] | | [] | 干扰频段 |
| `jam_band_weights` | dict | | {} | 各频段权重 |
| `interceptors` / `intercept_pk` | int / float | | 0 | 拦截弹数量 / 单发拦截率 |
| `can_capture` | bool | | false | 可占领目标点 |
| `amphibious` | bool | | false | 两栖 |
| `is_radar_emitter` | bool | | 有雷达即真 | 开机即暴露 |
| `max_altitude` | enum | | ground | 见 `Altitude` |
| `cost` | int | | 0 | 价值（AI 目标排序用） |
| `sortie_shots` | int | | 1 | 出击架次 |
| `datalink` | bool | | true | 具备数据链 |
| `decoy_charges` | int | | 0 | 诱饵次数 |
| `entrench_bonus` | float | | 0 | 驻守加成 |
| `is_infantry` | bool | | class 为步兵 | 享受工事加成 |
| `one_shot_kill` | bool | | 空中为真 | 一击必杀判定 |
| `can_fortify` / `fort_power` | bool / int | | 按 class | 可筑工事 / 工事强度 |
| `echelon` | str | | platoon | 编制（platoon / company …） |
| `command_radius` / `command_range` | float | | 0 | 指挥半径 / 通信距离 |
| `notes` | str | | "" | 说明 |

**覆盖写法**（复用既有武器但改几项）：

```json
{
  "id": "demo_ifv",
  "name": "演示步战车",
  "class": "ifv",
  "domain": "land",
  "hp": 90,
  "mp": 3.4,
  "weapons": [
    "demo_cannon",
    { "ref": "demo_atgm", "ammo": 2, "pk": 0.62 }
  ]
}
```

### 2.5 枚举取值

写 JSON 时用**小写英文值**（不是中文名）：

| 枚举 | 取值 |
|---|---|
| `Domain` | land, air, sea, sub, infra, space |
| `WeaponKind` | gun, artillery, mlrs, atgm, sam, aam, asm, land_attack, arm, torpedo, bomb, depth_charge, ciws |
| `SensorKind` | radar, esm, ir, optical, sonar, sat |
| `Guidance` | optical, ir, radar_semi, radar_active, gps_ins, anti_radiation, passive_rf, laser, wire, sonar |
| `Altitude` | ground, low, med, high |
| `Detection` | none, contact, classified, tracked |
| `Terrain` | water, coast, plain, forest, hills, mountain, urban, desert, marsh |
| `Weather` | clear, rain, fog, snow |
| `Facility` | none, airbase, port, radar_site, depot, sam_site, hq, city |
| `ObjectiveKind` | capture, hold, destroy, survive |
| `TimeOfDay` | dawn, day, dusk, night |
| `Side` | BLUE, RED, NEUTRAL |

`UnitClass` 共 25 种：mbt, ifv, infantry, arty, mlrs, sam, aaa, ew, radar, hq,
destroyer, frigate, carrier, sub, fighter, bomber, aew, uav, tanker, satellite,
engineer, heli, attack_heli, transport, amphib。

> 写错了会怎样：加载器抛 `core.loader.LoadError`，消息里带**字段路径**
> （如 `units[12](demo_ifv).domain: 未知取值 'tank'`）。

---

## 3. 想定文件格式

一份想定就是一张地图 + 双方部署 + 若干声明式规则。放在 `data/maps/`
（随游戏分发）或用户数据目录的 `maps/`（自己玩）。

### 3.1 顶层字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | str | | 想定标识，缺省用文件名 |
| `name` | str | | 显示名 |
| `description` | str | | 一句话简介（主菜单卡片显示） |
| `size` | `[w, h]` | ✔ | 4..256 |
| `terrain` | list[str] | ✔ | h 行，每行 w 个地形符号 |
| `elevation` | dict | | `{"rows": [...]}`，h 行 w 个 0-9 数字 |
| `tile_km` | float | | 每格代表多少公里（默认 12） |
| `turn_limit` | int | | 回合上限（到期按胜负点判胜） |
| `deployments` | dict | ✔ | `{"BLUE": [...], "RED": [...]}` |
| `objectives` | list | | 目标点 |
| `events` | list | | 定时事件（§4.1） |
| `rules` | list | | 条件规则（§4.2） |
| `meta` | dict | | 元数据（§3.5） |
| `tutorial` | dict | | 教学脚本（§3.6） |

### 3.2 地形与高程

地形符号（每行必须正好 `w` 个字符）：

| 符号 | 地形 | 缺省海拔 (m) |
|---|---|---|
| `w` | 水域 | 0 |
| `c` | 海岸 | 20 |
| `p` | 平原 | 90 |
| `f` | 林地 | 160 |
| `h` | 丘陵 | 380 |
| `m` | 山地 | 1100 |
| `u` | 城区 | 120 |
| `d` | 荒漠 | 220 |
| `s` | 沼泽 | 30 |

高程用 0-9 的一位数字，每级 300m（`elevation.rows` 与地形同样尺寸）。
不写就按上表缺省值。

```json
{
  "size": [6, 3],
  "terrain": [
    "pppppp",
    "phhhhp",
    "wwccpp"
  ]
}
```

### 3.3 部署

```jsonc
"deployments": {
  "BLUE": [
    { "template": "m1a2", "x": 3, "y": 4 },
    { "template": "f35a", "x": 3, "y": 4, "altitude": "med", "name": "长机" }
  ],
  "RED": [
    { "template": "t72", "x": 12, "y": 9 }
  ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `template` | ✔ | 兵种 id，必须在武器库里存在 |
| `x` / `y` | ✔ | 坐标，越界直接报错 |
| `name` | | 覆盖显示名（用于「长机」「旗舰」这类） |
| `altitude` | | 空中单位的初始高度 |

**同格规则**（加载时校验，违反直接报错）：

| 域 | 每格上限 | 可与谁同格 |
|---|---|---|
| 地面 / 设施 | 1 | 空中单位 |
| 水面舰 | 1 | 潜艇 |
| 潜艇 | 2 | 水面舰 |
| 空中 | 4（机场停机坪上限） | 地面单位 |

另外：陆上 / 设施单位不得部署在水域格。

### 3.4 目标点与胜利条件

```json
"objectives": [
  { "x": 10, "y": 8, "name": "补给站", "kind": "capture", "side": "NEUTRAL", "value": 5 }
]
```

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `x` / `y` | ✔ | — | 必须在地图内 |
| `name` | | `目标N` | 显示名 |
| `kind` | | capture | 见 `ObjectiveKind` |
| `side` | | NEUTRAL | 初始归属 |
| `value` | | 1 | 每回合胜负点 |

胜负判定：回合结束时目标点由**具备占领能力**的单位控制，持有方每回合得分；
`turn_limit` 到期按总分判胜，平局则双方无胜者。

### 3.5 元数据

`meta` 只影响**展示与检索**，不参与结算：

| 字段 | 说明 |
|---|---|
| `author` | 作者 |
| `summary` | 简介（比 `description` 长） |
| `difficulty` | easy / normal / hard / extreme |
| `tags` | 标签数组，主菜单最多显示 3 个 |
| `recommended_units` | 推荐兵种 id |
| `recommended_side` | 推荐阵营 |
| `version` | 想定自身版本 |
| `license` | 授权方式 |
| `campaign` / `campaign_step` | 归属战役与序号 |

`difficulty` 会被主菜单渲染成彩色芯片，取值不在上表内则显示原样字符串。

### 3.6 教学脚本

```jsonc
"tutorial": {
  "steps": [
    { "id": "sel", "title": "选中部队", "body": "点击蓝方棋子。",
      "goal": "选中一支蓝方部队", "trigger": "select_unit" }
  ]
}
```

`trigger` 取以下值之一（写错会被忽略而不是报错）：

`select_unit`、`hover_enemy`、`tracked`、`fire`、`counter_viewed`、`fortify`、
`turn_end`、`next`、`air_move`、`profile`、`radar`、`jam`、`datalink`、
`smoke`、`decoy`、`prepare`、`order`。

**只有声明了 `tutorial` 的想定才会挂教学引导机**——普通想定不会
凭空冒出教学提示。

---

## 4. 声明式规则

两条互不冲突的路线：**事件**（第 N 回合发生什么）与**规则**（满足条件时发生什么）。
两者都在**回合结算**时求值：事件先算，规则后算，规则能看见事件的结果。

### 4.1 事件 events

```jsonc
"events": [
  { "turn": 5, "type": "airstrike", "side": "RED", "x": 12, "y": 8,
    "weapon": "kh101", "radius": 2, "damage": 45,
    "message": "红方航空兵对训练场实施空袭" },
  { "turn": 8, "type": "reinforce", "side": "BLUE",
    "unit": "m1a2", "x": 5, "y": 20, "count": 2 },
  { "turn": 3, "type": "weather", "weather": "fog" },
  { "turn": 10, "type": "objective", "x": 18, "y": 18,
    "name": "补给点", "owner": "BLUE", "value": 5 }
]
```

| `type` | 作用 | 关键字段 |
|---|---|---|
| `airstrike` | 区域空袭 | `side` `x` `y` `weapon` `radius` `damage` |
| `reinforce` | 增援部署 | `side` `unit` `x` `y` `count` |
| `weather` | 改变天候 | `weather` |
| `objective` | 新增 / 变更目标点 | `x` `y` `name` `owner` `value` |
| `reveal` | 强制暴露某区域 | `side` `x` `y` `radius` |
| `message` | 播报文本 | `message` |

设计约定：事件**只在玩家行动开始前**结算，玩家总有反应机会；
已触发的事件不会重复执行（除非显式写 `repeat`）。

### 4.2 规则 rules：条件

```jsonc
"rules": [
  {
    "id": "reinforce_on_loss",
    "when": { "type": "unit_lost", "side": "BLUE", "template": "m1a2" },
    "then": [
      { "type": "reinforce", "side": "BLUE", "template": "m1a2",
        "x": 12, "y": 44, "count": 1 },
      { "type": "message", "text": "预备队填补缺口" }
    ]
  }
]
```

条件类型（`core.hooks.CONDITION_TYPES`）：

| `type` | 含义 | 相关字段 |
|---|---|---|
| `always` | 恒真 | — |
| `turn_at_least` / `turn_at_most` / `turn_is` | 回合数比较 | `turn` |
| `side_controls_min` | 某方控制的目标点 ≥ N | `side` `count` |
| `side_lost_min` | 某方损失单位数 ≥ N | `side` `count` |
| `unit_near_objective` | 某方有单位在某目标点附近 | `side` `x` `y` `radius` |
| `enemy_near_objective` | 敌方有单位在某目标点附近 | `side` `x` `y` `radius` |
| `weather_is` | 当前天候是 | `text` |
| `time_of_day_is` | 当前时段是 | `text` |
| `victory_points_lead` | 某方胜负点领先 ≥ N | `side` `count` |
| `all` / `any` / `not` | 组合器，子条件放 `conditions` 数组 | `conditions` |

> 规则里写的条件类型不在上表内会**报错并指出可选值**——这是有意的：
> 静默忽略一个拼错的 `when` 会让作者以为规则生效了。

### 4.3 规则 rules：效果

| `type` | 作用 | 关键字段 |
|---|---|---|
| `message` | 播报文本 | `text` |
| `reinforce` | 增援 | `side` `template` `x` `y` `count` |
| `airstrike` | 区域空袭 | `side` `x` `y` `weapon` `radius` `damage` |
| `weather` | 改变天候 | `weather` |
| `reveal` | 强制暴露 | `side` `x` `y` `radius` |
| `objective` | 变更目标点 | `x` `y` `name` `owner` `value` |
| `damage` / `heal` | 区域伤害 / 治疗 | `x` `y` `radius` `value` / `side` |
| `set_flag` | 设置标志位（供后续条件读取） | `text` |

单条规则出错不影响其它规则；规则求值顺序固定，因此**可复现**。

---

## 5. 模组（Python）

### 5.1 最小模组

放在用户数据目录的 `mods/` 下（每个目录一个模组）或安装目录 `data/mods/`。

`mods/hello/main.py`：

```python
"""最小模组：加一件武器、订阅一个钩子。"""

def register(api):
    api.add_weapon({
        "id": "hello_gun",
        "name": "问候炮",
        "kind": "gun",
        "targets": ["land"],
        "range": 2.0,
        "pk": 0.5,
        "damage": 15,
        "ammo": 6,
    })
    api.add_unit({
        "id": "hello_tank",
        "name": "问候坦克",
        "class": "mbt",
        "domain": "land",
        "hp": 120,
        "mp": 3.0,
        "armor": 6,
        "weapons": ["hello_gun"],
    })
    api.on("modify_damage", boost)
    api.log("问候模组已加载")

def boost(state, attacker, target, weapon, damage):
    # 返回新伤害值；返回 None 表示不改
    if weapon.id == "hello_gun":
        return damage * 1.5
    return None
```

只要导出 `register(api)` 就算一个模组（无 `mod.json` 也能加载，但会记一条
「不规范」警告）。

### 5.2 mod.json 清单

```json
{
  "id": "hello",
  "name": "问候模组",
  "version": "1.2.0",
  "author": "someone",
  "description": "一句话说明",
  "api": ">=1.0.0",
  "requires": { "other_mod": ">=1.0.0" },
  "entry": "main.py"
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✔ | 唯一标识，与依赖声明的键一致 |
| `name` / `version` / `author` / `description` | | 展示用 |
| `api` | | 适配的 SDK 版本范围，如 `>=1.0.0`、`>=1.0.0,<2.0.0` |
| `requires` | | `{mod_id: 版本范围}`，不满足则拒绝加载 |
| `entry` | | 入口文件名，默认 `main.py` |

未识别的字段会原样保留在 `extra` 里，方便社区自定义。

### 5.3 注册接口 ModAPI

白名单（`core.mods.PUBLIC_API`）共 10 个方法，全部有版本承诺：

| 方法 | 作用 |
|---|---|
| `add_weapon(data)` | 注册武器，字段同 §2.2 |
| `add_sensor(data)` | 注册传感器，字段同 §2.3 |
| `add_unit(data)` | 注册兵种，字段同 §2.4 |
| `add_mount(data)` | 注册挂载预设（武器 + 备弹组合） |
| `add_icon(unit_id, path)` | 为兵种指定图标（相对模组目录，PNG/SVG） |
| `add_localization(lang, entries)` | 注入词条（§5.5） |
| `set_metadata(key, value)` | 自定义元数据，供其它模组查询 |
| `require(mod_id, spec)` | 声明依赖（也可写在 `mod.json`） |
| `on(event, fn)` | 订阅规则钩子（§5.4） |
| `log(message)` | 加载期输出一条信息 |

所有方法失败时抛 `core.mods.ModError`，消息里带模组 id。

### 5.4 规则钩子

十个事件（`core.mods.HOOK_EVENTS`），签名以 `core.mods.HOOK_SIGNATURES` 为准。
按用途分两类——**能改结果的**只有前三个：

| 事件 | 回调签名 | 返回值 | 何时触发 |
|---|---|---|---|
| `before_fire` | `(state, attacker, target, weapon)` | 忽略 | 解算前（可改状态） |
| `after_fire` | `(state, attacker, target, weapon, result)` | 忽略 | 一次射击结算完 |
| `modify_damage` | `(state, attacker, target, weapon, damage)` | **新伤害值**或 `None` | 伤害应用前 |

通知型（v3.3 新增，**只播报既成事实，改不了它**）：

| 事件 | 回调签名 | 何时触发 |
|---|---|---|
| `turn_start` | `(state, turn)` | 回合物资结算完成、玩家可操作之前 |
| `turn_end` | `(state, turn)` | 本回合结束、清理动作之前 |
| `unit_destroyed` | `(state, unit)` | 单位被摧毁（任何原因） |
| `unit_damaged` | `(state, unit, damage, by)` | 单位受损（未必摧毁） |
| `facility_struck` | `(state, tile, damage, by)` | 机场 / 港口 / 枢纽被打 |
| `facility_captured` | `(state, side, x, y, by)` | 设施易手 |
| `objective_captured` | `(state, side, x, y, by)` | 目标点易手 |

`result` 是 `core.engine.FireResult`，含 `ok` / `message` / `hits` / `damage` /
`destroyed` / `intercepted` / `flight` 等字段。

钩子里的 `state` 是 `core.engine.GameState`——**可以读取任何字段，但改状态
请走它自己的方法**（`fire` / `move_unit` / `deploy_decoy` …），
直接改内部字段会让回放与原局分叉。

#### 事件总线怎么工作的

所有通知型事件走同一条总线（`core.bus.EventBus`，挂在 `state.bus` 上）：

```
引擎状态改变处 ──emit(name, state, **fields)──▶ 总线
                                                ├─▶ 历史（最近 96 条，state.bus.recent()）
                                                ├─▶ 模组钩子（本节表格的签名）
                                                └─▶ 本地订阅者 (state, event)
```

三条要记住的性质：

* **顺序即因果**。`turn_end` 一定在清理动作之前、`turn_start` 在之后；
  `unit_damaged` 一定在同一个单位的 `unit_destroyed` 之前。
  靠这个顺序就能把一回合还原成一条时间线；
* **有一条统一出口**。单位阵亡走 `state.kill_unit()`，所以直射、触雷、
  燃油耗尽、诱饵到期、想定脚本的区域伤害——五条路径都发同一个事件，
  带 `cause` 区分原因（`direct_fire` / `mines` / `fuel` /
  `decoy_expired` / `scenario_event`）。**不要**自己置 `unit.destroyed`；
* **订阅者写错不会带崩对局**。回调抛异常会被收进 `state.bus.errors`，
  引擎照常往下走；但**别依赖这一点**，`tools/test_bus.py` 会把异常打出来。

要「看」事件而不用改引擎的场合（战报增强、成就、外部日志），订阅总线即可；
要「改」结果的场合，用上面三个 `modify_*` / `before_*` 钩子。两类接口不要混用。

### 5.5 本地化注入

```python
def register(api):
    api.add_localization("zh", {"hello.unit_desc": "问候坦克：用来演示词条注入"})
    api.add_localization("en", {"hello.unit_desc": "Hello tank: a localization demo"})
```

规则：

* 键随便起，但**不能覆盖游戏本体的键**（会被拒绝）——推荐用 `模组id.` 前缀；
* 词条在**本地化初始化之前**注入，所以模组词条当天生效；
* 某语言缺失时按回退链降级（具体语言 → 中文 → 键名）。

### 5.6 依赖与版本匹配

```json
"requires": { "core_extras": ">=1.2.0,<2.0.0" }
```

加载器按声明顺序检查：依赖缺失 / 版本不匹配 → **本模组不加载**，并在
启动日志里说明是哪个依赖、要求什么版本、实际是什么版本。

版本范围支持 `>=` `>` `<=` `<` `==` `=`，多条件用逗号分隔（全部满足）。

### 5.7 错误隔离与调试

| 情形 | 结果 |
|---|---|
| 语法错误 | 该模组不加载，其它模组正常 |
| `register` 抛异常 | 同上 |
| 缺 `register` | 同上，提示「未导出 register(api)」 |
| 钩子内部抛异常 | 记录一条错误，**该次钩子跳过**，对局继续 |

调试三板斧：

```bash
python main.py --check      # 数据与模组注册情况
python tools/test_mods.py   # 模组加载 / 隔离 / 钩子
python tools/test_sdk.py    # 清单 / 版本承诺 / 依赖 / 词条注入
```

模组加载摘要会写进用户配置的 `last_mods` 字段，主菜单出错时也会显示 `self.error`。

---

## 6. 命令流与回放 API

一条命令记的是**语义**（「把 3 号单位移到 (10,20)」），不是鼠标坐标——
所以回放不依赖分辨率、不依赖 UI 版本。

### 6.1 Command 与 CommandLog

```python
from core.commands import Command, CommandLog, CMD_MOVE

log = CommandLog()
log.record(CMD_MOVE, turn=3, uid=7, x=10, y=20)
```

`Command` 字段：`kind`（str）、`args`（dict）、`turn`（int）。
`to_dict()` / `from_dict()` 保证 JSON 往返。

命令类型（`core.commands.ALL_KINDS`）与 `args` 约定：

| `kind` | `args` |
|---|---|
| `select` | `uid` |
| `move` | `uid` `x` `y` |
| `fire` | `attacker` `target` `weapon` [`salvo`] |
| `fire_facility` | `attacker` `weapon` `x` `y` |
| `end_turn` | — |
| `radar` / `jam` / `datalink` | `uid` |
| `fortify` | `uid` |
| `decoy` | `uid` `x` `y` `kind` |
| `smoke` | `uid` `weapon` `x` `y` |
| `hold` | `uid` |
| `profile` | `uid` `mission` |
| `prepare` | `uid` `x` `y` |
| `order` | `commander` `order` [`target`] |

### 6.2 命令执行器

```python
from core.replay import apply_command, run_ai_turn

ok = apply_command(state, cmd)     # 返回是否执行成功，失败不抛异常
run_ai_turn(state)                 # 一次跑完 AI 回合
```

**UI 与回放共用 `core.replay.apply_command`**——玩家点「开火」和回放跑到
「开火」走的是同一行代码，不存在「回放版本行为略有不同」。

失败返回 `False` 而不是抛异常：回放要能容忍「命令在当前状态不可执行」
（目标已被消灭），否则一条失效命令会让整局回放崩掉。

### 6.3 ReplayFile

```python
from core.replay import ReplayFile

rf = ReplayFile.from_game(state, "strait_shield.json", log,
                          note="演示", scenario_path=path)
rf.save("replays/demo.bwrep")

rf2 = ReplayFile.load("replays/demo.bwrep")
path, note = rf2.materialize_scenario(["data/maps", user_maps_dir], user_maps_dir)
```

字段：

| 字段 | 说明 |
|---|---|
| `scenario` | 想定文件名 |
| `seed` | 对局种子 |
| `player_side` | 玩家阵营 |
| `commands` | 命令流（观战回放为空） |
| `version` | 回放格式版本 |
| `kind` | `match`（命令流）/ `ai_vs_ai`（观战） |
| `game_version` | 制作版本 |
| `scenario_sha256` | 想定**内容**指纹 |
| `scenario_data` | 内嵌想定（分享用） |
| `note` | 备注 |

`materialize_scenario(search_dirs, writable_dir)` 的匹配顺序：
**内容一致**的本地文件 → 内嵌想定 → 同名文件（带告警）。
只按文件名匹配会把「同名不同版」的想定当成同一张图，演出来的就不是那一局了。

### 6.4 Replayer

```python
from core.replay import Replayer

r = Replayer(scenario, seed=rf.seed, commands=rf.commands, player_side=Side.BLUE)
final = r.run(on_command=lambda cmd, ok: print(cmd.kind, ok))
```

`run()` 在 `state.over` 或超出 `max_turns` 时停止。要逐帧推进（做播放器）就用
`Replayer` 的调用方 `ui.replayplayer.ReplayPlayer`——它同时支持命令流回放与
观战回放。

### 6.5 观战 SpectatorMatch

```python
from core.spectate import SpectatorMatch

m = SpectatorMatch(scenario, seed=1234, player_side=Side.BLUE)
while (info := m.step()) is not None:
    print(info["side_label"], info["unit"], info["text"])
print(m.summary())
```

* 玩家方相位用 `core.ai.AIPlanner` 出动作，AI 方相位走引擎原有流程；
* `step()` 一次推进**一个动作**，相位切换在内部消化，调用方只管按节奏调；
* `state.spectator` 为真 → 界面全图可见（**只影响显示**，AI 仍按各自探测判定）；
* 整局由「想定 + 种子」唯一决定，所以观战回放只存种子。

---

## 7. 分享格式

### 7.1 想定包 .bwscn

本质是 zip：

```
package.json          清单：格式版本、名称、作者、游戏版本、逐文件 sha256
maps/<id>.json        想定（含部署、事件、规则、元数据）
armory.json           武器覆盖（可选，只写与内置库不同的部分）
```

```python
from core import pkg

pkg.export_package(["data/maps/my_map.json"], armory_path, "out/my_map.bwscn",
                   name="我的想定", author="someone", include_armory=False)
man = pkg.inspect_package("out/my_map.bwscn")     # 只读清单
res = pkg.import_package("out/my_map.bwscn", user_maps_dir, armory_path)
print(res.notes)                                  # 版本差异 / 覆盖提示
```

导入时的三道关：

1. **格式版本**高于程序支持 → 拒绝，并说明「请更新游戏」；
2. **sha256 不符** → 拒绝，并区分「传输损坏」与「内容不对」；
3. **结构非法**（缺 `deployments`、不是合法 JSON）→ 拒绝，且**不落盘**。

旧格式包（版本 1，无校验和）仍可导入，只是无法验证完整性。

### 7.2 回放 .bwrep

纯 JSON，字段见 §6.3。两处关键设计：

* **内嵌想定**——只记文件名的回放换台机器就打不开；
* **内容指纹**——`scenario_sha256` 按规范化 JSON 计算，编辑器另存后键序变了
  也不会误判成「另一张图」。

观战回放（`kind` 为 `ai_vs_ai`）不含命令流，只有种子：整局由种子唯一决定。

---

## 8. 界面层接口

### 8.1 主题与度量函数

模组若要写界面（少见但允许），**不要硬编码颜色和像素**：

```python
from ui import theme as T

T.draw_text(screen, "标题", (x, y), 12, T.TEXT, bold=True)
```

| 接口 | 用途 |
|---|---|
| `T.PANEL` / `T.TEXT` / `T.TEXT_DIM` / `T.CYAN` … | 主题色，四套主题自动适配 |
| `T.side_color(side)` | 阵营色（含色盲主题变体） |
| `T.button_fill(color, hover)` / `T.button_text(color, hover)` | 按钮底色与文字色 |
| `T.px(size)` | 基准字号 → 当前字号档像素 |
| `T.m(base)` | 基准度量 → 当前档（行高、控件高、内边距） |
| `T.lh(size)` | 一行文字占的高度（排版累加 `y` 用它） |
| `T.text_width(text, size)` / `T.wrap_text(...)` | 测量与换行 |
| `T.ellipsize(text, size, width)` | 按**像素**截断（不要用 `s[:14]`） |

字号三档（标准 / 大 / 特大）由 `T.set_font_scale()` 切换，界面代码照常写
基准值——`render_text` / `text_width` / `wrap_text` 内部统一换算，测量与绘制
不会分叉。

### 8.2 本地化 tr()

```python
from core.i18n import tr

tr("ui.menu.start")                       # 查词条
tr("ui.kills_fmt", "击杀 %s")              # 第二个参数是回退值
tr("ui.kills_fmt") % (3,)                  # 格式化由调用方做
```

规则：

* 词条键放在 `data/lang/<lang>.json`（核心）与 `<lang>_ui.json`（界面）；
* 模组用自己的 `<模组id>.` 前缀，**不得覆盖游戏键**；
* 占位符统一 `%s`，个数必须与调用点一致（`tools/lang_check.py` 会查）；
* 缺词条时回退：当前语言 → 中文 → 键名本身。

### 8.3 单位图标包

一个目录 + 一份清单：

```
icon_packs/my_pack/
  pack.json
  mbt.png
  fighter.png
```

```json
{
  "id": "my_pack",
  "name": "我的图标包",
  "author": "someone",
  "version": 1,
  "icons": { "mbt": "mbt.png", "fighter": "fighter.png" }
}
```

* `icons` 的键是 `UnitClass` 取值；
* 缺失的兵种自动回退到内置矢量图形，不必覆盖全部 25 种；
* 图标以**白色**绘制，运行时按阵营着色——一套图标同时服务红蓝双方；
* 查找顺序：用户目录 `icon_packs/` 优先于安装目录 `data/icon_packs/`。

---

## 9. 示例与自检

| 想学什么 | 看这里 |
|---|---|
| 最小可用模组 | `examples/mods/example_orbital.py` |
| 钩子怎么改伤害 | 同上（`modify_damage`） |
| 想定事件与规则 | `data/maps/training_ground.json` |
| 教学脚本 | `data/maps/training_ground.json` 的 `tutorial` |
| 战役串联 | `data/campaigns/` |

自检脚本（全部不依赖 pytest，可直接跑）：

```bash
python main.py --check            # 数据校验（武器 / 兵种 / 想定 / 模组）
python tools/test_mods.py         # 模组加载与错误隔离
python tools/test_sdk.py          # 清单 / 版本承诺 / 依赖 / 词条注入
python tools/test_share.py        # 想定包 / 回放分享 / 观战可复现
python tools/test_replay.py       # 命令流往返与重演一致
python tools/lang_check.py        # 词条覆盖率与占位符
python tools/check_sdk.py         # 本文档与代码是否对得上
```

---

## 10. 核心模块地图

改引擎之前先知道代码在哪。规则层 v3.3 起按域拆开，`core/rules.py` 只剩
re-export——**它是对外的名字清单，不是实现**。

### 10.1 规则层的九个模块

| 模块 | 管什么 | 关键符号 |
|---|---|---|
| `core/dice.py` | 骰子 | `roll` `jitter` |
| `core/geo.py` | 几何、视线、共用常量 | `dist` `has_los` `crossfire_bonus` `RANGE_TOLERANCE` |
| `core/ew.py` | 电子战 | `jam_field` `jam_strength_at` `gps_integrity` `decoy_signature` |
| `core/detect.py` | 探测链与增量缓存 | `evaluate_detection` `detection_level` `build_detection_map` |
| `core/adn.py` | 防空网、逐发拦截、中段反导 | `AirDefenseNetwork` `resolve_salvo_interception` `interception_estimate` |
| `core/combat.py` | 射击剖面、威胁评估 | `shot_profile` `counter_threat_profile` |
| `core/facility.py` | 设施打击与服务 | `facility_strike_profile` `service_tile` |
| `core/logistics.py` | 补给链 | `supply_sources` `assign_supply` `supply_status` |
| `core/pathfind.py` | 机动与寻路 | `find_path` `flood_reachable` `tile_move_cost` |

依赖方向（箭头指向被依赖者，**无环**）：

```
pathfind → facility → combat → adn → detect → ew → geo → dice
```

`pathfind` 依赖 `facility` 只是因为基地容量常量 `AIRBASE_CAPACITY` 放在那边。

### 10.2 加新机制该放哪

| 你要加的 | 放这里 | 理由 |
|---|---|---|
| 新的探测条件（如被动声呐受温跃层影响） | `detect.py` 的 `sensor_effective_range` | 所有传感器倍率都在同一处连乘，散开就无法比较 |
| 新的毁伤修正（如攻顶弹药） | `combat.py` 的 `shot_profile` | 它已经收集了全部伤害因子 |
| 新的拦截方式 | `adn.py` | 拦截窗口与备弹消耗必须一起看 |
| 新的地形影响 | `geo.py`（通视）+ `pathfind.py`（代价） | 两件事本来就分开 |
| 新的电子战频段 | `ew.py` 的 `jam_band_weights` 与 `jam_field` | 频段是「场」的属性，不是单个单位的 |

### 10.3 两条必须守住的约定

**一、跨模块的「环境依赖」走属性访问。** 下面这几处刻意写成
`ew.jam_strength_at(...)` 而不是 `from .ew import jam_strength_at`：

```
dice.roll · geo.has_los · ew.jam_strength_at · ew.gps_integrity · detect.own_track
```

原因只有一个：自检脚本要把它们换成确定性桩值。**打补丁只能改定义处模块的
属性**，函数体内直接引用的名字是另一份绑定，补丁打不到。如果你新写一个
「自检需要打桩」的函数，照此办理。

**二、缓存必须挂在 `state` 上，不能模块级。** 用 `geo._cache_of(state, "名字")`
取一个对局私有的字典。模块级缓存只用坐标或 `id(state)` 做键都会出事：

- 只用坐标 → 换一张地图开局时，同坐标对命中上一张图的地形结论；
- 用 `id(state)` → CPython 回收旧对局后会复用地址，新对局命中上一局的场。

两条都实测踩过，都会让「同种子重跑」悄悄失效。

### 10.4 门面的约束

`core/rules.py` 只允许有 `import` 与 `__all__`。往里加实现的那一刻，
拆分就白做了——下一个改动又会找不到地方。要加东西，加到对应的域模块，
再让门面导出它。

**新写的纯函数要登记进单元测试清单。** `tools/test_units.py` 的
`TARGETS` 是规则层「每个决定点都有人盯过」的账本：加了函数却没登记，
清单覆盖率仍显示 100%，但那个函数其实一条断言都没有。所以新增机制时
两件事一起做——实现写进域模块，函数名写进 `TARGETS`，再用一个
`@case(group, "函数名")` 的用例把它认领掉。漏认领时脚本会失败并点名。

## 附录 A. 常见错误排查

| 现象 | 多半是 | 怎么确认 |
|---|---|---|
| 启动报「未定义的兵种模板 'x'」 | `deployments` 里的 `template` 拼错或武器库没这项 | `python main.py --check` |
| 想定加载失败「第 N 行长度不对」 | `terrain` 少写 / 多写字符 | 报错里带行列号 |
| 规则没生效 | `when.type` 拼错（会报错）或规则里 `side` 写成小写 | 报错会列可选值 |
| 模组不加载 | `mod.json` 的 `api` 范围与本机不符 | 启动日志 / `last_mods` |
| 模组加载了但没效果 | 钩子订阅了但返回值类型不对（`modify_damage` 要返回数值或 None） | `tools/test_mods.py` |
| 回放演出来和当初不一样 | 想定被改过（内容指纹不符） | 播放时的提示行会写明 |
| 观战回放打不开 | 缺内嵌想定（旧格式文件） | 提示「未内嵌想定数据」 |

## 附录 B. 版本历史

| 版本 | SDK 相关变更 |
|---|---|
| v2.0 | 模组原型：`register(api)` + 5 个钩子；命令流与回放 |
| v2.2 | 规则钩子系统（声明式 `rules`，条件 + 效果） |
| v3.0 | 官方 SDK：`mod.json` 清单、`API_VERSION`、公开 API 白名单、分层语言包、图标包 |
| v3.2 | 本文档；想定包格式 v2（版本标记 + sha256）；回放格式 v2（内嵌想定 + 指纹）；AI 观战 API |
