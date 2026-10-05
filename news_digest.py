#!/usr/bin/env python3
"""news_digest.py — 抓取 linux.do 日热门与 V2EX 热门话题，生成当日新闻摘要。

数据源：
  - https://linux.do/top.rss?period=daily （Discourse RSS，标题/链接/分类）
  - https://www.v2ex.com/api/topics/hot.json （官方 API，含回复数）

build_digest() 返回本轮四源合并去重后的条目；收进库存、省流、展示都在 news_stock.py（2026-10-03 库存制）。
仅标准库。
"""
import json
import os
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET

import arxiv_news  # 2026-10-05 起 arXiv 停抓；保留导入供 groom/历史工具用
import hnsw_digest
import jev_client
import shuiyuan
from datetime import datetime, timezone, timedelta
from html import unescape

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

TZ = timezone(timedelta(hours=8))
POLL_SECONDS = 1800  # 30 分钟（linux.do 有 Cloudflare 限流，不要更高频）
MAX_ITEMS = 40

UA = "Mozilla/5.0 (compatible; NovaPortal/1.0; +https://home.mcsqu.com)"

LOCK = threading.Lock()
BLOCKED_PATH = os.path.join(DATA_DIR, "news_blocked.json")
BLOCKED_LOCK = threading.Lock()


def list_blocked():
    try:
        with open(BLOCKED_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_blocked(d):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = BLOCKED_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
        f.write("\n")
        os.chmod(tmp, 0o600)
    os.replace(tmp, BLOCKED_PATH)


def block_url(url, title=""):
    with BLOCKED_LOCK:
        d = list_blocked()
        k = str(url or "").split("#")[0].rstrip("/")
        if not k:
            raise ValueError("缺少 url")
        d[k] = {"title": (title or "")[:200], "ts": int(time.time())}
        _write_blocked(d)
    return d


def unblock_url(url):
    with BLOCKED_LOCK:
        d = list_blocked()
        k = str(url or "").split("#")[0].rstrip("/")
        removed = d.pop(k, None) is not None
        if removed:
            _write_blocked(d)
    return removed

# 只收录技术硬核 / 前沿 AI 类内容；标题/分类/摘要任一命中即保留
INCLUDE_PATTERNS = [
    # 中文关键词
    r"前沿快讯", r"开发调优", r"国产替代", r"人工智能", r"大模型", r"开源", r"算法",
    r"大厂.{0,6}(开源|发布|推出)", r"(AI|LLM|GPT|GLM|Claude|Gemini|Qwen|DeepSeek|Kimi)",
    r"(Copilot|AI Agent|智能体|推理模型|多模态|RAG|微调|模型训练|训练.{0,4}模型|Infra|基础设施)",
    r"(Rust|Go\b|C\+\+|Python|TypeScript|前端|后端|编译器|数据库|内核|分布式|云原生|K8s?|Docker|Linux)",
    r"(CUDA|GPU|算力|芯片|半导体|显卡)", r"(安全|漏洞|CVE|逆向|渗透)",
    r"(论文|Paper|arXiv|基准|Benchmark|SOTA)",
]
# 明确排除的水化分类/关键词
EXCLUDE_PATTERNS = [
    r"搞七捻三", r"前沿快讯", r"纯水", r"水贴", r"抽奖", r"签到", r"吐槽.{0,4}(生活|日常)",
    r"(抽奖|转运|许愿|星座|情感|相亲|爆料|吃瓜)",
    # 广告/蹭额度类：公益站、中转站、体验卡、白嫖一律不要（用户 2026-09-20 明确要求）
    r"公益站", r"公益", r"中转站", r"体验卡", r"白嫖", r"羊毛", r"福利",
    r"(免费.{0,6}(API|额度|模型|生图))", r"(不限次数|0LDC|送.{0,4}(额度|会员|席位))",
]
INCLUDE_RE = [re.compile(pat, re.I) for pat in INCLUDE_PATTERNS]
EXCLUDE_RE = [re.compile(pat, re.I) for pat in EXCLUDE_PATTERNS]

# V2EX 技术节点白名单（node name）
V2EX_TECH_NODES = {
    "python", "programmer", "linux", "nodejs", "golang", "rust", "ai", "openai",
    "llm", "cloud", "docker", "kubernetes", "database", "postgresql", "mysql",
    "redis", "mongodb", "backend", "frontend", "react", "vue", "typescript",
    "javascript", "java", "cpp", "c", "swift", "kotlin", "flutter", "devops",
    "security", "crypto", "machinelearning", "deeplearning", "nlp", "cv",
    "gpu", "cuda", "algorithm", "compiler", "emacs", "vim", "macos", "ipv6",
    "selfhosted", "open source", "git", "github", "ssh", "nginx", "caddy",
}
# V2EX 偏生活的节点直接排除
V2EX_SKIP_NODES = {"life", "jobs", "play", "buy", "sell", "apple", "create", "share", "qna", "hot", "all"}


_JEV_TECH_CACHE = {}
_JEV_CACHE_MAX = 5000


JEV_TECH_MIN = 0.7       # 2026-10-03 加严：原 0.5
JEV_SUBSTANCE_MIN = 0.5


def jev_verdict(source, title, category, summary, node=None):
    """Jev 审查：True 放行 / False 拒 / None 表示 Jev 不可用。结果按 (source, title) 缓存。
    三问：硬核技术（只对 linux.do、v2ex 要求——水源收的是经验帖）、有没有实质干货（2026-10-03 加）、是不是保研帖。"""
    key = (source, (title or "")[:120], len(summary or "") // 200)  # desc 长度分桶：RSS 首轮摘要短、次轮更新后变长，不应沿用旧判决
    hit = _JEV_TECH_CACHE.get(key)
    if hit is not None:
        return hit
    state = (
        "来源: " + str(source) + ("\n板块/节点: " + str(node) if node else "") +
        "\n分类: " + str(category or "无") +
        "\n标题: " + str(title or "") +
        "\n摘要: " + str(summary or "")[:600]
    )
    r = jev_client.ask(state, {
        "is_tech": {
            "type": "noul",
            "instructions": "这是硬核技术或前沿 AI 内容吗？true=编程/系统/AI 模型/硬件芯片/安全/学术论文等有技术营养的帖子；false=生活闲聊、情感、娱乐、时事吃瓜、纯广告推广、蹭额度羊毛帖",
            "criteria": {"true": "有技术营养，值得出现在技术人首页", "false": "水帖/生活/广告/羊毛，技术人不想看"},
        },
        "has_substance": {
            "type": "noul",
            "instructions": "这个帖子本身有实质干货吗？true=作者给出了具体的经验、方案、教程、数据、分析或成果，读完能学到东西；false=求助提问帖、一两句话的提问或感慨、纯转发链接没有自己的内容、投票闲聊、求推荐、吐槽",
            "criteria": {"true": "正文自带可学的具体内容", "false": "求助/提问/纯转发/闲聊，正文没有干货"},
        },
        "is_baoyan": {
            "type": "noul",
            "instructions": "这个帖子的主题是围绕「保研」（本科推免：保研排名/夏令营/预推免/保研去向选择/保研经验）展开的吗？true=通篇在讨论保研流程与选择；false=不涉及保研，或只是顺带提及——研究生/博士生的申请与读研心得、考研、本科就业求职技巧、实习招聘经验都算 false",
            "criteria": {"true": "保研主题帖（推免/夏令营/保研选择）", "false": "非保研主题：读研心得/考博/就业/实习/技术帖，或仅顺带提到保研"},
        },
    })
    if r is None:
        return None

    def noul(q):
        v = (r["answers"].get(q) or {}).get("noul")
        return v if isinstance(v, (int, float)) else None

    tech, sub, baoyan = noul("is_tech"), noul("has_substance"), noul("is_baoyan")
    verdict = not (
        (baoyan is not None and baoyan >= 0.75)  # 保研主题帖直接拒（用户 2026-09-27 要求）
        or (sub is not None and sub < JEV_SUBSTANCE_MIN)
        or (source in ("linux.do", "v2ex") and (tech is None or tech < JEV_TECH_MIN))
    )
    # nodeseek/idcflare（主机圈，2026-10-05 接入）：不要求"硬核技术"，只要求干货——
    # 测评、线路观察、商家动态对主机圈读者就是内容；收售拼车贴由干货门槛拦
    if source in ("nodeseek", "idcflare"):
        verdict = not (
            (baoyan is not None and baoyan >= 0.75)
            or (sub is not None and sub < 0.6)
        )
    if len(_JEV_TECH_CACHE) > _JEV_CACHE_MAX:
        _JEV_TECH_CACHE.clear()
    _JEV_TECH_CACHE[key] = verdict
    return verdict


def _jev_is_technical(source, title, category, summary, node=None):
    """准入：硬性黑名单（推广分类、水化、广告蹭额度）先行，再交 Jev；Jev 不可用回退正则白名单。"""
    blob = " ".join(filter(None, [title, category, summary]))
    if "推广" in (category or "") or "营销" in (category or ""):
        return False
    if any(rx.search(blob) for rx in EXCLUDE_RE):
        return False
    verdict = jev_verdict(source, title, category, summary, node)
    if verdict is None:
        return _regex_is_technical(source, title, category, summary, node)
    return verdict


def _regex_is_technical(source, title, category, summary, node=None):
    """原正则白名单逻辑（Jev 回退用）。"""
    blob = " ".join(filter(None, [title, category, summary]))
    if source == "v2ex":
        if node and node.lower() in V2EX_TECH_NODES:
            return True
        return any(rx.search(blob) for rx in INCLUDE_RE)
    if category in ("开发调优", "国产替代", "资源荟萃", "文档共建"):
        return True
    return any(rx.search(blob) for rx in INCLUDE_RE)


def is_technical(source, title, category, summary, node=None, url=None):
    """准入过滤：屏蔽帖直接拒（不浪费 Jev 调用）→ 黑名单正则 → Jev 主判 → 正则白名单回退。"""
    if url:
        k = str(url).split("#")[0].rstrip("/")
        if k and k in list_blocked():
            return False
    return _jev_is_technical(source, title, category, summary, node)


def _fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(3_000_000)


def _fetch_curl(url, timeout=25):
    """linux.do 的 Cloudflare 会挑战 Python TLS 指纹；curl（HTTP/2）可过。"""
    import subprocess
    proc = subprocess.run(
        ["curl", "-sSL", "--max-time", str(timeout), "-A", UA, url],
        capture_output=True, timeout=timeout + 10)
    if proc.returncode or not proc.stdout.startswith(b"<?xml") and not proc.stdout.startswith(b"["):
        raise RuntimeError("curl fetch failed: rc=%d" % proc.returncode)
    return proc.stdout[:3_000_000]


def _clean_html(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    return unescape(text).strip()


def fetch_linuxdo():
    """linux.do 日热门 RSS。条目含 discourse 分类，可初筛质量。"""
    data = _fetch_curl("https://linux.do/top.rss?period=daily")
    root = ET.fromstring(data)
    items = []
    for it in root.findall("./channel/item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        category = (it.findtext("category") or "").strip()
        desc = _clean_html(it.findtext("description") or "")[:1200]
        pub = it.findtext("pubDate") or ""
        if not title or not link:
            continue
        if not is_technical("linux.do", unescape(title), category, desc, url=link):
            continue
        items.append({
            "source": "linux.do",
            "title": unescape(title),
            "url": link,
            "category": category,
            "summary": desc,
            "published": pub,
        })
    return items


def fetch_v2ex():
    """V2EX 热门话题 API（官方），带回复数，按回复数排序本身就是精品信号。"""
    data = json.loads(_fetch("https://www.v2ex.com/api/topics/hot.json"))
    items = []
    for it in data:
        title = (it.get("title") or "").strip()
        url = (it.get("url") or "").strip()
        if not title or not url:
            continue
        node = (it.get("node") or {})
        node_title = node.get("title", "")
        node_name = node.get("name", "")
        replies = it.get("replies", 0)
        content = _clean_html(it.get("content") or "")[:1200]
        if not is_technical("v2ex", title, node_title, content, node=node_name, url=url):
            continue
        items.append({
            "source": "v2ex",
            "title": title,
            "url": url,
            "category": node_title,
            "summary": content,
            "replies": replies,
            "published": datetime.fromtimestamp(it.get("created", 0), TZ).strftime("%a, %d %b %Y %H:%M:%S +0800"),
        })
    return items


def fetch_idcflare():
    """idcflare.com（Discourse 主机论坛）最新话题 RSS。
    2026-10-05 接入：latest.rss 可直连（top.rss/分类 RSS 有 Cloudflare 挑战，勿用）。
    分类：交易/求助/测评/福利/茶馆——主机/VPS 圈，按内容关键词 + Jev 审查过滤。
    """
    data = _fetch_curl("https://idcflare.com/latest.rss")
    root = ET.fromstring(data)
    items = []
    for it in root.findall("./channel/item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        category = (it.findtext("category") or "").strip()
        desc = _clean_html(it.findtext("description") or "")[:1200]
        pub = it.findtext("pubDate") or ""
        if not title or not link:
            continue
        if not is_technical("idcflare", unescape(title), category, desc, url=link):
            continue
        items.append({
            "source": "idcflare",
            "title": unescape(title),
            "url": link,
            "category": category,
            "summary": desc,
            "published": pub,
        })
    return items


def fetch_nodeseek():
    """NodeSeek（nodeseek.com）主机论坛官方 RSS：https://rss.nodeseek.com/
    2026-10-05 接入：主站有 CF 挑战，但 rss 子域可直连（curl）。20 条最新帖，
    category: daily(水)/trade(交易)/review(测评)。description 是正文摘要（短）。
    预筛已于同日晚移除（用户反馈 Jev 额度花不完，统一走 is_technical→Jev）。
    """
    data = _fetch_curl("https://rss.nodeseek.com/")
    root = ET.fromstring(data)
    items = []
    for it in root.findall("./channel/item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        category = (it.findtext("category") or "").strip()
        desc = _clean_html(it.findtext("description") or "")[:1200]
        pub = it.findtext("pubDate") or ""
        if not title or not link:
            continue
        if not is_technical("nodeseek", unescape(title), category, desc, url=link):
            continue
        items.append({
            "source": "nodeseek",
            "title": unescape(title),
            "url": link,
            "category": category,
            "summary": desc,
            "published": pub,
        })
    return items


def build_digest(fast_only=False, only_nodeseek=False):
    """合并各源，URL 归一化去重，生成当日摘要。arXiv 2026-10-05 起停抓（用户要求）。

    fast_only=True 时跳过 linux.do（CF 限流源），供 15 分钟快轮用；
    only_nodeseek=True 时只抓 NodeSeek（高频源，3 分钟快拍）。
    """
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    all_items = []
    errors = []
    if only_nodeseek:
        try:
            all_items.extend(fetch_nodeseek())
        except Exception as exc:  # noqa: BLE001
            errors.append(f"nodeseek: {type(exc).__name__}")
        return _merge_dedupe(all_items, today, now, errors)
    sources = (("linux.do", fetch_linuxdo), ("v2ex", fetch_v2ex), ("idcflare", fetch_idcflare), ("nodeseek", fetch_nodeseek))
    for name, fn in sources:
        if fast_only and name == "linux.do":
            continue
        try:
            all_items.extend(fn())
        except Exception as exc:  # 单源失败不影响另一源
            errors.append(f"{name}: {type(exc).__name__}")
    try:
        all_items.extend(shuiyuan.fetch_latest())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"shuiyuan: {type(exc).__name__}")
    for name, fn in (("hn", hnsw_digest.fetch_hn_best), ("simonw", hnsw_digest.fetch_simonw)):
        try:
            all_items.extend(fn())
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__}")
    return _merge_dedupe(all_items, today, now, errors)


def _merge_dedupe(all_items, today, now, errors):
    seen = set()
    blocked = list_blocked()
    items = []
    for it in all_items:
        url = it["url"].split("#")[0].rstrip("/")
        if url in seen or url in blocked:
            continue
        seen.add(url)
        items.append(it)
    # 各源轮转交错（各自保持热度顺序），展示更均衡
    queues = [
        [i for i in items if i["source"] == "linux.do"],
        [i for i in items if i["source"] == "shuiyuan"],
        [i for i in items if i["source"] == "idcflare"],
        [i for i in items if i["source"] == "nodeseek"],
        [i for i in items if i["source"] == "v2ex"],
        [i for i in items if i["source"] == "hn"],
        [i for i in items if i["source"] == "simonw"],
    ]
    merged = []
    idx = 0
    while len(merged) < MAX_ITEMS and any(queues):
        q = queues[idx % len(queues)]
        if q:
            merged.append(q.pop(0))
        idx += 1
    items = merged
    payload = {
        "date": today,
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(items),
        "errors": errors,
        "items": items[:MAX_ITEMS],
    }
    return payload


HISTORY_PATH = os.path.join(DATA_DIR, "news_history.jsonl")  # 2026-10-03 起停写，只供 news_stock.groom 读


# 保研主题粗筛（模块级：抓取层 Jev 语义判定 + 补位/缓存路径的标题正则兜底，用户 2026-09-27 要求屏蔽保研帖）
import re as _re_mod
_BAOYAN_RX = _re_mod.compile(r"保研|推免|夏令营|预推免")


def _not_baoyan(it_or_rec):
    txt = (it_or_rec.get("title") or "") + " " + (it_or_rec.get("summary") or "")[:200]
    return not _BAOYAN_RX.search(txt)


def _load_tldr_cache():
    """省流缓存 {url: {tldr, ts}}（这些帖子已有省流）。"""
    try:
        cache_path = os.path.join(DATA_DIR, "news_summary_cache.json")
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}
