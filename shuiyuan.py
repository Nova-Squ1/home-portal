#!/usr/bin/env python3
"""shuiyuan.py — 水源社区（shuiyuan.sjtu.edu.cn，Discourse）抓取模块。

认证：User-Api-Key（永久有效，除非用户在水源设置里撤销）。
  key 存 config/shuiyuan/key.json（600）；解密私钥 sy_priv.pem 同目录。
  重新授权流程：生成 RSA 密钥对 -> 构造 /user-api-key/new 授权 URL（需 auth_key/
  client_id/nonce 参数）-> 用户浏览器批准 -> 把页面显示的 encrypted_key 用私钥解密。

抓取目标分类（技术 + 经验分享，用户 2026-09-20 指定）：
  51 极客时间 / 66 硬件产品 / 77 软件应用   （数码科技）
  92 职场生涯 / 89 学习进阶 / 91 境外求索   （人生经验：实习/求职/自我提升）
  85 科研科创 / 75 深度讨论
每个分类取 /c/<id>.json 最新话题（限 2 天内），热度前 16 条取首楼 cooked 文本
（截 1200 字）做省流输入——只看第一楼，省 token。
限速：分类间 sleep 1s，逐帖 0.5s。
UA 用 'curl/8.5.0'：水源 Cloudflare 对浏览器 UA 挑战（同 linux.do 的指纹问题）。
"""
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from html import unescape

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SY_DIR = os.path.join(BASE_DIR, "config", "shuiyuan")
KEY_PATH = os.path.join(SY_DIR, "key.json")

HOST = "https://shuiyuan.sjtu.edu.cn"
UA = "curl/8.5.0"

WANT_CATEGORIES = {
    51: "极客时间", 66: "硬件产品", 77: "软件应用",
    92: "职场生涯", 89: "学习进阶", 91: "境外求索",
    85: "科研科创", 75: "深度讨论",
}

TZ = timezone(timedelta(hours=8))


def load_key():
    try:
        with open(KEY_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("key")
    except (OSError, json.JSONDecodeError):
        return None


def _api(path, timeout=25):
    key = load_key()
    if not key:
        raise RuntimeError("shuiyuan user-api-key not configured")
    req = urllib.request.Request(HOST + path, headers={
        "User-Agent": UA,
        "User-Api-Key": key,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _strip_html(html):
    text = re.sub(r"<[^>]+>", " ", html or "")
    return unescape(text).strip()


def fetch_latest(max_age_days=2, per_category=8, with_first_post=True):
    """各目标分类最新话题；首楼 cooked 文本（截 1200 字）做省流输入。"""
    now = datetime.now(TZ)
    items = []
    for cid, cname in WANT_CATEGORIES.items():
        try:
            data = _api("/c/%d.json" % cid)
        except Exception:  # noqa: BLE001
            time.sleep(1)
            continue
        topics = (data.get("topic_list") or {}).get("topics") or []
        picked = 0
        for t in topics:
            if picked >= per_category:
                break
            created = t.get("created_at") or ""
            try:
                ct = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(TZ)
                if (now - ct).days > max_age_days:
                    continue
            except ValueError:
                continue
            title = (t.get("title") or "").strip()
            if not title:
                continue
            items.append({
                "source": "shuiyuan",
                "title": title,
                "url": "%s/t/topic/%d" % (HOST, t.get("id", 0)),
                "category": cname,
                "summary": "",
                "replies": max(0, t.get("posts_count", 1) - 1),
                "like_count": t.get("like_count", 0),
                "created_at": created,
            })
            picked += 1
        time.sleep(1)

    if with_first_post:
        for item in items[:16]:
            tid = item["url"].rsplit("/", 1)[-1]
            for attempt in range(2):
                try:
                    d = _api("/t/%s.json" % tid, timeout=30)
                    posts = d.get("post_stream", {}).get("posts") or []
                    if posts:
                        item["summary"] = _strip_html(posts[0].get("cooked", ""))[:1200]
                    break
                except Exception:  # noqa: BLE001
                    time.sleep(3)
            time.sleep(1.5)

    items.sort(key=lambda x: (x.get("like_count", 0) * 2 + x.get("replies", 0)), reverse=True)
    return items
