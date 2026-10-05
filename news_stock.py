#!/usr/bin/env python3
"""News 库存制（2026-10-03）。

- 库存 data/news_stock.json：{key: 帖子}，只收有省流的帖子，全站一份。
- 待省流 data/news_pending.json：{key: {"item", "tries", "ts"} 或 {"dead": ts}}。
  省流连续失败 MAX_TRIES 次标 dead：丢弃且以后不再收。
- 每人展示 data/users/<uid>/news_view.json：{"shown": [key], "seen": [key]}。
  不点刷新 shown 不变，只补空位（删除/收藏后流进新帖）；点刷新整批换，换下去的进 seen，
  库存轮完一遍才重新出现。
- arXiv 帖标题存成「中文 ｜ English」双语（title_zh 是已翻译标记），没翻译好的不上主页；前端卡片不显示 arXiv 的省流。
- 收录节奏不变：poller 每 30 分钟抓四源；屏蔽表（news_digest）与收藏（news_saved）逻辑不变。

用法：python3 news_stock.py groom   # 从历史归档重建库存（只留有省流的，过 Jev 重审）
     python3 news_stock.py check   # 自检
"""
import json
import os
import random
import sys
import threading
import time

import news_digest
import news_saved
import news_summary

STOCK_PATH = os.path.join(news_digest.DATA_DIR, "news_stock.json")
PENDING_PATH = os.path.join(news_digest.DATA_DIR, "news_pending.json")
SHOW = 40          # 每人主页展示条数
MAX_TRIES = 3      # 省流连续失败几次后丢弃
PENDING_TTL = 7 * 86400   # 一直抓不到正文的待省流帖保留多久
DEAD_MAX = 3000
FIELDS = ("source", "title", "url", "category", "summary", "replies", "authors", "like_count")
LOCK = threading.RLock()


def key(url):
    return str(url or "").split("#")[0].rstrip("/")


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _slim(it):
    out = {f: it[f] for f in FIELDS if it.get(f) is not None}
    out["summary"] = (out.get("summary") or "")[:3000]
    return out


def size():
    """库存数（展示用）：只数非 arxiv——arXiv 已停抓下线，旧论文帖不进推送（2026-10-05）。"""
    with LOCK:
        return sum(1 for it in _read(STOCK_PATH).values() if it.get("source") != "arxiv")


def collect(items):
    """把一轮抓取结果收进库存（已有省流）或待省流队列。返回 (进库存数, 进队列数)。"""
    blocked = news_digest.list_blocked()
    cache = news_digest._load_tldr_cache()
    with LOCK:
        known = set(_read(STOCK_PATH))
        pending0 = _read(PENDING_PATH)
    fresh = []
    for it in items:
        k = key(it.get("url"))
        if not k or k in known or k in blocked or (pending0.get(k) or {}).get("dead"):
            continue
        if not news_digest._not_baoyan(it):
            continue
        # linux.do / v2ex 在抓取层已过 Jev；水源、idcflare、nodeseek 在这里补审干货（Jev 不可用时放行）；arXiv 不审
        if it.get("source") in ("shuiyuan", "idcflare", "nodeseek") and k not in pending0 and news_digest.jev_verdict(
                it.get("source"), it.get("title"), it.get("category"), it.get("summary")) is False:
            continue
        fresh.append((k, it))
    added = queued = 0
    now = int(time.time())
    with LOCK:
        stock, pending = _read(STOCK_PATH), _read(PENDING_PATH)
        for k, it in fresh:
            if k in stock:
                continue
            hit = cache.get(k)
            if hit and hit.get("tldr"):
                stock[k] = dict(_slim(it), tldr=hit["tldr"], ts=now)
                pending.pop(k, None)
                added += 1
            else:
                old = pending.get(k) or {}
                pending[k] = {"item": _slim(it), "tries": old.get("tries", 0), "ts": old.get("ts", now)}
                queued += 0 if old else 1
        # 清理：过期的待省流、过多的 dead 记录
        for k in [k for k, p in pending.items() if not p.get("dead") and now - p.get("ts", now) > PENDING_TTL]:
            del pending[k]
        dead = sorted((p["dead"], k) for k, p in pending.items() if p.get("dead"))
        for _, k in dead[:-DEAD_MAX] if len(dead) > DEAD_MAX else []:
            del pending[k]
        _write(STOCK_PATH, stock)
        _write(PENDING_PATH, pending)
    return added, queued


def fill_tldr(limit=8, summarize=None):
    """给待省流的帖子生成省流：成功进库存；失败计次，满 MAX_TRIES 次丢弃。"""
    summarize = summarize or news_summary.summarize
    with LOCK:
        todo = [(k, p) for k, p in _read(PENDING_PATH).items()
                if not p.get("dead") and (p["item"].get("summary") or "").strip()][:limit]
    for k, p in todo:
        it = p["item"]
        try:
            tldr = summarize(it.get("title", ""), it.get("summary", ""), it.get("url", ""))
        except Exception:  # noqa: BLE001
            tldr = None
        with LOCK:
            stock, pending = _read(STOCK_PATH), _read(PENDING_PATH)
            if k not in pending or pending[k].get("dead"):
                continue  # 期间被屏蔽/处理掉了
            if tldr:
                stock[k] = dict(it, tldr=tldr, ts=int(time.time()))
                del pending[k]
                _write(STOCK_PATH, stock)
            elif pending[k].get("tries", 0) + 1 >= MAX_TRIES:
                pending[k] = {"dead": int(time.time())}
            else:
                pending[k]["tries"] = pending[k].get("tries", 0) + 1
            _write(PENDING_PATH, pending)


def fill_titles(limit=20, translate=None):
    """给库存里的 arXiv 帖补中文标题，一次一批。返回本次翻译条数。"""
    translate = translate or news_summary.translate_titles
    with LOCK:
        todo = [(k, v["title"]) for k, v in _read(STOCK_PATH).items()
                if v.get("source") == "arxiv" and not v.get("title_zh")][:limit]
    if not todo:
        return 0
    zh = translate([en for _, en in todo])
    if not zh:
        return 0
    with LOCK:
        stock = _read(STOCK_PATH)
        for (k, en), z in zip(todo, zh):
            if k in stock:
                stock[k].update(title_zh=z, title=z + " ｜ " + en)
        _write(STOCK_PATH, stock)
    return len(todo)


def drop(url):
    """屏蔽时从库存和队列里拿掉（屏蔽表本身由 news_digest.block_url 写）。"""
    k = key(url)
    with LOCK:
        for path in (STOCK_PATH, PENDING_PATH):
            d = _read(path)
            if d.pop(k, None) is not None:
                _write(path, d)


def view(t, refresh=False):
    """当前用户主页的帖子列表。refresh=True 时整批从库存随机换。"""
    path = t.data("news_view.json")
    with LOCK:
        stock = _read(STOCK_PATH)
        v = _read(path)
        skip = {i.get("key") for i in news_saved.list_saved(t)} | set(news_digest.list_blocked())
        # 2026-10-05 arXiv 停抓后：库存旧论文帖仍可展示，但优先级最低——
        # 只有非 arxiv 帖不够填满时才用 arxiv 补位（随机补位时排除，最后兜底）
        ok = [k for k, it in stock.items()
              if k not in skip and (it.get("source") != "arxiv" or it.get("title_zh"))]
        non_arxiv = [k for k in ok if stock[k].get("source") != "arxiv"]
        okset = set(ok)
        shown = [k for k in v.get("shown", []) if k in okset]
        seen = set(v.get("seen", [])) & okset
        if refresh:
            seen |= set(shown)
            shown = []
        need = SHOW - len(shown)
        if need > 0:
            pool = [k for k in non_arxiv if k not in seen and k not in shown]
            pick = random.sample(pool, min(need, len(pool)))
            if len(pick) < need:  # 非arxiv轮完一遍：清空已看从头再来；宁可空位也不拿 arxiv 填（2026-10-05 用户要求下论文）
                seen = set()
                pool2 = [k for k in non_arxiv if k not in shown and k not in pick]
                pick += random.sample(pool2, min(need - len(pick), len(pool2)))
            shown += pick
        new = {"shown": shown, "seen": sorted(seen)}
        if new != v:
            _write(path, new)
        return [stock[k] for k in shown]


def sync_once(fast_only=False, only_nodeseek=False):
    return collect(news_digest.build_digest(fast_only=fast_only, only_nodeseek=only_nodeseek).get("items", []))


def poller():
    time.sleep(8)  # 让门户先起来
    tick = 0
    while True:
        try:
            # 轮转节奏（2026-10-05）：tick%5==2/4 只抓 NodeSeek（3 分钟一轮，高频源）；
            # tick%2==1 抓快源（水源/idcflare/V2EX，15 分钟）；tick 偶数全量（30 分钟含 linux.do）
            only_ns = tick % 5 in (2, 4)
            fast_only = (tick % 2 == 1) and not only_ns
            sync_once(fast_only=fast_only, only_nodeseek=only_ns)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(news_digest.POLL_SECONDS // 10)  # 3 分钟一拍
        tick += 1


def tldr_poller():
    time.sleep(20)
    while True:
        for step in (fill_tldr, fill_titles):
            try:
                step()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(120)


def groom():
    """一次性梳理：历史归档里每帖取最新记录，只留有省流、未屏蔽、非保研、过 Jev 重审的进库存。"""
    latest = {}
    with open(news_digest.HISTORY_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("url"):
                latest[key(rec["url"])] = rec
    cache = news_digest._load_tldr_cache()
    blocked = news_digest.list_blocked()
    stock = _read(STOCK_PATH)
    stat = {"total": len(latest), "no_tldr": 0, "blocked": 0, "baoyan": 0, "jev_reject": 0, "jev_down": 0, "kept": 0}
    for k, rec in latest.items():
        tldr = rec.get("tldr") or (cache.get(k) or {}).get("tldr")
        if not tldr:
            stat["no_tldr"] += 1
        elif k in blocked:
            stat["blocked"] += 1
        elif not news_digest._not_baoyan(rec):
            stat["baoyan"] += 1
        else:
            verdict = None if rec.get("source") == "arxiv" else news_digest.jev_verdict(
                rec.get("source"), rec.get("title"), rec.get("category"), rec.get("summary"))
            if verdict is False:
                stat["jev_reject"] += 1
                print("拒:", rec.get("source"), rec.get("title"))
                continue
            if verdict is None and rec.get("source") != "arxiv":
                stat["jev_down"] += 1  # Jev 没答上来：保留
            stock[k] = dict(_slim(rec), tldr=tldr, ts=rec.get("ts") or int(time.time()))
            stat["kept"] += 1
    with LOCK:
        _write(STOCK_PATH, stock)
    print(stat)


def check():
    """自检：收录、省流重试三次丢弃、展示补位与轮换。"""
    import tempfile
    global STOCK_PATH, PENDING_PATH
    tmp = tempfile.mkdtemp()
    STOCK_PATH, PENDING_PATH = os.path.join(tmp, "s.json"), os.path.join(tmp, "p.json")
    saved, blocked = [], {}
    news_saved.list_saved = lambda t: saved
    news_digest.list_blocked = lambda: blocked
    news_digest._load_tldr_cache = lambda: {}
    t = type("T", (), {"data": staticmethod(lambda name: os.path.join(tmp, name))})()
    items = [{"source": "v2ex", "title": "t%d" % i, "url": "https://x/%d" % i, "summary": "body"} for i in range(100)]
    assert collect(items) == (0, 100)
    fill_tldr(limit=99, summarize=lambda *a: None if a[2].endswith("/99") else "tl")
    assert size() == 99
    for _ in range(MAX_TRIES):
        fill_tldr(summarize=lambda *a: None)
    assert _read(PENDING_PATH) == {"https://x/99": _read(PENDING_PATH)["https://x/99"]} and "dead" in _read(PENDING_PATH)["https://x/99"]
    assert collect(items) == (0, 0), "dead 与已入库的不再收"
    a = [i["url"] for i in view(t)]
    assert len(a) == SHOW and a == [i["url"] for i in view(t)], "不刷新不变"
    saved.append({"key": a[0]}); blocked[a[1]] = {}
    b = [i["url"] for i in view(t)]
    assert b[:SHOW - 2] == a[2:] and len(b) == SHOW and not {a[0], a[1]} & set(b), "收藏/删除后原位保留并补位"
    c = [i["url"] for i in view(t, refresh=True)]
    assert not set(c) & set(b), "刷新换一批，看过的不重复"
    d = [i["url"] for i in view(t, refresh=True)]
    assert len(d) == SHOW and len(set(d) - set(b) - set(c)) == 97 - 2 * SHOW, "库存不够时先出没看过的，再从头轮"
    _write(STOCK_PATH, dict(_read(STOCK_PATH), **{"https://a/1": {"source": "arxiv", "title": "Foo", "url": "https://a/1", "tldr": "x"}}))
    assert "https://a/1" not in {i["url"] for _ in range(4) for i in view(t, refresh=True)}, "没翻译的 arXiv 不上主页"
    assert fill_titles(translate=lambda ts: ["富"]) == 1 and _read(STOCK_PATH)["https://a/1"]["title"] == "富 ｜ Foo"
    print("ok")


if __name__ == "__main__":
    {"groom": groom, "check": check}[sys.argv[1] if len(sys.argv) > 1 else "check"]()
