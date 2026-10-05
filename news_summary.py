#!/usr/bin/env python3
"""news_summary.py — 给今日热帖生成省流摘要（Z.AI Coding Plan glm-5.3 主力，Codex gpt-5.6-luna 回退）。

主链路：https://api.z.ai/api/coding/paas/v4/chat/completions，key 读
/root/.hermes/.env 的 ZAI_API_KEY（home-portal 以 root 运行可直接读）。
回退链路：Codex OAuth /root/.codex/auth.json →
https://chatgpt.com/backend-api/codex/responses（必须 stream=true）。
按 URL 缓存摘要到 data/news_summary_cache.json，同一帖子只总结一次。
任何失败返回 None，不阻塞新闻展示。
"""
import json
import os
import re
import threading
import time
import urllib.request
import urllib.error

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(BASE_DIR, "data", "news_summary_cache.json")
CODEX_AUTH = "/root/.codex/auth.json"
HERMES_ENV = "/root/.hermes/.env"
ZAI_MODEL = "glm-5.3"
CODEX_MODEL = "gpt-5.6-luna"
CACHE_MAX = 3000
HTTP_TIMEOUT = 90

SYSTEM_PROMPT = (
    "你是技术社区帖子的省流编辑。输入一个热帖的标题和内容摘要，输出一篇 300~500 字的中文省流长文（字数不足 300 视为不合格），要求：\n"
    "1. 开头一两句直击核心（发生了什么/发布了什么/结论是什么）；\n"
    "2. 然后分层次展开帖子的精华内容：技术细节、实现思路、关键数据、性能对比、作者的核心论据和结论；有步骤/列表的内容保留要点；\n"
    "3. 保留所有具体信息：数字、型号/版本名、链接指向的东西、价格、适用人群、注意事项；\n"
    "4. 语气客观紧凑，不要客套、不要重复标题、不要'本文介绍了'式套话；\n"
    "5. 篇幅必须写满：把输入内容里的技术要点尽量全部覆盖展开，宁可详细不可简略；\n"
    "直接输出省流正文，不要任何前后缀，不要 markdown 标题。"
)

_lock = threading.Lock()


def _load_cache():
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache):
    if len(cache) > CACHE_MAX:
        items = sorted(cache.items(), key=lambda kv: kv[1].get("ts", 0))
        cache = dict(items[-CACHE_MAX:])
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, CACHE_PATH)


def _load_token():
    try:
        with open(CODEX_AUTH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d["tokens"]["access_token"], d["tokens"].get("account_id")
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None, None


def _load_zai_key():
    try:
        with open(HERMES_ENV, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("ZAI_API_KEY="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def summarize(title, summary, url):
    """返回省流摘要字符串；失败或已有缓存时快速返回。"""
    key = url.split("#")[0].rstrip("/")
    with _lock:
        cache = _load_cache()
        hit = cache.get(key)
        if hit and hit.get("tldr"):
            return hit["tldr"]

    if not (summary or "").strip():
        return None  # 正文空（水源限流留空）没有可总结的内容，等下轮抓到正文再生成
    content = "标题: " + title[:200] + "\n内容: " + (summary or "（无）")[:3000]

    tldr = _summarize_zai(content) or _summarize_codex(content)

    if not tldr:
        return None
    tldr = re.sub(r"\s+", " ", tldr)[:1300]
    with _lock:
        cache = _load_cache()
        cache[key] = {"tldr": tldr, "ts": int(time.time())}
        try:
            _save_cache(cache)
        except OSError:
            pass
    return tldr


def _summarize_zai(content, system=None, max_tokens=2400):
    """Z.AI Coding Plan glm-5.3（主力）。"""
    key = _load_zai_key()
    if not key:
        return None
    payload = {
        "model": ZAI_MODEL,
        "messages": [
            {"role": "system", "content": system or SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.4,
    }
    req = urllib.request.Request(
        "https://api.z.ai/api/coding/paas/v4/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "User-Agent": "home-portal-news/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.load(resp)
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip() or None
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError, ValueError) as exc:
        print("zai summary error:", type(exc).__name__, exc, flush=True)
        return None


def translate_titles(titles):
    """把一批英文论文标题译成中文；返回等长列表，失败返回 None。"""
    out = _summarize_zai(json.dumps(titles, ensure_ascii=False), system=(
        "把输入 JSON 数组里的每个英文论文标题翻译成简洁准确的中文标题，模型名、方法名、缩写等专有名词保留原文。"
        "只输出一个 JSON 字符串数组，长度和顺序与输入完全一致，不要任何解释。"), max_tokens=6000)
    try:
        zh = json.loads(out[out.index("["):out.rindex("]") + 1])
    except (TypeError, ValueError, AttributeError):
        return None
    if len(zh) != len(titles) or not all(isinstance(z, str) and z.strip() for z in zh):
        return None
    return [z.strip() for z in zh]


def _summarize_codex(content):
    """Codex gpt-5.6-luna（回退）。"""
    return codex_complete(SYSTEM_PROMPT, content)


def codex_complete(instructions, content, effort=None):
    """调一次 Codex（CODEX_MODEL）拿纯文本输出，任何失败返回 None。翻译接口 translate.py 也用它。"""
    token, account = _load_token()
    if not token:
        return None
    payload = {
        "model": CODEX_MODEL,
        "instructions": instructions,
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": content}]}],
        "store": False,
        "stream": True,
    }
    if effort:
        payload["reasoning"] = {"effort": effort}
    req = urllib.request.Request(
        "https://chatgpt.com/backend-api/codex/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
            "chatgpt-account-id": account or "",
            "User-Agent": "home-portal-news/1.0",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    try:
        parts = []
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            for raw in resp:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                ev = line[6:]
                if ev == "[DONE]":
                    break
                try:
                    obj = json.loads(ev)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "response.output_text.delta":
                    parts.append(obj.get("delta") or "")
        text = "".join(parts).strip()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
        print("codex error:", type(exc).__name__, exc, flush=True)
        return None
    return text or None
