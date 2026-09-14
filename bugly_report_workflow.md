# Bugly 每日质量报告 · 完整工作流拆解

> 目的：让你一眼看懂「每天 09:40 那份钉钉报告」从触发到送达的全链路。
> 现状：本机/WorkBuddy 已不再承担日常推送，改为 **GitHub Actions 云端定时**执行。

---

## 0. 全链路总览

```
[GitHub Actions 定时]  ──UTC 01:40 (=北京 09:40)──▶  [云端 Linux runner]
        │                                                      │
        │  python bugly_dingtalk_report.py                    ▼
        │                                        [独立 Python 脚本]
        │                                                      │
        │                        ┌──────────── 取数：Bugly MCP ────────────┐
        │                        │  Android(e43ecf9d21) + iOS(6b0455dc18)  │
        │                        │  各 4 次 tools/call                     │
        │                        └─────────────────────────────────────────┘
        │                                                      │
        │                                                      ▼
        │                                        [生成钉钉 markdown]
        │                                                      │
        │                                                      ▼
        │                          [HMAC-SHA256 加签] ──▶ POST ──▶ [测试钉钉群]
```

---

## 1. 触发层：GitHub Actions

| 项 | 值 |
|----|----|
| 文件 | `.github/workflows/bugly-report.yml` |
| 触发条件 | `schedule: cron "40 1 * * *"`（**UTC 01:40 = 北京 09:40**）+ `workflow_dispatch`（手动） |
| 运行环境 | `ubuntu-latest` + Python 3.12（`actions/setup-python@v5`） |
| 注入 Secrets | `BUGLY_ACCESS_TOKEN` / `DINGTALK_WEBHOOK` / `DINGTALK_SECRET` / `DINGTALK_ENV=test` |
| 时区 | `TZ: Asia/Shanghai`（让报告「生成时间」显示北京时间，不影响取数时间窗） |

要点：
- GitHub runner 在云端 **7×24 在线**，本机关机/WorkBuddy 不在线都不影响触发。
- `schedule` 用 **UTC**，所以写 `40 1 * * *`（不是 09:40）。
- 免费 runner 高峰期可能延迟 5~30 分钟触发，属正常。

---

## 2. 脚本入口

`python bugly_dingtalk_report.py` → `main()`：
1. 读环境变量（token / 钉钉凭据）。
2. 建 `BuglyMCP` 客户端并 `initialize()`。
3. `run_once()`：对两个产品取数 → 生成报告 → 推钉钉。
4. 零第三方依赖（仅 Python 标准库 `urllib`/`hmac`/`json` 等），任何公网主机可跑。

---

## 3. 取数层：Bugly MCP（streamableHttp）

- 端点 `https://bugly.tds.qq.com/mcp`，鉴权 `Authorization: Bearer <BUGLY_ACCESS_TOKEN>`。
- 客户端流程：`initialize` → `notifications/initialized` → 多次 `tools/call`。
- 每个产品（Android / iOS）做 **4 次调用**：

| # | MCP 工具 | 入参 | 取到的数据 |
|---|----------|------|-----------|
| 1 | `query_quality_overview` | `metric_types=["crash"]`, `days_ago=1` | 设备崩溃率 / 次数 / 影响设备 / 环比 |
| 2 | `generate_token` | `metric_type="error"`, `time_window=2`, `event_time∈[now-48h, now]` | 取趋势用的临时 token |
| 3 | `query_exception_rate_trend` | `metric_type="error"`, `token` | `device_uv_ratio`(48 点) + `count`(48 点) |
| 4 | `query_top_issues` | `metric_type="error"`, `days_ago=1`, `top_n=5` | 错误问题总数 + Top5 |

---

## 4. 计算层（关键口径）

- **崩溃率** = `device_ratio.current × 100`（保留 3 位小数）。
- **错误率** = `device_uv_ratio` **后 24 个点均值 × 100**。
  - ⚠️ 为什么不直接用 `query_quality_overview` 的 error？因为 Bugly 对该维度返回「无数据」，真实错误率必须走 `query_exception_rate_trend` 的 `device_uv_ratio`（每小时 error 影响设备数 / 该小时 DAU 设备数，与崩溃率同口径）。
- **错误率环比** = `(今日 − 昨日) / 昨日 × 100`（昨日前 24 点、今日后 24 点）。
- **错误次数(24h)** = `count` 后 24 点求和。
- **Top5 单行格式**：`序号. 版本号 [string 文件名][点击跳转](链接) 次数: X | 影响: X`

---

## 5. 推送层：钉钉

加签算法（HMAC-SHA256）：
```
string_to_sign = f"{timestamp}\n{secret}"
sign = base64( hmac_sha256(secret, string_to_sign) )
url  = webhook + "&timestamp=" + timestamp + "&sign=" + urlencode(sign)
```
- `POST` `msgtype=markdown` 到 webhook。
- 顺序：先 Android 后 iOS（各自一条消息）。

---

## 6. 报告模板（钉钉 markdown，原版）

```
### Bugly 早报 (Android)
范围：**最近 24 小时**

#### **【崩溃率】**
设备崩溃率: `0.224%` (较昨日 下降 19.36%)
> 次数: `316` | 影响设备: `142`

=========================

#### **【错误率】**
设备错误率: `0.038%` (较昨日 上涨 19.34%)
> 错误次数(24h): `110` | 错误问题数: `93`

=========================

#### **近期Top 5 错误(Error)**

1. V1336 [string "views/inbox/item/InboxItem_GiftCodePopLayer.luac"][点击跳转](链接) 次数: 5 | 影响: 5
2. ...

🕒 生成时间: `2026-09-15 09:40`
```

---

## 7. 密钥与安全

- 三个 Secret 在 **GitHub 加密存储**，不入代码、不入仓库。
- `BUGLY_ACCESS_TOKEN` 是**纯令牌字符串**（脚本自己拼 `Bearer `，不要带前缀）。
- `DINGTALK_WEBHOOK` / `DINGTALK_SECRET` 决定推送到**哪个群**（当前 = 测试群，已钉死 `DINGTALK_ENV=test`）。

---

## 8. 旧链路退役状态

| 链路 | 状态 | 说明 |
|------|------|------|
| Windows 计划任务 **111** | ✅ 已禁用（2026-09-14） | 原 `bugly_report.py`，依赖 Chrome 9222 偷 token，最脆弱 |
| WorkBuddy 每日自动化 **64e46f0e** | ⚠️ 仍 ACTIVE | 会与原 GitHub 链路**双推**，待你确认停用 |
| 原 `bugly_report.py`（Chrome 取数） | 废弃 | 由独立脚本取代 |

---

## 9. 监控与排错

- **看结果**：GitHub 仓库 → **Actions** 页，绿色 ✓ = 成功；点进去看日志。
- **手动验证**：Actions → 选工作流 → **Run workflow**（随时可跑，不等定时）。
- **失败常见原因**：Secret 缺失 / `BUGLY_ACCESS_TOKEN` 过期 / Bugly 接口异常。
- **定时不准点**：免费 runner 高峰期延后 5~30 分钟，非故障。

---

## 10. 本次逻辑核查结论（2026-09-14）

- ✅ **cron 时区正确**：`40 1 * * *` = UTC 01:40 = 北京 09:40。
- ✅ **取数/计算/推送逻辑**已随你手动 Run workflow 验证通过（测试钉钉群收到双平台报告）。
- 🔧 **已修复**：报告「生成时间」原显示 UTC（01:40），已加 `TZ: Asia/Shanghai` 改为北京时间（不影响取数时间窗，因时间窗用 epoch 计算）。
- ⚠️ **待你决定**：WorkBuddy 自动化 `64e46f0e` 仍活跃，明天 09:40 会与 GitHub 链路**重复推送**两份报告，建议一并停用。
