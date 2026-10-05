#!/usr/bin/env python3
"""LLM 邮件审查：判断未读邮件是否重要。

- 主力 Jev（TypeSafe System One，2026-09-23 起）：POST /v1/systemone，
  Noul 重要度 + Choice 分类 + Score 紧急度一次往返，强类型概率输出；
  key 读 /root/.hermes/.env 的 TYPESAFE_API_KEY；
- 回退：OpenAI 兼容 /chat/completions（config/llm.yml 的 base_url/model/api_key）；
- 判定结果按 Message-ID 持久化缓存到 data/triage_cache.json，同一封邮件只问一次；
- 任何失败返回 None，由调用方回退到关键词规则，保证不阻塞邮件展示。
"""

import json
import os
import time
import urllib.request
import urllib.error

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(BASE_DIR, "data", "triage_cache.json")
HERMES_ENV = "/root/.hermes/.env"
JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
IMPORTANT_THRESHOLD = 0.5  # Noul 概率 ≥ 0.5 视为重要
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

    # 主力：Jev
    result = _classify_jev(content)
    if result is not None:
        if mid:
            cache[mid] = dict(result, ts=int(time.time()))
            save_cache(cache)
        return result
    # 回退：OpenAI 兼容（原逻辑，cfg 可能为 None 则跳过）
    if not cfg or not cfg.get("api_key"):
        return None


def _load_typesafe_key():
    try:
        with open(HERMES_ENV, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("TYPESAFE_API_KEY="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


JEV_CATEGORY = {
    "school": "学业", "bill": "财务", "security": "账号安全",
    "social": "私人", "service": "服务通知", "recruit": "招聘", "other": "其他",
}
JEV_URGENCY_TXT = ("不急", "本周内处理", "需要今明两天处理")


def _classify_jev(content):
    """Jev System One：一次往返拿 重要度(Noul)+分类(Choice)+紧急度(Score)。失败返回 None。"""
    key = _load_typesafe_key()
    if not key:
        return None
    payload = {
        "model": JEV_MODEL,
        "state": content,
        "questions": {
            "important": {
                "type": "noul",
                "instructions": "这封邮件对收件人重要吗？重要=需要及时看到和处理（学校/教务通知、账单付款、银行、本人操作触发的验证码、本人订阅服务的状态异常告警、认识的人的私人来信）；不重要=营销推广、newsletter、优惠券、群发资讯、社交平台动态、钓鱼垃圾、Google 账号例行安全提醒/活动记录、Facebook/Instagram/Reddit 等社交平台通知、快递物流自动更新（速递）、例行系统巡检报告。注意：验证码和银行告警若由本人操作触发则重要，例行推送的不重要",
                "criteria": {"true": "需要本人及时处理的真实邮件", "false": "营销、群发、垃圾、钓鱼、社交平台通知、Google 例行安全提醒、快递自动更新、机器人例行报告"},
            },
            "category": {
                "type": "choice",
                "instructions": "这封邮件属于哪一类？",
                "criteria": {
                    "school": "学校、课程、教务、学业",
                    "bill": "账单、付款、订阅扣费、发票",
                    "security": "账号安全、验证码、登录告警",
                    "social": "认识的人的私人来信、社交",
                    "service": "服务器/域名/订阅服务状态通知、快递、票务、预约",
                    "recruit": "招聘、实习、HR",
                    "other": "其他",
                },
            },
            "urgency": {
                "type": "score",
                "instructions": "这封邮件的紧急程度？",
                "criteria": ["不急，有空再看", "本周内处理", "需要今明两天处理"],
            },
        },
    }
    req = urllib.request.Request(
        JEV_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "User-Agent": "home-portal-mail-triage/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        answers = data.get("answers", {})
        imp = answers.get("important", {}).get("noul")
        cat = answers.get("category", {}).get("choice", "other")
        urg = answers.get("urgency", {}).get("score")
        if imp is None:
            return None
        urg_txt = JEV_URGENCY_TXT[int(urg)] if isinstance(urg, (int, float)) and 0 <= urg < 3 else ""
        return {
            "important": bool(imp >= IMPORTANT_THRESHOLD),
            "category": JEV_CATEGORY.get(cat, "其他"),
            "reason": f"Jev p={imp:.2f} 紧急度:{urg_txt}".strip(),
        }
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError,
            IndexError, ValueError, TimeoutError, OSError) as exc:
        print("jev triage error:", type(exc).__name__, exc, flush=True)
        return None


def _classify_openai(content, cfg, mid, cache):
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
