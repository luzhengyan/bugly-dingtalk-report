# Bugly 报告独立脚本 · 腾讯云 SCF 部署指南

目标：把 `bugly_dingtalk_report.py` 部署到腾讯云函数 SCF，由 **cron-job.org** 每天 09:40 定时触发，彻底摆脱"本机关机推不出去"。

---

## 0. 准备（一次性的）

1. **Bugly 令牌** `BUGLY_ACCESS_TOKEN`
   登录 Bugly 控制台 → 个人中心 / OpenAPI → 生成**个人访问令牌**（PAT）。
   这是 `Authorization: Bearer <token>` 用的那个，和 WorkBuddy 里的 bugly-token 是同一类凭据。
2. **钉钉凭据**（已有，`D:/bugly/bugly/config.ini` 的 `[DingTalkTest]` / `[DingTalk]`）
   - `webhook`：`https://oapi.dingtalk.com/robot/send?access_token=...`
   - `secret`：`SECxxxx...`
   部署时直接填到 SCF 环境变量（比放 config.ini 更干净）。

---

## 1. 上传函数到 SCF

1. 腾讯云控制台 → **云函数 SCF** → 新建函数 → 自定义创建。
2. 运行环境：**Python 3.10**（或 3.12）；地域任选。
3. 函数名叫 `bugly-dingtalk-report`。
4. 把本仓库的 `bugly_dingtalk_report.py` 上传，**重命名为 `index.py`**（SCF 要求入口文件名为 `index.py`，入口函数 `main_handler` 已在脚本里写好）。
5. **配置**：
   - 执行超时：改成 **30 秒**（建 MCP 会话 + 4 次调用 + 钉钉推送，实测几秒，留余量）。
   - 内存：128 MB 足够。
6. **环境变量**（函数配置 → 环境变量，SCF 会静态加密）：
   | 键 | 值 |
   |----|----|
   | `BUGLY_ACCESS_TOKEN` | 步骤 0 生成的令牌 |
   | `DINGTALK_WEBHOOK` | 钉钉 webhook |
   | `DINGTALK_SECRET` | 钉钉 secret |
   | `DINGTALK_ENV` | `test` 或 `prod` |

   > 脚本优先读这两个环境变量；没设才会去读 `CONFIG_INI` 指定的 config.ini。**部署时直接填环境变量即可。**

---

## 2. 暴露公网 URL（给 cron-job.org 打）

1. SCF → 触发管理 → 创建触发器 → 触发类型：**API 网关（API Gateway）**。
2. API 服务：新建一个（如 `bugly-report-svc`），前端类型 HTTP，勾选"开启集成响应"（可选）。
3. 创建后，触发器会给出一个**公网 URL**，形如：
   `https://xxx.apigw.tencentcs.com/release/bugly-dingtalk-report`
   这就是 cron-job.org 要请求的地址。

---

## 3. 在 cron-job.org 配置定时

1. 打开 https://console.cron-job.org/dashboard → **Create Cronjob**。
2. **URL**：粘贴上一步的 APIGW 公网地址。
3. **Schedule**：选每天 **09:40**（cron-job.org 可设时区，确认是 Asia/Shanghai/北京时间）。
   - 若用 Cron 表达式且站点按 UTC：北京时间 09:40 = UTC 01:40 → `40 1 * * *`。
4. 建议开启 **Retry on failure** 和 **Notification email**。
5. 保存。可点 **Run now** 立即验证一次。

> 触发后，SCF 函数执行 `main_handler` → 跑 `main()` → 分别给 Android / iOS 拉数据 + 推钉钉，返回 `{"statusCode":200,"body":"Bugly report sent"}`。

---

## 4. 本地自测（部署前先在机器上跑通）

```bash
# 1) 仅验证报告格式（不发网络请求）
python bugly_dingtalk_report.py --demo

# 2) 用样例数据 + 真推送到测试钉钉（需本机 D:/bugly/bugly/config.ini 有测试凭据）
python bugly_dingtalk_report.py --demo --push

# 3) 正式跑（需 BUGLY_ACCESS_TOKEN 环境变量）
set BUGLY_ACCESS_TOKEN=你的令牌
set DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxx
set DINGTALK_SECRET=SECxxx
set DINGTALK_ENV=test
python bugly_dingtalk_report.py
```

---

## 5. 验证清单

- [ ] `python bugly_dingtalk_report.py --demo` 报告格式与原版一致
- [ ] `--demo --push` 测试钉钉收到 `errcode:0`
- [ ] SCF 函数用真实 `BUGLY_ACCESS_TOKEN` 手动测试一次（SCF 控制台"测试"按钮，或本地设好环境变量跑第 3 步）
- [ ] APIGW 公网 URL 在浏览器/Postman 直接 POST 能触发推送
- [ ] cron-job.org Run now 成功，钉钉收到报告
- [ ] 第二天 09:40 自动收到，且**本机关机也能收到**

---

## 6. 其他说明

- **零第三方依赖**：脚本只用 Python 标准库（`urllib`/`hmac`/`configparser` 等），SCF 内置 Python 运行时直接能跑，无需层/依赖包。
- **安全**：`BUGLY_ACCESS_TOKEN` 与钉钉 `secret` 只存在于 SCF 环境变量（静态加密），不进代码、不进仓库。建议用**专用** Bugly 令牌，便于泄露后单独吊销。
- **和 WorkBuddy 自动化的关系**：两者互不冲突。建议新链路（SCF+cron-job.org）稳定跑几天后，再停用 WorkBuddy 的每日自动化（id `64e46f0e…`）和 Windows 计划任务 111（受"不可改/删"硬约束，停用前需你确认）。
- **监控**：cron-job.org 失败会邮件通知；也可在 SCF 开日志（日志服务 CLS）留存每次执行结果。

---

## 7. 方案 B：GitHub Actions（更简单，推荐先把这一个跑通）

相比 SCF + cron-job.org，**GitHub Actions 自带定时调度**，不再需要 API 网关和 cron-job.org 两个外部件，整个链路就是「GitHub 每天 09:40 启动一个 Linux runner → 跑脚本 → 推钉钉」。runner 在 GitHub 云端 7×24 在线，**本机关机完全不影响**。

### 7.1 仓库结构
把脚本和 workflow 放进同一个 GitHub 仓库（任意仓库，`bugly` 脚本放根目录）：
```
bugly_dingtalk_report.py          # 已写好的独立脚本
.github/workflows/bugly-report.yml # 本次新增的定时任务
```

### 7.2 workflow 文件（已生成，无需改动）
`.github/workflows/bugly-report.yml` 核心：
```yaml
on:
  schedule:
    - cron: "40 1 * * *"      # GitHub 用 UTC；北京 09:40 = UTC 01:40
  workflow_dispatch: {}        # 允许在 Actions 页面手动 Run workflow 测试
jobs:
  report:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: python bugly_dingtalk_report.py
        env:
          BUGLY_ACCESS_TOKEN: ${{ secrets.BUGLY_ACCESS_TOKEN }}
          DINGTALK_WEBHOOK:   ${{ secrets.DINGTALK_WEBHOOK }}
          DINGTALK_SECRET:    ${{ secrets.DINGTALK_SECRET }}
          DINGTALK_ENV:       ${{ secrets.DINGTALK_ENV || 'test' }}
```

### 7.3 配置 Secret（仓库 Settings → Secrets and variables → Actions → New repository secret）
| Secret | 值 |
|--------|----|
| `BUGLY_ACCESS_TOKEN` | Bugly PAT |
| `DINGTALK_WEBHOOK` | `https://oapi.dingtalk.com/robot/send?access_token=...` |
| `DINGTALK_SECRET` | `SECxxxx...` |
| `DINGTALK_ENV` | `test`（或 `prod`） |

> runner 是 Linux，没有本机 `D:/bugly/bugly/config.ini`，所以必须走 `DINGTALK_WEBHOOK`/`DINGTALK_SECRET` 两个环境变量（脚本已优先读它们）。

### 7.4 验证
1. 推上去后，到 **Actions** 页面点 **Run workflow** 手动触发一次，看钉钉是否收到双平台报告。
2. 等第二天 09:40（UTC 01:40）自动跑，确认**本机关机也能收到**。

### 7.5 注意点
- **时区**：GitHub cron 是 UTC。`40 1 * * *` = 北京 09:40。若想改时间按此时差换算。
- **定时不保证秒级准点**：GitHub 免费 runner 在高峰期可能延迟 5~30 分钟触发，对每日报告完全可接受。
- **免费额度**：公开仓库 Actions 免费；私有仓库每月 2000 分钟（每日跑一次约 30 分钟/月，绰绰有余）。
- **脚本零依赖**：纯标准库，runner 里 `actions/setup-python@v5` 装好 3.12 即可直接 `python` 运行，`__main__` 入口会自动调 `main()`。
