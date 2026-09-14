# -*- coding: utf-8 -*-
"""
钉钉推送助手（自包含，不依赖 signer.py）。
用标准 HMAC-SHA256 签名后 POST 到钉钉机器人 webhook。

凭据从 D:/bugly/bugly/config.ini 读取：
  [DingTalk]     正式环境 webhook + secret
  [DingTalkTest] 测试环境 webhook + secret   <-- 当前默认
用 --env test|prod 切换，默认 test（测试环境）。

钉钉安全规范（加签）：
  1. timestamp = 当前毫秒时间戳
  2. string_to_sign = f"{timestamp}\n{secret}"
  3. sign = urlencode(base64(HMAC-SHA256(secret, string_to_sign)))
  4. 请求地址追加 &timestamp=..&sign=..

用法：
  python dingtalk_push.py --env test --title "Android早报" --file report.md
  python dingtalk_push.py --env test --markdown "### hello"
  python dingtalk_push.py --env test --text "纯文本消息" [--at-all]
"""
import sys
import time
import argparse
import configparser
import requests
import hmac
import hashlib
import base64
import urllib.parse

CONFIG_PATH = r"D:\bugly\bugly\config.ini"

# 环境 -> config.ini 中的 section
ENV_SECTION = {
    "prod": "DingTalk",
    "test": "DingTalkTest",
}


def get_dingtalk_signature(secret):
    """钉钉安全规范：timestamp + secret 做 HMAC-SHA256，结果 base64 后 URL 编码。"""
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote(base64.b64encode(hmac_code))
    return timestamp, sign


def _post(webhook, secret, payload):
    """通用的钉钉 POST：加签 + 发送 + 返回响应 JSON，异常向上抛。"""
    ts, sign = get_dingtalk_signature(secret)
    url = f"{webhook}&timestamp={ts}&sign={sign}"
    sess = requests.Session()
    sess.trust_env = False  # 避免任何环境的 HTTP_PROXY 拦截（如 127.0.0.1:8058）
    r = sess.post(url, json=payload, timeout=10)
    r.raise_for_status()  # HTTP 非 2xx 抛 requests.HTTPError
    return r.json()


def send_text(webhook, secret, content, at_mobiles=None, at_all=False):
    """发送 text 类型消息。

    参数：
      webhook    : 钉钉机器人地址（含 access_token）
      secret     : 加签密钥
      content    : 纯文本内容
      at_mobiles : 需要 @ 的手机号列表（可选）
      at_all     : 是否 @ 所有人（可选）
    返回：钉钉接口响应 dict（含 errcode/errmsg）
    异常：网络错误 / HTTP 非 2xx 会向上抛出，由调用方处理
    """
    payload = {
        "msgtype": "text",
        "text": {"content": content},
    }
    if at_all:
        payload["at"] = {"isAtAll": True}
    elif at_mobiles:
        payload["at"] = {"atMobiles": list(at_mobiles)}
    return _post(webhook, secret, payload)


def send_markdown(webhook, secret, title, text):
    """发送 markdown 类型消息（用于每日质量报告）。"""
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text},
    }
    return _post(webhook, secret, payload)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", choices=["prod", "test"], default="test",
                    help="钉钉环境，默认 test（测试环境）")
    ap.add_argument("--title", help="markdown 标题")
    ap.add_argument("--markdown", help="markdown 文本")
    ap.add_argument("--file", help="markdown 文件路径")
    ap.add_argument("--text", help="text 纯文本消息")
    ap.add_argument("--at-all", action="store_true", help="text 消息 @ 所有人")
    args = ap.parse_args()

    cfg = configparser.ConfigParser()
    read_ok = cfg.read(CONFIG_PATH, encoding="utf-8")
    if not read_ok:
        print("CONFIG_READ_ERROR", CONFIG_PATH)
        return 2
    section = ENV_SECTION[args.env]
    if not cfg.has_section(section):
        print("NO_SECTION", section, "in", CONFIG_PATH)
        return 2
    webhook = cfg.get(section, "webhook")
    secret = cfg.get(section, "secret")

    try:
        if args.text is not None:
            resp = send_text(webhook, secret, args.text, at_all=args.at_all)
            print("TEXT_RESULT", resp)
        else:
            if args.file:
                with open(args.file, "r", encoding="utf-8") as f:
                    content = f.read()
            else:
                content = args.markdown or ""
            title = args.title or "Bugly 报告"
            resp = send_markdown(webhook, secret, title, content)
            print("MARKDOWN_RESULT", resp)

        if resp.get("errcode") == 0:
            return 0
        print("DINGTALK_ERR", resp)
        return 1
    except Exception as e:
        # 异常统一处理：打印可读错误，返回非 0 退出码
        print("PUSH_ERROR", repr(e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
