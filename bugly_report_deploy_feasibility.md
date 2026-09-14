# Bugly 每日报告「脱离本机」部署可行性方案

## 结论
✅ **可以部署到 cron-job.org（或任意公网定时服务）**，彻底摆脱"电脑关机推不出去"的问题。

## 关键调研发现（实测）
1. **WorkBuddy 自动化是本机/客户端执行**：电脑关机或 app 不在线，任务不触发。
   佐证：9/14 当天 09:40 未自动跑（最终 10:03 手动触发），说明依赖本机 app 在线。
2. **bugly-token MCP 实际入口是公网地址**：
   - 生效配置 = `https://bugly.tds.qq.com/mcp`（**不是** SKILL.md 里写的默认值 `bugly.mcp.it.woa.com`）。
   - 实测：`bugly.tds.qq.com` 解析到腾讯云公网 IP，**TCP 443 连通**。
   - MCP 端点对无 token 请求返回 **401**，鉴权 = 标准 `Authorization: Bearer <BUGLY_ACCESS_TOKEN>`（Bugly OpenAPI 个人访问令牌）。
   - ⇒ 只要持有 token，**任意公网主机都能直接调它**，不依赖 WorkBuddy、不依赖本机。
3. **钉钉推送逻辑已具备**：`dingtalk_push.py`（HMAC-SHA256 加签）可原样复用。

## 目标架构
```
cron-job.org  (每日 09:40 定时)
      │  HTTPS GET/POST
      ▼
你的 7×24 托管端点 (VPS / 腾讯云 SCF / 云函数)
      │  ① 作为 MCP client 调 bugly.tds.qq.com/mcp（带 Bearer token）
      │  ② 取 4 类数据 → 拼报告
      │  ③ 调钉钉 webhook 推送
      ▼
钉钉群
```

## 需要的交付物
1. **Token**：`BUGLY_ACCESS_TOKEN`（Bugly OpenAPI 个人令牌）。
   建议从 Bugly 控制台重新生成一个**专用令牌**，作为托管环境的 secret 环境变量，不写进代码/仓库。
   （本地虽存有加密凭据，但为安全与可移植，推荐用独立新令牌。）
2. **最小 MCP client（Python）**：实现 streamableHttp 握手
   `initialize → notifications/initialized → tools/call`。这是唯一技术难点。
3. **报告逻辑**：复用现有——
   崩溃率 `query_quality_overview`、错误率 `query_exception_rate_trend`(+`generate_token`)、Top5 `query_top_issues`。
4. **钉钉推送**：复用 `dingtalk_push.py`。
5. **HTTP 端点**：在 7×24 主机暴露一个接口供 cron-job.org 触发。

## 托管方案对比
| 方案 | 可达 bugly | 复杂度 | 说明 |
|------|-----------|--------|------|
| **腾讯云 SCF（推荐）** | ✅ 天然可达 | 低 | 公网 URL、按调用计费、无需常驻；用户是腾讯，最顺 |
| 任意公网 VPS / 容器 | ✅ | 低 | 跑一个 Flask/FastAPI 小服务即可 |
| Cloudflare Workers / Vercel | ✅ | 中 | 需打包 Python 依赖，较繁琐 |

> 注意：SKILL.md 写的 `bugly.mcp.it.woa.com` 是默认值且当前**未启用**（disabled），真实用的是 `bugly.tds.qq.com/mcp`，二者都是公网可达，无需腾讯内网/VPN。

## cron-job.org 配置要点
- 新建 Job → URL 填你的端点公网地址 → 时间选「每日 09:40」（注意该站时区设置，必要时用 Cron `40 1 * * *` 对应北京时间）。
- 建议开启失败重试 + 通知邮件。

## 安全
- `BUGLY_ACCESS_TOKEN` 与钉钉 `secret` 仅存于托管环境 secret/env，不入库、不进聊天。
- 脚本读环境变量，不硬编码。

## 下一步（待确认）
- 是否现在就写独立脚本（MCP client + 报告 + 钉钉）？
- 托管在哪？（SCF / VPS / 先本地出脚本你自行部署）
