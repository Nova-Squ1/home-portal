"""收藏夹：只存链接，不做归档/分类。仅使用标准库。

数据 data/bookmarks.json（600）：[{id, url, title, description, domain, icon, note, pinned,
created_at, updated_at}]。

添加时服务器抓取网页的标题、描述和图标（尽力而为，失败也照样保存）。
为防 SSRF，只抓 http/https、默认端口，且目标及每次跳转解析出的地址都必须是公网 IP。
"""

import html.parser
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

FETCH_TIMEOUT = 8
FETCH_MAX_BYTES = 512 * 1024
MAX_REDIRECTS = 4
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0 Safari/537.36 NovaBookmarks/1.0")

LOCK = threading.Lock()


class BookmarkError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def write_private_json(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        os.chmod(tmp, 0o600)
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def clean_text(value, limit):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def normalize_url(value):
    url = str(value or "").strip()
    if not url:
        raise BookmarkError("请填写链接")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise BookmarkError("只支持 http / https 链接")
    try:
        parts.port  # 端口不是数字时这里抛 ValueError
    except ValueError as exc:
        raise BookmarkError("链接格式不对") from exc
    if len(url) > 2000:
        raise BookmarkError("链接太长")
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/",
                                    parts.query, parts.fragment))


def domain_of(url):
    host = urllib.parse.urlsplit(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------- 抓取元数据

def assert_public(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.port not in (None, 80, 443):
        raise BookmarkError("不抓取非标准端口或非 http(s) 地址")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise BookmarkError("域名解析失败") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise BookmarkError("不抓取内网或本机地址")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(NoRedirect)


def fetch_page(url):
    """返回 (最终 url, html 文本)；每次跳转前都检查目标地址。"""
    for _ in range(MAX_REDIRECTS + 1):
        assert_public(url)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "text/html,application/xhtml+xml",
                                                   "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
        try:
            resp = _opener.open(req, timeout=FETCH_TIMEOUT)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                url = urllib.parse.urljoin(url, exc.headers["Location"])
                continue
            raise
        with resp:
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype.lower():
                return url, ""
            raw = resp.read(FETCH_MAX_BYTES)
            return url, decode_html(raw, resp.headers.get_content_charset())
    raise BookmarkError("跳转次数过多")


def decode_html(raw, header_charset):
    charset = header_charset
    if not charset:
        m = re.search(rb'<meta[^>]+charset=["\']?([A-Za-z0-9_-]+)', raw[:4096], re.I)
        charset = m.group(1).decode("ascii") if m else "utf-8"
    if charset.lower() in ("gb2312", "gbk"):
        charset = "gb18030"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


class MetaParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.icons, self.title, self._in_title = {}, [], "", False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"]
        elif tag == "link" and "icon" in a.get("rel", "").lower() and a.get("href"):
            self.icons.append((a.get("rel", "").lower(), a.get("sizes", ""), a["href"]))

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and len(self.title) < 500:
            self.title += data


def pick_icon(base, icons):
    """优先 apple-touch-icon（清晰），其次普通 icon，最后 /favicon.ico。"""
    def score(item):
        rel, sizes, _ = item
        m = re.search(r"(\d+)", sizes)
        return (1 if "apple-touch" in rel else 0, int(m.group(1)) if m else 0)
    for _, _, href in sorted(icons, key=score, reverse=True):
        url = urllib.parse.urljoin(base, href.strip())
        if url.startswith(("https://", "http://")):
            return url[:1000]
    parts = urllib.parse.urlsplit(base)
    return "%s://%s/favicon.ico" % (parts.scheme, parts.netloc)


def fetch_metadata(url):
    """尽力抓取；任何失败都返回空字段而不是报错。"""
    info = {"title": "", "description": "", "icon": ""}
    try:
        final_url, text = fetch_page(url)
    except Exception as exc:  # noqa: BLE001
        print("bookmark fetch error:", url, exc, flush=True)
        parts = urllib.parse.urlsplit(url)
        info["icon"] = "%s://%s/favicon.ico" % (parts.scheme, parts.netloc)
        return info
    parser = MetaParser()
    try:
        parser.feed(text)
    except Exception:  # noqa: BLE001  # 坏 HTML 不影响已解析出的部分
        pass
    meta = parser.meta
    info["title"] = clean_text(meta.get("og:title") or meta.get("twitter:title") or parser.title, 300)
    info["description"] = clean_text(meta.get("og:description") or meta.get("description")
                                     or meta.get("twitter:description"), 500)
    info["icon"] = pick_icon(final_url, parser.icons)
    return info


# ---------------------------------------------------------------- 存储

class Store:
    def __init__(self, data_path):
        self.data_path = data_path

    def load(self):
        try:
            with open(self.data_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def listing(self):
        with LOCK:
            items = self.load()
        items.sort(key=lambda b: b.get("created_at", ""), reverse=True)
        return {"bookmarks": items}

    def create(self, body):
        url = normalize_url(body.get("url"))
        with LOCK:
            existing = next((b for b in self.load() if b["url"] == url), None)
        if existing:
            return existing, False
        meta = fetch_metadata(url)  # 网络请求放在锁外
        stamp = now_iso()
        item = {
            "id": secrets.token_hex(8), "url": url,
            "title": clean_text(body.get("title"), 300) or meta["title"] or domain_of(url),
            "description": meta["description"], "domain": domain_of(url), "icon": meta["icon"],
            "note": clean_text(body.get("note"), 500), "pinned": False,
            "created_at": stamp, "updated_at": stamp,
        }
        with LOCK:
            items = self.load()
            existing = next((b for b in items if b["url"] == url), None)
            if existing:  # 抓取期间别处已加过
                return existing, False
            items.append(item)
            write_private_json(self.data_path, items)
        return item, True

    def update(self, item_id, body):
        changes = {}
        if "title" in body:
            changes["title"] = clean_text(body["title"], 300)
            if not changes["title"]:
                raise BookmarkError("标题不能为空")
        if "url" in body:
            changes["url"] = normalize_url(body["url"])
            changes["domain"] = domain_of(changes["url"])
        if "note" in body:
            changes["note"] = clean_text(body["note"], 500)
        if "pinned" in body:
            changes["pinned"] = bool(body["pinned"])
        if not changes:
            raise BookmarkError("没有可更新的字段")
        with LOCK:
            items = self.load()
            item = self._find(items, item_id)
            if "url" in changes and any(b["url"] == changes["url"] and b["id"] != item_id for b in items):
                raise BookmarkError("这个链接已经收藏过了", 409)
            item.update(changes)
            item["updated_at"] = now_iso()
            write_private_json(self.data_path, items)
        return item

    def refresh(self, item_id):
        """重新抓取标题/描述/图标；标题只在抓到时覆盖。"""
        with LOCK:
            url = self._find(self.load(), item_id)["url"]
        meta = fetch_metadata(url)
        with LOCK:
            items = self.load()
            item = self._find(items, item_id)
            if meta["title"]:
                item["title"] = meta["title"]
            item["description"] = meta["description"] or item.get("description", "")
            item["icon"] = meta["icon"] or item.get("icon", "")
            item["updated_at"] = now_iso()
            write_private_json(self.data_path, items)
        return item

    def delete(self, item_id):
        with LOCK:
            items = self.load()
            self._find(items, item_id)
            write_private_json(self.data_path, [b for b in items if b["id"] != item_id])

    @staticmethod
    def _find(items, item_id):
        item = next((b for b in items if b.get("id") == item_id), None)
        if not item:
            raise BookmarkError("收藏不存在", 404)
        return item
