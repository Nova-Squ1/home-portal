#!/usr/bin/env python3
"""news_saved.py — News 帖子收藏：永久保存快照，不随每日刷新消失。

数据 data/users/<uid>/news_saved.json（600）：
[{key, url, title, source, category, replies, like_count, authors,
  summary, tldr, saved_at}]
key = URL 去 fragment 去尾斜杠（同 news_digest 去重键）。
仅标准库；锁内读写。
"""
import json
import os
import threading
import time

LOCK = threading.Lock()


def _path(t):
    return t.data("news_saved.json")


def _key(url):
    return str(url or "").split("#")[0].rstrip("/")


def _read(t):
    try:
        with open(_path(t), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (OSError, ValueError):
        pass
    return []


def _write(t, items):
    t.ensure_dirs()
    p = _path(t)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
        f.write("\n")
        os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def list_saved(t):
    with LOCK:
        return _read(t)


def is_saved(t, url):
    k = _key(url)
    with LOCK:
        return any(i.get("key") == k for i in _read(t))


def save(t, item):
    """收藏一条帖子（item 为 /api/news 的条目）。已收藏则更新快照，返回完整列表。"""
    k = _key(item.get("url"))
    if not k:
        raise ValueError("缺少 url")
    snap = {
        "key": k,
        "url": item.get("url", ""),
        "title": item.get("title", ""),
        "source": item.get("source", ""),
        "category": item.get("category"),
        "replies": item.get("replies"),
        "like_count": item.get("like_count"),
        "authors": item.get("authors"),
        "summary": (item.get("summary") or "")[:4000],
        "tldr": item.get("tldr"),
        "saved_at": int(time.time()),
    }
    with LOCK:
        items = [i for i in _read(t) if i.get("key") != k]
        items.insert(0, snap)
        _write(t, items)
    return snap


def unsave(t, url):
    k = _key(url)
    with LOCK:
        items = _read(t)
        rest = [i for i in items if i.get("key") != k]
        _write(t, rest)
        return len(items) != len(rest)
