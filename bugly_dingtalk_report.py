# -*- coding: utf-8 -*-
"""
Bugly 每日质量报告（脱离 WorkBuddy / 本机）独立脚本
取数：bugly-token MCP (streamableHttp, https://bugly.tds.qq.com/mcp, Bearer token)
推送：钉钉机器人 (HMAC-SHA256 加签)

特点：
- 零第三方依赖（仅用 Python 标准库），可直接跑在腾讯云 SCF / 任意公网主机。
- 由 cron-job.org 或 SCF 定时器触发，电脑关机也能推送。

环境变量：
  BUGLY_ACCESS_TOKEN  必填  Bugly OpenAPI 个人访问令牌
  BUGLY_MCP_URL       选填  默认 https://bugly.tds.qq.com/mcp
  DINGTALK_ENV        选填  test / prod，决定读 config.ini 的 [DingTalkTest]/[DingTalk]
  DINGTALK_WEBHOOK    选填  直接给 webhook（优先于 config.ini）
  DINGTALK_SECRET     选填  直接给 secret（优先于 config.ini）
  CONFIG_INI          选填  config.ini 路径，默认 D:/bugly/bugly/config.ini

用法：
  python bugly_dingtalk_report.py            # 正式跑（需 BUGLY_ACCESS_TOKEN）
  python bugly_dingtalk_report.py --demo      # 用内置样例数据打印报告，不发网络请求
  python bugly_dingtalk_report.py --demo --push   # 样例数据 + 真推送到钉钉（需钉钉凭据）
"""
import os
import sys
import re
import json
import time
import hmac
import hashlib
import base64
import urllib.parse
import urllib.request
import urllib.error
import configparser

# ---------------- 产品配置 ----------------
PRODUCTS = [
    {"name": "Android", "product_id": "e43ecf9d21", "pid": 1},
    {"name": "iOS",     "product_id": "6b0455dc18", "pid": 2},
]


# ---------------- 最小 MCP streamableHttp 客户端 ----------------
class BuglyMCP:
    def __init__(self, url, token):
        self.url = url
        self.token = token
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        }
        self.session_id = None
        self._id = 0

    @staticmethod
    def _http_post(url, headers, body_bytes):
        req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                status = r.status
                resp_headers = {k.lower(): v for k, v in r.getheaders()}
                text = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status = e.code
            resp_headers = {k.lower(): v for k, v in e.headers.items()}
            text = e.read().decode("utf-8", "replace")
        return status, resp_headers, text

    @staticmethod
    def _parse(text, content_type):
        if "text/event-stream" in content_type:
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    d = line[5:].strip()
                    try:
                        return json.loads(d)
                    except Exception:
                        pass
            return None
        try:
            return json.loads(text)
        except Exception:
            return None

    def _raw(self, method, params=None, notification=False):
        self._id += 1
        payload = {"jsonrpc": "2.0", "method": method}
        if not notification:
            payload["id"] = self._id
        if params is not None:
            payload["params"] = params
        h = dict(self.headers)
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        status, rh, text = self._http_post(self.url, h, json.dumps(payload).encode("utf-8"))
        sid = rh.get("mcp-session-id") or rh.get("Mcp-Session-Id")
        if sid and not self.session_id:
            self.session_id = sid
        return status, rh, text

    def initialize(self):
        status, rh, text = self._raw(
            "initialize",
            {"protocolVersion": "2024-11-05", "capabilities": {},
             "clientInfo": {"name": "bugly-report", "version": "1.0"}},
        )
        # 发送 initialized 通知（无响应）
        self._raw("notifications/initialized", notification=True)
        return self._parse(text, rh.get("content-type", ""))

    def call(self, name, arguments):
        status, rh, text = self._raw("tools/call", {"name": name, "arguments": arguments})
        res = self._parse(text, rh.get("content-type", ""))
        if not res:
            raise RuntimeError(f"[MCP] {name} 返回为空 (http={status})")
        if "error" in res:
            raise RuntimeError(f"[MCP] {name} 错误: {res['error']}")
        result = res.get("result", {})
        for c in result.get("content", []):
            if c.get("type") == "text":
                try:
                    return json.loads(c["text"])
                except Exception:
                    return c["text"]
        return result


# ---------------- 取数 ----------------
def get_crash(mcp, product_id):
    d = mcp.call("query_quality_overview",
                 {"product_id": product_id, "days_ago": 1, "metric_types": ["crash"]})
    m = d["products"][0]["metrics"][0]
    return (
        m["device_ratio"]["current"] * 100,
        m["device_ratio"]["rate"],
        m["count_ratio"]["current"],
        m["device_uv"]["current"],
    )


def get_error_token(mcp, product_id, now, now48):
    r = mcp.call("generate_token", {
        "product_id": product_id,
        "metric_type": "error",
        "form_list": {
            "time_window": 2,
            "issue_status": [],
            "tapd_status": [],
            "filters": [
                {"key": "", "name": "event_time", "operator": "GE", "values": [str(now48)]},
                {"key": "", "name": "event_time", "operator": "LE", "values": [str(now)]},
            ],
        },
    })
    return r["token"]


def get_error_rate(mcp, product_id, token):
    d = mcp.call("query_exception_rate_trend",
                 {"product_id": product_id, "metric_type": "error", "token": token})
    series = {m["metric_name"]: m["data_points"] for m in d["data"]}
    ratio = series["device_uv_ratio"]
    counts = series["count"]
    today = sum(ratio[24:]) / 24 * 100
    yest = sum(ratio[:24]) / 24 * 100
    rate = (today - yest) / yest * 100 if yest else 0.0
    err_count = sum(int(c) for c in counts[24:])
    return today, rate, err_count


def get_top5(mcp, product_id):
    d = mcp.call("query_top_issues",
                 {"product_id": product_id, "metric_type": "error", "days_ago": 1, "top_n": 5})
    return d.get("total", 0), d.get("issues", [])[:5]


# ---------------- 格式化 ----------------
def fmt_issue(issue, idx, product_id, pid):
    msg = issue.get("exception_msg", "")
    mver = re.search(r"V\d+", msg)
    version = mver.group(0) if mver else ""
    mstr = re.search(r'\[string "([^"]*)"\]', msg)
    if mstr:
        error_info = f'[string "{mstr.group(1)}"]'
    else:
        error_info = re.sub(r"^\s*google\s*", "", msg)
        error_info = re.sub(r"^V\d+\s*", "", error_info).strip()
        if len(error_info) > 80:
            error_info = error_info[:80] + "…"
    link = (f"https://bugly.tds.qq.com/v2/exception/error/dashboard"
            f"?productId={product_id}&pid={pid}")
    count = issue.get("count_current", 0)
    device = issue.get("device_count", 0)
    return f"{idx}. {version} {error_info}[点击跳转]({link}) 次数: {count} | 影响: {device}"


def trend(r):
    return f"较昨日 上涨 {abs(r):.2f}%" if r >= 0 else f"较昨日 下降 {abs(r):.2f}%"


def build_report(name, crash_rate, crash_change, crash_cnt, crash_uv,
                 err_rate, err_change, err_cnt, err_total, issues, product_id, pid, gen_time):
    top_lines = [fmt_issue(iss, i, product_id, pid) for i, iss in enumerate(issues, 1)]
    lines = [
        f"### Bugly 早报 ({name})",
        "范围：**最近 24 小时**",
        "",
        "#### **【崩溃率】**",
        f"设备崩溃率: `{crash_rate:.3f}%` ({trend(crash_change)})",
        f"> 次数: `{crash_cnt}` | 影响设备: `{crash_uv}`",
        "",
        "=========================",
        "",
        "#### **【错误率】**",
        f"设备错误率: `{err_rate:.3f}%` ({trend(err_change)})",
        f"> 错误次数(24h): `{err_cnt}` | 错误问题数: `{err_total}`",
        "",
        "=========================",
        "",
        "#### **近期Top 5 错误(Error)**",
        "",
    ]
    lines.extend(top_lines)
    lines.append("")
    lines.append(f"🕒 生成时间: `{gen_time}`")
    return "\n".join(lines)


# ---------------- 钉钉推送 ----------------
def load_dingtalk(env="test"):
    wh = os.environ.get("DINGTALK_WEBHOOK")
    sec = os.environ.get("DINGTALK_SECRET")
    if wh and sec:
        return wh, sec
    ini = os.environ.get("CONFIG_INI", "D:/bugly/bugly/config.ini")
    section = "DingTalkTest" if env == "test" else "DingTalk"
    cp = configparser.ConfigParser()
    cp.read(ini, encoding="utf-8")
    if cp.has_section(section):
        return cp.get(section, "webhook"), cp.get(section, "secret")
    raise RuntimeError("未找到钉钉凭据：请设置 DINGTALK_WEBHOOK/DINGTALK_SECRET 或 CONFIG_INI")


def send_dingtalk(webhook, secret, title, content):
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    sign = base64.b64encode(
        hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    sign = urllib.parse.quote(sign)
    url = f"{webhook}&timestamp={timestamp}&sign={sign}"
    body = json.dumps({"msgtype": "markdown",
                       "markdown": {"title": title, "text": content}}).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------- 主流程 ----------------
def run_once(mcp, wh, sec, env, gen_time):
    now = int(time.time())
    now48 = now - 172800
    results = []
    for p in PRODUCTS:
        name, pid_id, pid = p["name"], p["product_id"], p["pid"]
        crash_rate, crash_change, crash_cnt, crash_uv = get_crash(mcp, pid_id)
        etoken = get_error_token(mcp, pid_id, now, now48)
        err_rate, err_change, err_cnt = get_error_rate(mcp, pid_id, etoken)
        err_total, issues = get_top5(mcp, pid_id)
        report = build_report(name, crash_rate, crash_change, crash_cnt, crash_uv,
                              err_rate, err_change, err_cnt, err_total,
                              issues, pid_id, pid, gen_time)
        resp = send_dingtalk(wh, sec, f"{name}早报", report)
        results.append((name, report, resp))
        print(f"[{name}] 钉钉响应: {resp}")
    return results


def main():
    token = os.environ.get("BUGLY_ACCESS_TOKEN")
    if not token:
        print("ERROR: 未设置环境变量 BUGLY_ACCESS_TOKEN", file=sys.stderr)
        sys.exit(1)
    mcp_url = os.environ.get("BUGLY_MCP_URL", "https://bugly.tds.qq.com/mcp")
    env = os.environ.get("DINGTALK_ENV", "test")
    wh, sec = load_dingtalk(env)
    gen_time = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    mcp = BuglyMCP(mcp_url, token)
    mcp.initialize()
    run_once(mcp, wh, sec, env, gen_time)


# ---------------- 演示 / 自测 ----------------
def demo(push=False):
    gen_time = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    sample_issues = [
        {"exception_msg": 'google V1336 [string "views/inbox/item/InboxItem_GiftCodePopLayer.luac"]:313: attempt to compare userdata with nil', "count_current": 5, "device_count": 5},
        {"exception_msg": 'google V1336 [string "CodeGameScreenClassicRapid2Machine.luac"]:1925: attempt to index local \'node\' (a nil value)', "count_current": 4, "device_count": 4},
        {"exception_msg": 'google V1336 display.newSprite() - create sprite failure, source "#PandasPouchesSymbol/Socre_PandasPouches_3.png"', "count_current": 3, "device_count": 3},
    ]
    for p in PRODUCTS:
        report = build_report(
            p["name"], 0.224, -19.36, 316, 142,
            0.038, 19.34, 110, 93,
            sample_issues, p["product_id"], p["pid"], gen_time,
        )
        print("=" * 30, p["name"], "=" * 30)
        print(report)
        print()
        if push:
            wh, sec = load_dingtalk(os.environ.get("DINGTALK_ENV", "test"))
            resp = send_dingtalk(wh, sec, f"{p['name']}早报(DEMO)", report)
            print(f"[{p['name']}] 钉钉响应: {resp}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--demo":
        demo(push=("--push" in sys.argv))
    else:
        main()


# ---------------- 腾讯云 SCF 入口 ----------------
def main_handler(event, context):
    try:
        main()
        return {"statusCode": 200, "body": "Bugly report sent"}
    except Exception as e:
        return {"statusCode": 500, "body": str(e)}
