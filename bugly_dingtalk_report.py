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


# ---------------- 通用工具 ----------------
# 本机可能挂着 HTTP_PROXY（如 127.0.0.1:8058），urllib 默认会读取并走代理导致请求被拦。
# 显式构造带空 ProxyHandler 的 opener，绕过所有代理环境变量（与 dingtalk_push.py 的
# trust_env=False 等价）。GitHub runner 上没有代理，此改动对线上无副作用。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _num(v, name="value"):
    """把取数结果安全转成 float。

    None / 非数字直接报错，而不是当成 0 —— 早报里一个假的 0% 比没有报告更危险。
    """
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if v is None:
        raise RuntimeError(f"[取数失败] {name} 为 null")
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        raise RuntimeError(f"[取数失败] {name} 不是数字: {v!r}")


def _num_opt(v, name="value"):
    """可缺省的数值：取不到时返回 None，由展示层显示「无可比数据」，不让整份报告失败。"""
    try:
        return _num(v, name)
    except RuntimeError:
        return None


def _dig(obj, *path, what="响应"):
    """按路径逐层索引，失败时抛出带上下文的错误（而不是裸 KeyError/IndexError）。"""
    cur = obj
    for k in path:
        try:
            cur = cur[k]
        except (KeyError, IndexError, TypeError):
            where = f"索引 {k}" if isinstance(k, int) else f"字段 {k!r}"
            raise RuntimeError(
                f"[取数失败] {what} 缺少{where}，实际结构: {str(obj)[:200]}"
            )
    return cur


# ---------------- 最小 MCP streamableHttp 客户端 ----------------
class BuglyMCP:
    RETRY_DELAYS = (1, 3)  # 失败后的等待秒数，共尝试 len(RETRY_DELAYS)+1 = 3 次

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
            with _OPENER.open(req, timeout=30) as r:
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
        body = json.dumps(payload).encode("utf-8")

        # 传输层重试：只对网络异常 / 5xx / 429 重试，业务错误与 4xx 立即失败。
        # 退避 1s、3s，共 3 次尝试 —— 覆盖单次网络抖动，又不至于把 workflow 拖长。
        last_err = None
        for attempt in range(len(self.RETRY_DELAYS) + 1):
            try:
                status, rh, text = self._http_post(self.url, h, body)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last_err = f"网络异常 {e!r}"
            else:
                if status < 500 and status != 429:
                    sid = rh.get("mcp-session-id") or rh.get("Mcp-Session-Id")
                    if sid and not self.session_id:
                        self.session_id = sid
                    return status, rh, text
                last_err = f"服务端错误 http={status}"
            if attempt < len(self.RETRY_DELAYS):
                time.sleep(self.RETRY_DELAYS[attempt])
        raise RuntimeError(
            f"[MCP] {method} 重试 {len(self.RETRY_DELAYS) + 1} 次仍失败: {last_err}"
        )

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
        if not isinstance(res, dict):
            raise RuntimeError(f"[MCP] {name} 返回非 JSON 对象 (http={status}): {str(res)[:200]}")
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
    m = _dig(d, "products", 0, "metrics", 0, what="query_quality_overview")
    return (
        _num(_dig(m, "device_ratio", "current", what="device_ratio"), "device_ratio.current") * 100,
        _num_opt(_dig(m, "device_ratio", "rate", what="device_ratio"), "device_ratio.rate"),
        _num(_dig(m, "count_ratio", "current", what="count_ratio"), "count_ratio.current"),
        _num(_dig(m, "device_uv", "current", what="device_uv"), "device_uv.current"),
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
    return _dig(r, "token", what="generate_token")


def get_error_rate(mcp, product_id, token):
    d = mcp.call("query_exception_rate_trend",
                 {"product_id": product_id, "metric_type": "error", "token": token})
    series = {}
    for item in _dig(d, "data", what="query_exception_rate_trend"):
        series[item.get("metric_name")] = item.get("data_points") or []
    ratio = series.get("device_uv_ratio")
    counts = series.get("count")
    if not ratio or counts is None:
        raise RuntimeError(
            "[取数失败] query_exception_rate_trend 缺少 device_uv_ratio/count，"
            f"实际返回: {sorted(series)}"
        )
    # 不再假定「恰好 48 个点」：按实际长度对半分，均值用实际点数当除数。
    # 若某小时无数据被裁剪，写死 24 会切错位置、算出一个「看起来正常但是错的」错误率。
    n = len(ratio)
    if n < 2:
        raise RuntimeError(f"[取数失败] 趋势点数过少: {n}")
    half = n // 2
    yest_pts, today_pts = ratio[:half], ratio[half:]
    today = sum(_num(x, "device_uv_ratio") for x in today_pts) / len(today_pts) * 100
    yest = (sum(_num(x, "device_uv_ratio") for x in yest_pts) / len(yest_pts) * 100
            if yest_pts else 0.0)
    rate = (today - yest) / yest * 100 if yest else 0.0
    # counts 理论长度与 ratio 一致；万一更短，按实际长度切，避免切片为空或越界
    cut = min(half, len(counts))
    err_count = int(sum(_num(c, "count") for c in counts[cut:]))
    return today, rate, err_count


def get_top5(mcp, product_id):
    d = mcp.call("query_top_issues",
                 {"product_id": product_id, "metric_type": "error", "days_ago": 1, "top_n": 5})
    return d.get("total", 0), d.get("issues", [])[:5]


# ---------------- 格式化 ----------------
# 异常文本里混入 markdown 元字符时，钉钉会把它渲染成链接/图片/粗斜体，
# 典型症状：`!` + `[string "..."]` 变成图片语法（消息里出现加载失败的灰块），
#           或以 `]` 结尾时与后面的 `[点击跳转](url)` 拼成引用式链接（文字黏连）。
# 统一换成全角，保证按纯文本显示。
# 只处理真会引发解析的字符；下划线是文件名/类名里的高频字符且单个不会触发斜体，
# 圆括号/竖线在方括号被转义后也已无风险 —— 这些保留半角，可读性优先。
_MD_ESCAPE = str.maketrans({
    "[": "［", "]": "］", "!": "！", "*": "＊", "`": "｀", "~": "～",
})


def esc_md(s):
    """把异常文本里的 markdown 元字符替换为全角，避免被钉钉解析。"""
    return s.translate(_MD_ESCAPE)


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
    # 先截断再转义：全角字符不影响长度语义，但避免在转义后切断实体
    if len(error_info) > 80:
        error_info = error_info[:80] + "…"
    error_info = esc_md(error_info)
    link = (f"https://bugly.tds.qq.com/v2/exception/error/dashboard"
            f"?productId={product_id}&pid={pid}")
    count = issue.get("count_current", 0)
    device = issue.get("device_count", 0)
    # 链接放行尾：异常文本后面紧跟「次数」，天然不会与 [点击跳转] 黏成引用式链接
    return f"{idx}. {version} {error_info} 次数: {count} | 影响: {device} [点击跳转]({link})"


def trend(r):
    if r is None:
        return "较昨日 无可比数据"
    return f"较昨日 上涨 {abs(r):.2f}%" if r >= 0 else f"较昨日 下降 {abs(r):.2f}%"


def build_report(name, crash_rate, crash_change, crash_cnt, crash_uv,
                 err_rate, err_change, err_cnt, err_total, issues, product_id, pid, gen_time):
    top_lines = [fmt_issue(iss, i, product_id, pid) for i, iss in enumerate(issues, 1)]
    if not top_lines:
        # 空列表不能只留个标题，否则看起来像「零错误」，实际可能是取数没拿到
        top_lines = ["（本期无错误数据）"]
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
    # webhook 是否已带 query 不确定，自适应拼 ? 或 &，避免拼成 ".../send&timestamp=" 直接 401
    sep = "&" if "?" in webhook else "?"
    url = f"{webhook}{sep}timestamp={timestamp}&sign={sign}"
    body = json.dumps({"msgtype": "markdown",
                       "markdown": {"title": title, "text": content}}).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with _OPENER.open(req, timeout=10) as r:
        resp = json.loads(r.read().decode("utf-8"))
    # 钉钉业务失败同样返回 HTTP 200，必须看 errcode。
    # 否则「群没收到报告」会被当成功，Actions 打绿勾，问题被完全隐藏。
    if resp.get("errcode") != 0:
        raise RuntimeError(
            f"钉钉推送失败: errcode={resp.get('errcode')} errmsg={resp.get('errmsg')}"
        )
    return resp


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
    try:
        mcp.initialize()
        run_once(mcp, wh, sec, env, gen_time)
    except Exception as e:
        # 打一行机器可读的失败标记（方便在 Actions 日志里 grep），再重新抛出：
        # 非 0 退出码会让 workflow 变红，GitHub 会给仓库发失败邮件，不会被静默吞掉。
        print(f"REPORT_FAILED: {e}", file=sys.stderr)
        raise


# ---------------- 演示 / 自测 ----------------
def demo(push=False):
    gen_time = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    sample_issues = [
        {"exception_msg": 'google V1336 [string "views/inbox/item/InboxItem_GiftCodePopLayer.luac"]:313: attempt to compare userdata with nil', "count_current": 5, "device_count": 5},
        {"exception_msg": 'google V1336 [string "CodeGameScreenClassicRapid2Machine.luac"]:1925: attempt to index local \'node\' (a nil value)', "count_current": 4, "device_count": 4},
        {"exception_msg": 'google V1336 display.newSprite() - create sprite failure, source "#PandasPouchesSymbol/Socre_PandasPouches_3.png"', "count_current": 3, "device_count": 3},
        # 脏数据回归样例：`!` 紧邻 [string ...] 会拼成 markdown 图片语法（线上出现过灰块占位）
        {"exception_msg": 'google V1352 fail![string "DarkfinDominionPublicConfig.luac"] is invalid!!', "count_current": 3, "device_count": 3},
        # 脏数据回归样例：`[xx]` 结尾会与 [点击跳转] 拼成引用式链接
        {"exception_msg": 'google V1353 bindRef[Activity_WickedWhack] is invalid!!', "count_current": 2, "device_count": 2},
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
