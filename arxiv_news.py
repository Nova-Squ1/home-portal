#!/usr/bin/env python3
"""arxiv_news.py — arXiv cs.AI 论文抓取模块。

数据源：arXiv Atom API https://export.arxiv.org/api/query（无需认证）。
按关键词过滤（LLM/Agent/推理/RAG/多模态等），取最近 2 天提交。
summary = 论文 abstract（本来就是首楼性质，直接喂省流）。
"""

import re
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))
ATOM = "http://www.w3.org/2005/Atom"
API = "https://export.arxiv.org/api/query"

KEYWORDS = [
    r"\bLLM\b", r"\blarge language model", r"language model",
    r"\bAgent\b", r"\bAgents\b", r"agentic", r"tool[- ]use", r"tool calling",
    r"reasoning", r"\bchain[- ]of[- ]thought", r"\bCoT\b", r"\bRAG\b",
    r"retrieval[- ]augmented", r"multimodal", r"\bVLA\b",
    r"fine[- ]tuning", r"\bRLHF\b", r"reinforcement learning",
    r"\binference\b", r"\bbenchmark\b", r"\bGPT\b", r"\btransformer\b",
]
KEYWORD_RE = [re.compile(k, re.I) for k in KEYWORDS]

MAX_PAPERS = 8
FETCH_PAGE = 60  # 先取最近 60 篇再过滤


def fetch_papers(max_age_days=2):
    """最近 cs.AI 论文，关键词命中 + 2 天内，最多 MAX_PAPERS 篇。"""
    query = urllib.parse.urlencode({
        "search_query": "cat:cs.AI",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": str(FETCH_PAGE),
    })
    req = urllib.request.Request(API + "?" + query, headers={
        "User-Agent": "Mozilla/5.0 (compatible; NovaPortal/1.0; +https://home.mcsqu.com)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        root = ET.fromstring(resp.read(2_000_000))

    now = datetime.now(TZ)
    items = []
    for e in root.findall("{%s}entry" % ATOM):
        title = " ".join(((e.findtext("{%s}title" % ATOM) or "")).split())
        link = (e.findtext("{%s}id" % ATOM) or "").strip()
        abstract = " ".join(((e.findtext("{%s}summary" % ATOM) or "")).split())
        published = (e.findtext("{%s}published" % ATOM) or "").strip()
        authors = [a.findtext("{%s}name" % ATOM) or "" for a in e.findall("{%s}author" % ATOM)]
        if not title or not link:
            continue
        try:
            pt = datetime.fromisoformat(published.replace("Z", "+00:00")).astimezone(TZ)
            if (now - pt).days > max_age_days:
                continue
        except ValueError:
            continue
        blob = title + " " + abstract
        hits = sum(1 for rx in KEYWORD_RE if rx.search(blob))
        if hits < 1:
            continue
        items.append({
            "source": "arxiv",
            "title": title,
            "url": link,
            "category": "cs.AI",
            "summary": abstract[:1200],
            "authors": ", ".join(authors[:4]) + (" et al." if len(authors) > 4 else ""),
            "keyword_hits": hits,
            "published": published,
        })
    # 关键词命中数排序
    items.sort(key=lambda x: x.get("keyword_hits", 0), reverse=True)
    return items[:MAX_PAPERS]
