#!/usr/bin/env python3
"""hnsw_digest.py — Hacker News Best 与 Simon Willison 博客抓取（2026-10-05 接入 News 子页）。

- HN Best: https://hnrss.org/best （RSS，30 条；desc 里带 Article URL/Comments URL，
  link 指向原文，comments 指向 HN 讨论帖）
- Simon Willison: https://simonwillison.net/atom/everything/ （Atom，30 条；
  summary 是 HTML 正文，取 1200 字做省流输入）

两个源都走 curl 抓取（simonwillison.net 偶尔对 Python UA 挑战）。
过滤：is_technical（黑名单正则 + Jev，与其他源同口径）；HN 是社区投票的精华帖
默认有干货，Jev 只拒明显跑题的（生活/娱乐）；SW 是个人技术博客基本全收。
发布节奏：随 news poller 快轮（15 分钟）。
"""
import re
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET

import news_digest
from html import unescape

UA = "Mozilla/5.0 (compatible; NovaPortal/1.0; +https://home.mcsqu.com)"  # 与 news_digest.UA 相同；别在模块级引用它（循环导入）
ATOM = "{http://www.w3.org/2005/Atom}"


def _fetch(url, timeout=25):
    proc = subprocess.run(
        ["curl", "-sSL", "--max-time", str(timeout), "-A", UA, url],
        capture_output=True, timeout=timeout + 10)
    if proc.returncode or not proc.stdout[:100].strip():
        raise RuntimeError("curl fetch failed: rc=%d" % proc.returncode)
    return proc.stdout[:3_000_000]


def _clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return unescape(text).strip()


def fetch_hn_best():
    """HN Best（社区投票精华）。link=原文；URL 用 HN 讨论帖（信息量更大且稳定），
    原文链接放 summary 首行。"""
    root = ET.fromstring(_fetch("https://hnrss.org/best"))
    items = []
    for it in root.findall("./channel/item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        comments = (it.findtext("comments") or "").strip()
        desc = _clean(it.findtext("description") or "")[:1200]
        pub = it.findtext("pubDate") or ""
        if not title or not (link or comments):
            continue
        url = comments or link
        if not news_digest.is_technical("hn", title, "HackerNews", desc, url=url):
            continue
        items.append({
            "source": "hn",
            "title": unescape(title),
            "url": url,
            "category": "HackerNews",
            "summary": ("原文: " + link + "\n" + desc) if link and link != url else desc,
            "published": pub,
        })
    return items


def fetch_simonw():
    """Simon Willison 博客（LLM 应用/工具/安全）。个人技术博客，几乎全是干货，
    只过黑名单正则不走 Jev（省调用且他不会发保研帖）。"""
    root = ET.fromstring(_fetch("https://simonwillison.net/atom/everything/"))
    items = []
    for e in root.findall(ATOM + "entry"):
        title = (e.findtext(ATOM + "title") or "").strip()
        link_el = e.find(ATOM + "link")
        link = (link_el.get("href") if link_el is not None else "") or ""
        summary = _clean(e.findtext(ATOM + "summary") or "")[:1200]
        pub = e.findtext(ATOM + "published") or ""
        if not title or not link:
            continue
        # 只保留带正文的条目（link-outs / 短记录也有 summary，均可）
        blob = title + " " + summary
        if any(rx.search(blob) for rx in news_digest.EXCLUDE_RE):
            continue
        items.append({
            "source": "simonw",
            "title": unescape(title),
            "url": link.split("#")[0],
            "category": "Simon Willison",
            "summary": summary,
            "published": pub,
        })
    return items
