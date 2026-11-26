# 项目二：足球比分量化预测与赛后复盘系统（v1.6 · 五大联赛 + 欧冠）

本项目基于 **Dixon-Coles 泊松建模 + 预期进球（xG）+ 实时 ELO + 大小球水位反推总进球 + 临场大轮换自动检测 + AI 动态情报加减分修正**，实现赛前初判简报、赛前 1 小时临场终审、体彩五大玩法与 EV 性价比提示、5×5 比分热力图、飞书卡片推送、手机直看云端网页以及每日赛后归因复盘（含把握度星级分层与 CLV 跑赢临场统计）闭环。

## 一、本机环境与云端状态

- **数据与代码**：✅ **完整迁移并已与 GitHub 远端对齐**（包含 SQLite 数据库 [football_predictor.db](file:///d:/gemini/projects/football-predictor/data/football_predictor.db)、五大联赛历史缓存 CSV、飞书配置 [settings.json](file:///d:/gemini/projects/football-predictor/data/settings.json) 及静态展示页 [docs/index.html](file:///d:/gemini/projects/football-predictor/docs/index.html)）
- **运行环境**：✅ **已就绪（实测通过）**（Python 3.12.8、Git 2.47.1、`.venv` 虚拟环境与全部 9 项依赖库均齐全；CLI、数据库读写、量化引擎及 FastAPI Web 服务均正常）
- **云端自动托管**：✅ 已绑定 GitHub 仓库 `Felix82430/football-predictor`，通过 [.github/workflows/football_auto_push.yml](file:///d:/gemini/projects/football-predictor/.github/workflows/football_auto_push.yml) 实现 0 成本定时拉取盘口、推送飞书与刷新手机网页。
  > **本地/云端防冲突提醒**：每次在本地执行同步、分析或复盘前，建议先运行 `git pull --ff-only` 拉取云端最新数据库。

## 二、快速使用入口

- **手机直看云端网页（免开机随时看）**：https://felix82430.github.io/football-predictor/
- **本地网页控制台**：直接双击 [启动足球预测软件.bat](file:///d:/gemini/projects/football-predictor/启动足球预测软件.bat)，浏览器会自动打开 `http://127.0.0.1:8765`。
- **AI 对话驱动**：在 Antigravity 里直接说“分析今天的五大联赛/欧冠比赛”“临场终审”或“核对昨晚比分并复盘”即可自动调用技能执行。

## 三、关联文档与入口

- **AI 技能规则（v1.6）**：[SKILL.md](file:///d:/gemini/.agents/skills/football-predictor/SKILL.md)
- **飞书配置指南**：[飞书机器人配置指南.md](file:///d:/gemini/projects/football-predictor/飞书机器人配置指南.md)
- **GitHub 云端自动推送指南**：[GitHub云端托管1分钟配置指南.md](file:///d:/gemini/projects/football-predictor/GitHub云端托管1分钟配置指南.md)
