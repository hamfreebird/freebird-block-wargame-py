# 贡献者 · Contributors

本项目由以下成员贡献：

| 昵称 | 主页 | 职责 |
|---|---|---|
| **hamfreebird** | https://github.com/hamfreebird | 项目作者：玩法设计、核心引擎（规则/状态机/AI）、界面与编辑器、武器库与想定数据、图标、CI 与打包流程、文档 |

## 参与方式

1. 从 [AUDIT.md](AUDIT.md) 的建议清单里认领一条，或提交 Issue 描述想法
2. 从 `main` 切出 `feature/<主题>` 分支开发
3. 提交前务必本地跑通以下检查：

```bash
python tools/check_names.py     # 未定义名检查（含类型注解）
python main.py --check          # 数据校验
python tools/diag.py 20         # 作战逻辑自检
python tools/smoke_test.py      # 无界面 UI 冒烟测试
```

4. 提交 PR，等待 CI（多版本 × 多平台）通过
5. 新增机制时请同步更新 `GAME_GUIDE.md`（机制说明）与 `tools/diag.py`（新增不变量）

## 维护者发布流程

1. 从 `main` 切 `feature/<版本>-<主题>`
2. 完成条目并补 `tools/diag.py` 不变量 / `tools/smoke_test.py` 断言
3. PR → CI 通过（校验 + 平衡无严重失衡 + 文档自检）
4. 合并 → 打 `vX.Y.Z` 标签 → 发布流水线产出安装包
5. 更新 `README.md`、`GAME_GUIDE.md`、`AUDIT.md` 与 `ROADMAP_HISTORY.md`

> 版本号三处必须一致：`core/version.py`、README 徽章、`AUDIT.md` 的
> 「当前基线」——`tools/check_docs.py` 会连 git 标签一起查。

## 行为准则

尊重其他贡献者，讨论聚焦技术本身；涉及现实装备的数据请尽量采用公开资料并注明出处。
