# GitHub Actions 0 成本云端托管指南（电脑关机照样 24 小时推飞书）

配好之后，哪怕你**电脑关机、半夜睡觉、出门出差**，手机飞书照样准点收到：
- **每天 `12:00`**：午间初判简报
- **每天 `20:00`**：晚间盘口更新简报
- **每场赛前 1 小时**：自动拉取官方首发与最终临场盘口，重算锁单并推送飞书
- **每天 `09:00`**：自动核对昨晚完场比分、归因复盘并推送报告

---

## 只需 3 步（全程免费，约 3 分钟）

### 第 1 步：建一个自己的私人 GitHub 仓库
1. 打开 https://github.com 登录（没账号点 Sign up 免费注册一个）。
2. 点右上角 **`+`** → **`New repository`**。
3. 仓库名填 `football-predictor`，**务必勾选 `Private`（私密仓库）**，点底部 **`Create repository`**。

### 第 2 步：把飞书 Webhook 存进 GitHub 加密保险箱（最安全，别人看不到）
1. 进入刚建好的仓库 → 点上方 **`Settings`** → 左侧选 **`Secrets and variables`** → **`Actions`**。
2. 点绿色的 **`New repository secret`**：
   - **Name** 填：`FEISHU_WEBHOOK`
   - **Secret** 填：你的飞书 Webhook 地址（`https://open.feishu.cn/open-apis/bot/v2/hook/...`）
   - 点 **`Add secret`** 保存。

### 第 3 步：把项目上传上去并开启定时开关
- **最省事的方法**：告诉我你的 GitHub 仓库地址（例如 `https://github.com/你的用户名/football-predictor.git`），我可以直接帮你安装轻量 Git 并把代码一键推送上去！
- 上传成功后，点仓库顶部的 **`Actions`** 标签页 → 左侧点 **`足球量化情报官 · 0成本云端自动推送飞书`** → 点右侧 **`Run workflow`** 手动触发一次，1 分钟后手机飞书立刻弹出卡片，以后就永久全自动运行了！
