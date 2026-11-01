# 贡献者 · Contributors

本项目由以下成员贡献：

| 昵称 | 主页 | 职责 |
|---|---|---|
| **hamfreebird** | https://github.com/hamfreebird | 项目作者：玩法设计、核心引擎（规则/状态机/AI）、界面与编辑器、武器库与想定数据、图标、CI 与打包流程、文档 |

## 参与方式

1. 认领 `ROADMAP.md` 中的任务，或提交 Issue 描述想法
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

## 行为准则

尊重其他贡献者，讨论聚焦技术本身；涉及现实装备的数据请尽量采用公开资料并注明出处。
