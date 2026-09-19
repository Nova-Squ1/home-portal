#!/usr/bin/env python3
"""LLM 邮件审查：判断未读邮件是否重要。

- OpenAI 兼容 /chat/completions 接口（stdlib urllib，无第三方依赖）；
- 判定结果按 Message-ID 持久化缓存到 data/triage_cache.json，同一封邮件只问一次；
- 任何失败返回 None，由调用方回退到关键词规则，保证不阻塞邮件展示。
"""

import json
import os
import time
import urllib.request
import urllib.error

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "triage_cache.json")
CACHE_MAX = 4000
HTTP_TIMEOUT = 25

SYSTEM_PROMPT = (
    "你是个人邮件助理，判断一封邮件对收件人是否“重要”，必须严格只输出 JSON："
    '{"important": true/false, "category": "类别", "reason": "不超过20字中文理由"}。\n'
    "重要的标准（满足任一即为 true）：\n"
    "1. 来自学校（.edu / .edu.cn 等教育机构域名）、银行、支付、运营商、政府、人力/招聘方的正式通知；\n"
    "2. 验证码、安全告警、登录提醒、账单、发票、快递、机票火车票、预约确认；\n"
    "3. 认识的人直接发给本人的私人邮件（非群发列表）；\n"
    "4. 与本人项目、服务器、域名、订阅服务相关的状态通知。\n"
    "以下判为 false：营销推广、newsletter、产品更新、优惠券、群发资讯、自动日报、"
    "社交平台动态、会议群发邀请、明显垃圾邮件。\n"
    "category 从 [学业, 财务, 账号安全, 私人, 服务通知, 招聘, 其他] 中选；不重要时 category 填 推广/垃圾/通知 等简述。"
)


def load_cache():
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache):
    tmp = CACHE_PATH + ".tmp"
    if len(cache) > CACHE_MAX:
        items = sorted(cache.items(), key=lambda kv: kv[1].get("ts", 0))
        cache = dict(items[-CACHE_MAX:])
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass


def classify(msg, cfg, cache):
    """msg: dict(message_id, from, subject, body_preview)。
    返回 dict(important, category, reason) 或 None（调用失败）。"""
    mid = (msg.get("message_id") or "").strip()
    if mid and mid in cache:
        hit = cache[mid]
        return {k: hit.get(k) for k in ("important", "category", "reason")}

    content = (
        "发件人: " + (msg.get("from") or "")[:200] + "\n"
        "主题: " + (msg.get("subject") or "")[:300] + "\n"
        "正文摘要:\n" + (msg.get("body_preview") or "")[:1500]
    )
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": 0,
        "max_completion_tokens": 120,
    }
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg["api_key"],
            "User-Agent": "home-portal-mail-triage/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"].strip()
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError,
            IndexError, ValueError, TimeoutError, OSError) as exc:
        print("llm triage error:", type(exc).__name__, exc, flush=True)
        return None

    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1 or e <= s:
        return None
    try:
        verdict = json.loads(text[s:e + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(verdict.get("important"), bool):
        return None
    result = {
        "important": verdict["important"],
        "category": str(verdict.get("category", "其他"))[:20],
        "reason": str(verdict.get("reason", ""))[:60],
    }
    if mid:
        cache[mid] = dict(result, ts=int(time.time()))
        save_cache(cache)
    return result
