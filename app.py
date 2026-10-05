#!/usr/bin/env python3
"""Nova Workspace — Notion 风格个人门户后端。

职责：
  1. 托管 web/ 下的静态页面；
  2. /api/server-stats 返回本机 CPU / 内存 / 磁盘 / 负载 / 在线状态；
  3. /api/mail 读取 data/mail.json —— 后台线程按 config/mail.yml（极简格式）
     通过 IMAP 拉取未读与“重要”邮件；凭证未配置时返回空结果，不报错；
  4. /api/finance* 订阅与记账（见 finance.py）；/api/bookmarks* 收藏夹（见 bookmarks.py）；
  5. /assets/site-config.js、/assets/schedule.json：个人站点配置与课表，读 config/ 下的私有文件，
     不存在时退回 web/assets/ 里的示例（*.example.*），所以仓库里不含个人信息；
  6. /api/health；
  7. /api/translate：Edge 翻译插件（extension/）的后端，见 translate.py。

仅监听 127.0.0.1，公网访问由 Caddy + Authelia 把关。
仅使用 Python 标准库，pip 无需安装任何东西。
"""

import email
import email.header
import email.utils
import imaplib
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote, unquote
import urllib.request

import bookmarks
import canvas_sync
import finance
import llm_triage
import news_digest
import news_saved
import news_stock
import news_summary
import translate

IPINFO_CACHE = {}  # ip -> (ts, data) 访客归属地缓存

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
CONFIG_DIR = os.path.join(BASE_DIR, "config")
DATA_DIR = os.path.join(BASE_DIR, "data")
LLM_CONFIG = os.path.join(CONFIG_DIR, "llm.yml")
NOTE_BODY_MAX_CHARS = 100_000

# ---------------------------------------------------------------- 多租户
# 身份来自 Caddy 的 forward_auth：Authelia 返回 Remote-User / Remote-Groups，
# Caddy 在 route 开头先 `request_header -Remote-*` 剥掉客户端伪造的同名头。
#
#   每人一份：data/users/<uid>/{notes,attachments,finance,bookmarks,mail,canvas}
#             config/users/<uid>/{site.json,schedule.json,mail.yml,canvas.yml,notebook.yml}
#   全站一份：data/{news_stock.json,news_pending.json,news_summary_cache.json,fx.json,triage_cache.json}
#             —— News 摘要烧的是 Codex 额度、linux.do 有 Cloudflare 限流，
#                共享缓存后每加一个用户的外部调用增量为 0。
USERS_DATA_DIR = os.path.join(DATA_DIR, "users")
USERS_CONFIG_DIR = os.path.join(CONFIG_DIR, "users")
SITE_DEFAULT_CONFIG = os.path.join(CONFIG_DIR, "site.default.json")
FX_DATA = os.path.join(DATA_DIR, "fx.json")

TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
ADMIN_GROUPS = {"admins"}
DEFAULT_TENANT = os.environ.get("HOME_PORTAL_DEFAULT_TENANT", "nova")
# 1 = 没有 Remote-User 一律 401；0 = 退回站主（仅供本机直连调试，上线后应置 1）
STRICT_AUTH = os.environ.get("HOME_PORTAL_STRICT_AUTH", "0") == "1"

# 子页 -> 所需功能开关。功能未开时连静态页都不发（前端隐藏不算安全）。
PAGE_FEATURES = {
    "finance.html": "finance",
    "notebook.html": "notebook",
    "bookmarks.html": "bookmarks",
    "news.html": "news",
    "tasks.html": "tasks",
    "calendar.html": "calendar",
}
# 只给管理员的子页（服务器订阅/API 台账），非管理员连静态文件都 404
ADMIN_PAGES = {"oneapi.html"}
ONEAPI_PATH = os.path.join(CONFIG_DIR, "oneapi.json")


def env_has(path, var):
    """.env 里 var 有非空值；只看有没有，不读出值。"""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                k, sep, v = line.strip().partition("=")
                if sep and k.strip() == var:
                    return bool(v.strip().strip("'\""))
    except OSError:
        pass
    return False


def oneapi_ledger():
    """config/oneapi.json 台账 + 每项凭据是否在位（只报在/不在，绝不回传内容）。"""
    with open(ONEAPI_PATH, encoding="utf-8") as f:
        data = json.load(f)
    for group in data.get("groups", []):
        for item in group.get("items", []):
            cred = item.get("credential") or {}
            path = cred.get("file")
            if not path:
                item["credential_ok"] = None
            elif cred.get("var"):
                item["credential_ok"] = env_has(path, cred["var"])
            else:
                item["credential_ok"] = os.path.isfile(path)
    return data


class Tenant:
    """一个用户的数据/配置视图。所有路径都被限制在他自己的目录下。"""

    __slots__ = ("uid", "groups", "is_admin")

    def __init__(self, uid, groups=()):
        self.uid = uid
        self.groups = {str(g).strip().lower() for g in groups if str(g).strip()}
        self.is_admin = bool(self.groups & ADMIN_GROUPS)

    def data(self, name):
        return os.path.join(USERS_DATA_DIR, self.uid, name)

    def config(self, name):
        return os.path.join(USERS_CONFIG_DIR, self.uid, name)

    @property
    def data_dir(self):
        return os.path.join(USERS_DATA_DIR, self.uid)

    notes_path = property(lambda self: self.data("notes.json"))
    attach_meta = property(lambda self: self.data("attachments.json"))
    attach_dir = property(lambda self: self.data("attachments"))
    mail_data = property(lambda self: self.data("mail.json"))
    mail_config = property(lambda self: self.config("mail.yml"))
    canvas_data = property(lambda self: self.data("canvas.json"))
    canvas_config = property(lambda self: self.config("canvas.yml"))

    def ensure_dirs(self):
        os.makedirs(self.data_dir, mode=0o700, exist_ok=True)

    def features(self):
        """site.json 里的 features 数组；没写 = 全部功能开放。"""
        try:
            feats = site_config(self).get("features")
        except (OSError, ValueError):
            return None
        return {str(f) for f in feats} if isinstance(feats, list) else None

    def has(self, feature):
        feats = self.features()
        return feats is None or feature in feats


TENANT_MAP_FILE = os.path.join(CONFIG_DIR, "tenants.json")
_TENANT_MAP = {"stamp": None, "aliases": {}}


def tenant_alias(name):
    """Authelia 用户名 -> 门户租户 id（config/tenants.json 的 aliases）。

    一个人可能有多个登录账号（admin/cjt 都是站主），映射到同一份数据。
    没列出的用户名直接当租户 id 用。
    """
    stamp = path_mtime(TENANT_MAP_FILE)
    if _TENANT_MAP["stamp"] != stamp:
        aliases = {}
        try:
            raw = _load_json_object(TENANT_MAP_FILE).get("aliases", {})
            if isinstance(raw, dict):
                aliases = {str(k).strip().lower(): str(v).strip().lower()
                           for k, v in raw.items()}
        except (OSError, ValueError):
            aliases = {}
        _TENANT_MAP["aliases"] = aliases
        _TENANT_MAP["stamp"] = stamp
    return _TENANT_MAP["aliases"].get(name, name)


def valid_tenant(uid):
    return bool(uid and TENANT_RE.match(uid)
                and os.path.isdir(os.path.join(USERS_CONFIG_DIR, uid)))


def iter_tenants():
    """按 config/users/<uid>/ 枚举租户，供后台轮询使用。"""
    try:
        names = sorted(os.listdir(USERS_CONFIG_DIR))
    except OSError:
        return []
    return [Tenant(n, ["admins"] if n == DEFAULT_TENANT else [])
            for n in names
            if TENANT_RE.match(n) and os.path.isdir(os.path.join(USERS_CONFIG_DIR, n))]


def path_mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def deep_merge(base, override):
    """字典递归合并；数组整体替换（导航/链接类配置按整组覆盖更直观）。"""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_json_object(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("%s 必须是 JSON 对象" % os.path.basename(path))
    return data


SITE_CACHE = {}
SITE_CACHE_LOCK = threading.Lock()


def site_config(t):
    """config/site.default.json（缺省退回 site.example.json）叠加 config/users/<uid>/site.json。"""
    base_path = (SITE_DEFAULT_CONFIG if os.path.isfile(SITE_DEFAULT_CONFIG)
                 else os.path.join(WEB_DIR, "assets", "site.example.json"))
    user_path = t.config("site.json")
    stamp = (base_path, path_mtime(base_path), path_mtime(user_path))
    with SITE_CACHE_LOCK:
        hit = SITE_CACHE.get(t.uid)
        if hit and hit[0] == stamp:
            return hit[1]
    merged = _load_json_object(base_path)
    if os.path.isfile(user_path):
        merged = deep_merge(merged, _load_json_object(user_path))
    with SITE_CACHE_LOCK:
        SITE_CACHE[t.uid] = (stamp, merged)
    return merged


# 每租户一个 Store；Store 只持有路径，建起来很便宜。汇率文件全站共享。
STORES = {}
STORES_LOCK = threading.Lock()


def stores_for(t):
    with STORES_LOCK:
        hit = STORES.get(t.uid)
        if hit is None:
            hit = (bookmarks.Store(t.data("bookmarks.json")),
                   finance.Store(t.data("finance.json"), FX_DATA))
            STORES[t.uid] = hit
        return hit

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("HOME_PORTAL_PORT", "8080"))
MAIL_POLL_SECONDS = 300
MAIL_LOOKBACK_DAYS = 7
MAIL_MAX_PER_ACCOUNT = 20
MAIL_BODY_PREVIEW_CHARS = 1500
MAIL_READ_SYNC_SECONDS = 30  # auto_remove_read 账号的已读状态同步间隔（只做 IMAP SEARCH，不调用 LLM）
CANVAS_POLL_SECONDS = 600

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json",
    ".apk": "application/vnd.android.package-archive",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

MAIL_LOCK = threading.Lock()
NOTES_LOCK = threading.Lock()
ATTACH_LOCK = threading.Lock()  # 加锁顺序：NOTES_LOCK -> ATTACH_LOCK


def read_notes(t):
    try:
        with open(t.notes_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [normalize_note(n) for n in data if isinstance(n, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def write_notes(t, notes):
    write_private_json(t.notes_path, notes)


def write_private_json(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        os.chmod(tmp, 0o600)
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def normalize_note(note):
    """旧笔记没有标签/置顶/归档字段，读取时补默认值。"""
    note.setdefault("tags", [])
    note.setdefault("pinned", False)
    note.setdefault("archived", False)
    return note


def clean_tags(value):
    if not isinstance(value, list):
        raise ValueError("tags 必须是数组")
    tags = []
    for item in value:
        tag = re.sub(r"\s+", " ", str(item)).strip().lstrip("#").strip()[:30]
        if tag and tag.lower() not in (t.lower() for t in tags):
            tags.append(tag)
    if len(tags) > 20:
        raise ValueError("每篇笔记最多 20 个标签")
    return tags


# ---------------------------------------------------------------- note attachments
# 文件存 data/attachments/<id>（无扩展名，权限 600），元数据存 data/attachments.json。
# 配额可在 config/notebook.yml 覆盖：quota_mb / max_file_mb / min_free_mb。

def parse_notebook_config(t):
    # 配额按租户算，否则一个人就能把根盘塞满、连带搞挂 Poste 和 Authelia
    cfg = {"quota_mb": 1024 if t.is_admin else 256, "max_file_mb": 25, "min_free_mb": 2048}
    try:
        with open(t.config("notebook.yml"), "r", encoding="utf-8") as f:
            for raw_line in f:
                key, sep, value = raw_line.split("#", 1)[0].partition(":")
                if sep and key.strip() in cfg and value.strip().isdigit():
                    cfg[key.strip()] = int(value.strip())
    except OSError:
        pass
    return {"quota": cfg["quota_mb"] * 1024 * 1024,
            "max_file": cfg["max_file_mb"] * 1024 * 1024,
            "min_free": cfg["min_free_mb"] * 1024 * 1024}


def read_attachments(t):
    try:
        with open(t.attach_meta, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def attachment_storage(t, attachments):
    cfg = parse_notebook_config(t)
    return {"used": sum(int(a.get("size", 0)) for a in attachments),
            "quota": cfg["quota"], "max_file": cfg["max_file"]}


def remove_attachment_file(t, attachment_id):
    try:
        os.remove(os.path.join(t.attach_dir, attachment_id))
    except FileNotFoundError:
        pass


# 只有这些类型允许在门户域名下内联显示；其余一律按下载处理，避免上传的 HTML/SVG 在门户域名执行脚本。
INLINE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif",
                "image/bmp", "application/pdf"}


# ---------------------------------------------------------------- server stats

def read_proc(path):
    try:
        with open(path, "r") as f:
            return f.read()
    except OSError:
        return ""


def cpu_sample():
    """返回 (busy, total) jiffies。"""
    line = read_proc("/proc/stat").splitlines()[0].split()[1:]
    nums = [int(x) for x in line]
    idle = nums[3] + nums[4]
    total = sum(nums)
    return total - idle, total


def cpu_percent(sample_every=0.25):
    a = cpu_sample()
    time.sleep(sample_every)
    b = cpu_sample()
    dbusy = b[0] - a[0]
    dtotal = b[1] - a[1]
    return round(dbusy * 100.0 / dtotal, 1) if dtotal > 0 else 0.0


def memory_stats():
    info = {}
    for line in read_proc("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.strip().split()
        if parts:
            info[key] = int(parts[0]) * 1024  # kB -> bytes
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    used = total - avail
    return {"total": total, "used": used, "available": avail,
            "percent": round(used * 100.0 / total, 1) if total else 0.0}


def disk_stats(path="/"):
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - free
    return {"total": total, "used": used, "available": free,
            "percent": round(used * 100.0 / total, 1) if total else 0.0}


def uptime_seconds():
    try:
        return int(open("/proc/uptime").read().split()[0].split(".")[0])
    except OSError:
        return 0


def load_averages():
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except OSError:
        return [0, 0, 0]


def server_stats():
    return {
        "hostname": os.uname().nodename,
        "status": "online",
        "time": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "cpu_percent": cpu_percent(),
        "memory": memory_stats(),
        "disk": disk_stats("/"),
        "load": load_averages(),
        "uptime": uptime_seconds(),
    }


# ---------------------------------------------------------------- mail polling

def decode_header_value(raw):
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(enc or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out).strip()


def parse_simple_mail_config(path):
    """极简 mail 配置解析（避免引入 PyYAML 依赖）。

    格式（缩进表示账号字段，# 开头注释）：

      accounts:
        - name: QQ邮箱
          email: you@example.com
          imap_host: imap.qq.com
          imap_port: 993
          username: you@example.com
          password: 授权码
          webmail: https://mail.qq.com/
          important_senders:
            - noreply@example.com
          important_keywords:
            - 账单
    """
    if not os.path.exists(path):
        return []
    accounts = []
    current = None
    list_key = None
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(line) - len(line.lstrip(" "))

            # 新账号：accounts 下缩进 2 的 "- name: ..."
            if indent == 2 and stripped.startswith("- "):
                current = {}
                accounts.append(current)
                stripped = stripped[2:].strip()
                list_key = None
            elif stripped.startswith("- "):
                # 账号内部的列表项（important_senders / important_keywords）
                item = stripped[2:].strip()
                if list_key and item and current is not None:
                    current.setdefault(list_key, []).append(item)
                continue

            key, sep, value = stripped.partition(":")
            if not sep:
                continue
            key = key.strip()
            value = value.strip()
            if indent <= 2:
                list_key = None
            if value == "":
                list_key = key
                if current is not None:
                    current.setdefault(key, [])
                continue
            list_key = None
            if current is None and key == "accounts":
                continue
            if current is not None:
                current[key] = value

    def usable(a):
        addr = a.get("email", "")
        user = a.get("username") or addr
        pw = a.get("password", "")
        if not addr or not pw:
            return False
        if "yourname" in addr:  # 示例未替换
            return False
        if pw.startswith("在此"):  # 中文占位符未替换
            return False
        # IMAP LOGIN 只接受 ASCII 凭证，非 ASCII 必然编码失败
        if not (addr.isascii() and user.isascii() and pw.isascii()):
            return False
        return True

    return [a for a in accounts if usable(a)]


def parse_llm_config(path):
    """解析极简 key: value 配置，返回 None 表示未启用。"""
    if not os.path.exists(path):
        return None
    cfg = {}
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, _, value = line.partition(":")
            cfg[key.strip()] = value.strip()
    if cfg.get("enabled", "true").lower() in ("false", "0", "no"):
        return None
    key = cfg.get("api_key", "")
    if not key or key.startswith("sk-在此") or not key.isascii():
        return None
    if not cfg.get("base_url") or not cfg.get("model"):
        return None
    return {"api_key": key, "base_url": cfg["base_url"], "model": cfg["model"]}


def extract_body_preview(msg, limit=MAIL_BODY_PREVIEW_CHARS):
    """从 email.message 提取纯文本正文前 limit 字符。"""
    chunks = []

    def walk(part):
        if part.is_multipart():
            for sub in part.walk():
                if sub is part:
                    continue
                walk(sub)
            return
        ctype = (part.get_content_type() or "").lower()
        disp = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disp or ctype not in ("text/plain", "text/html"):
            return
        try:
            payload = part.get_payload(decode=True)
            if not payload:
                return
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
        except (LookupError, UnicodeError, OSError):
            return
        if ctype == "text/html":
            text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
            text = email.utils.unquote(text) if False else text
        chunks.append(text)

    walk(msg)
    body = " ".join(chunks)
    body = re.sub(r"\s+", " ", body).strip()
    return body[:limit]


def open_mailbox(acc):
    host = acc.get("imap_host", "")
    port = int(acc.get("imap_port", "993"))
    if acc.get("ssl", "true").lower() != "false":
        conn = imaplib.IMAP4_SSL(host, port, timeout=20)
    else:
        conn = imaplib.IMAP4(host, port, timeout=20)
        # 明文端口通常支持 STARTTLS；回环上不支持就直接明文
        if "STARTTLS" in (conn.capabilities or ""):
            conn.starttls()
    conn.login(acc.get("username") or acc["email"], acc["password"])
    conn.select("INBOX", readonly=True)
    return conn


def search_unseen_uids(conn):
    since = (datetime.now().astimezone() -
             timedelta(days=MAIL_LOOKBACK_DAYS)
             ).strftime("%d-%b-%Y")
    typ, data = conn.uid("SEARCH", None, "UNSEEN", f"(SINCE {since})")
    if typ != "OK":
        # 某些服务器不接受多条件，退回纯 UNSEEN
        typ, data = conn.uid("SEARCH", None, "UNSEEN")
    if typ != "OK":
        raise RuntimeError("IMAP SEARCH 失败")
    return [uid.decode() for uid in data[0].split()] if data and data[0] else []


def acc_flag(acc, key):
    return str(acc.get(key, "")).lower() in ("true", "yes", "1")


def fetch_account_mail(acc, llm_cfg, triage_cache):
    """连接单个账号，返回未读邮件中“重要”的列表。失败返回 {"error": ...}。

    判定优先 LLM；LLM 不可用/未配置时回退关键词与发件人规则。
    """
    senders = [s.lower() for s in acc.get("important_senders", []) if s]
    # 命中 unimportant_senders 的邮件直接判为不重要，优先于关键词规则，也不调用 LLM
    muted = [s.lower() for s in acc.get("unimportant_senders", []) if s]
    keywords = [k.lower() for k in acc.get("important_keywords", []) if k]
    result = {
        "name": acc.get("name", acc["email"]),
        "email": acc["email"],
        "webmail": acc.get("webmail", ""),
        "unseen": 0,
        "important_count": 0,
        "messages": [],
        "error": None,
        "updated": None,
        "triage": "llm" if llm_cfg else "rules",
        "auto_remove_read": acc_flag(acc, "auto_remove_read"),
    }
    conn = None
    try:
        conn = open_mailbox(acc)
        ids = search_unseen_uids(conn)
        result["unseen"] = len(ids)
        for uid in reversed(ids[-MAIL_MAX_PER_ACCOUNT:]):
            typ, msg_data = conn.uid(
                "FETCH", uid,
                "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)] BODY.PEEK[TEXT])",
            )
            if typ != "OK" or not msg_data:
                continue
            header_bytes = b""
            body_bytes = b""
            for part in msg_data:
                if not isinstance(part, tuple) or len(part) < 2:
                    continue
                meta = part[0] if isinstance(part[0], bytes) else b""
                if b"HEADER" in meta:
                    header_bytes += part[1]
                elif b"TEXT" in meta:
                    body_bytes += part[1]
            msg = email.message_from_bytes(header_bytes)
            frm = decode_header_value(msg.get("From", ""))
            subject = decode_header_value(msg.get("Subject", ""))
            message_id = (msg.get("Message-ID") or "").strip()
            addr_match = re.search(r"<([^>]+)>", frm)
            from_addr = (addr_match.group(1) if addr_match else frm).lower()
            if any(s in from_addr for s in muted):
                continue
            rule_important = (
                any(s in from_addr for s in senders)
                or any(k in subject.lower() for k in keywords)
            )

            important = rule_important
            category = "规则命中" if rule_important else ""
            reason = ""
            if llm_cfg:
                body_msg = email.message_from_bytes(
                    header_bytes + b"\r\n" + body_bytes)
                verdict = llm_triage.classify(
                    {
                        "message_id": message_id,
                        "from": frm,
                        "subject": subject,
                        "body_preview": extract_body_preview(body_msg),
                    },
                    llm_cfg,
                    triage_cache,
                )
                if verdict is not None:
                    important = verdict["important"]
                    category = verdict.get("category", "")
                    reason = verdict.get("reason", "")
                # LLM 失败时沿用 rule_important 兜底

            if not important:
                continue
            result["messages"].append({
                "uid": uid,
                "from": frm or from_addr,
                "subject": subject or "(无主题)",
                "date": msg.get("Date", ""),
                "important": True,
                "category": category,
                "reason": reason,
            })
        result["important_count"] = len(result["messages"])
        result["updated"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    except Exception as exc:  # noqa: BLE001 — 任何账号失败只记录，不影响其他账号
        if isinstance(exc, (UnicodeError, UnicodeEncodeError)):
            result["error"] = "账号或授权码含非 ASCII 字符，请检查配置"
        else:
            result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass
    return result


def write_mail_data(t, payload):
    t.ensure_dirs()
    write_private_json(t.mail_data, payload)


def read_mail_data(t):
    if not os.path.exists(t.mail_data):
        return {"updated": None, "accounts": []}
    try:
        with open(t.mail_data, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"updated": None, "accounts": []}


def sync_read_state(t):
    """auto_remove_read 账号：只查当前未读 UID，把已读（或已删除）的邮件从主页数据中移除。"""
    accounts_cfg = {a["email"]: a for a in parse_simple_mail_config(t.mail_config)
                    if acc_flag(a, "auto_remove_read")}
    if not accounts_cfg:
        return
    unseen = {}
    for addr, acc in accounts_cfg.items():
        conn = None
        try:
            conn = open_mailbox(acc)
            unseen[addr] = set(search_unseen_uids(conn))
        except Exception as exc:  # noqa: BLE001 — 同步失败保留上一轮结果，等下次完整轮询
            print("mail read-sync error:", addr, type(exc).__name__, exc, flush=True)
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    pass
    if not unseen:
        return
    with MAIL_LOCK:
        data = read_mail_data(t)
        changed = False
        for account in data.get("accounts", []):
            uids = unseen.get(account.get("email"))
            if uids is None or account.get("error"):
                continue
            kept = [m for m in account.get("messages", []) if m.get("uid") is None or m.get("uid") in uids]
            if len(kept) != len(account.get("messages", [])) or account.get("unseen") != len(uids):
                account["messages"] = kept
                account["important_count"] = len(kept)
                account["unseen"] = len(uids)
                changed = True
        if changed:
            write_mail_data(t, data)


# 每租户一把节流锁：一个人点“立即刷新”不该卡住别人
READ_SYNC_SLOTS = {}
READ_SYNC_REGISTRY = threading.Lock()


def read_sync_slot(uid):
    with READ_SYNC_REGISTRY:
        slot = READ_SYNC_SLOTS.get(uid)
        if slot is None:
            slot = READ_SYNC_SLOTS[uid] = [threading.Lock(), 0.0]
        return slot


def sync_read_state_throttled(t, min_interval=5):
    """主页“立即刷新”/切回页面时调用；并发或 5 秒内重复的请求直接跳过。"""
    slot = read_sync_slot(t.uid)
    if not slot[0].acquire(blocking=False):
        return
    try:
        if time.monotonic() - slot[1] >= min_interval:
            slot[1] = time.monotonic()
            sync_read_state(t)
    finally:
        slot[0].release()


def read_sync_poller():
    time.sleep(15)
    while True:
        for t in iter_tenants():
            if not os.path.isfile(t.mail_config):
                continue
            try:
                sync_read_state_throttled(t, min_interval=0)
            except Exception as exc:  # noqa: BLE001 — 单个租户失败不影响其他人
                print("mail read-sync error:", t.uid, exc, flush=True)
        time.sleep(MAIL_READ_SYNC_SECONDS)


def mail_poller():
    # 启动后稍等再拉，避免与服务启动抢资源
    time.sleep(5)
    triage_cache = llm_triage.load_cache()
    while True:
        # 配置可能随时补凭证，每轮都重新读取
        llm_cfg = parse_llm_config(LLM_CONFIG)
        for t in iter_tenants():
            try:
                accounts_cfg = parse_simple_mail_config(t.mail_config)
                if accounts_cfg:
                    payload = {
                        "updated": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                        "configured": len(accounts_cfg),
                        "triage": "llm" if llm_cfg else "rules",
                        "accounts": [fetch_account_mail(a, llm_cfg, triage_cache)
                                     for a in accounts_cfg],
                    }
                else:
                    payload = {"updated": None, "configured": 0,
                               "triage": "llm" if llm_cfg else "rules",
                               "accounts": []}
                with MAIL_LOCK:
                    write_mail_data(t, payload)
            except Exception as exc:  # noqa: BLE001 — 同上
                print("mail poll error:", t.uid, exc, flush=True)
        time.sleep(MAIL_POLL_SECONDS)


# ---------------------------------------------------------------- canvas
# 每租户一把锁：朋友可能接的是别的学校的 Canvas（base_url/token 都在他自己的
# canvas.yml 里）。对方 API 抽风时只能卡住他自己的同步，不能卡住全站。

CANVAS_LOCKS = {}
CANVAS_LOCK_REGISTRY = threading.Lock()


def canvas_lock(uid):
    with CANVAS_LOCK_REGISTRY:
        return CANVAS_LOCKS.setdefault(uid, threading.Lock())


def canvas_poller():
    time.sleep(8)
    while True:
        for t in iter_tenants():
            if not os.path.isfile(t.canvas_config):
                continue
            t.ensure_dirs()
            with canvas_lock(t.uid):
                try:
                    canvas_sync.sync_once(t.canvas_config, t.canvas_data)
                except Exception as exc:  # noqa: BLE001
                    print("canvas sync error:", t.uid, exc, flush=True)
            time.sleep(2)  # 错峰，别让多所学校的 API 在同一秒被打
        time.sleep(CANVAS_POLL_SECONDS)


# ---------------------------------------------------------------- HTTP

SAFE_PREFIX = os.path.realpath(WEB_DIR)


def safe_static_path(url_path):
    rel = url_path.lstrip("/")
    if not rel:
        rel = "index.html"
    candidate = os.path.realpath(os.path.join(WEB_DIR, rel))
    if not (candidate == SAFE_PREFIX or candidate.startswith(SAFE_PREFIX + os.sep)):
        return None
    return candidate


class Handler(BaseHTTPRequestHandler):
    server_version = "HomePortal/1.0"

    def log_message(self, fmt, *args):
        # Caddy 前面已有访问日志，这里保持安静；错误单独打印
        pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Content-Type 必须是 application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("无效的 Content-Length") from exc
        # 10 万字中文 UTF-8 约 300KB，再加 JSON 转义，留足余量
        if length < 1 or length > 1_000_000:
            raise ValueError("请求内容为空或过大")
        try:
            data = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("无效的 JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("请求内容必须是对象")
        return data

    # ------------------------------------------------------------ 租户
    # Caddy 已在 route 开头 `request_header -Remote-*` 剥掉客户端伪造的同名头，
    # 这里读到的 Remote-User 只可能来自 Authelia 的 forward-auth 响应。

    def resolve_tenant(self):
        uid = (self.headers.get("Remote-User") or "").strip().lower()
        groups = [g for g in (self.headers.get("Remote-Groups") or "").split(",")]
        if not uid:
            if STRICT_AUTH:
                return None
            # 本机直连调试：退回站主，并留一行日志好排查
            print("portal: no Remote-User, falling back to", DEFAULT_TENANT,
                  self.path, flush=True)
            return Tenant(DEFAULT_TENANT, ["admins"])
        uid = tenant_alias(uid)
        if not valid_tenant(uid):
            print("portal: unknown tenant", uid, self.path, flush=True)
            return None
        return Tenant(uid, groups)

    def begin(self):
        """解析租户。失败时已写好响应并返回 None。"""
        t = self.resolve_tenant()
        if t is None:
            self.send_json({"error": "未登录或账号未开通门户"}, status=401)
            return None
        t.ensure_dirs()
        return t

    def require(self, t, feature):
        if t.has(feature):
            return True
        self.send_json({"error": "该功能未对当前账号开放"}, status=403)
        return False

    def require_admin(self, t):
        if t.is_admin:
            return True
        self.send_json({"error": "仅管理员可用"}, status=403)
        return False

    def serve_public(self, path):
        """不需要身份的路径（Caddy 也在 Authelia 之前放行了这些）。命中返回 True。"""
        if path == "/api/health":
            self.send_json({"status": "ok"})
            return True
        if path == "/manifest.webmanifest":
            self.serve_static_file(os.path.join(WEB_DIR, "assets", "manifest.webmanifest"))
            return True
        if path == "/.well-known/assetlinks.json":
            self.serve_static_file(os.path.join(WEB_DIR, "assets", "assetlinks.json"),
                                   no_cache=True)
            return True
        if path == "/api/workbench-version":
            self.serve_static_file(
                os.path.join(WEB_DIR, "assets", "apk", "workbench-version.json"),
                no_cache=True)
            return True
        if path == "/assets/apk/Workbench.apk":
            self.serve_static_file(
                os.path.join(WEB_DIR, "assets", "apk", "Workbench.apk"), no_cache=True)
            return True
        if path.startswith("/assets/app-icons/"):
            static_path = safe_static_path(path)
            if static_path and os.path.isfile(static_path):
                self.serve_static_file(static_path)
                return True
        return False

    def route_id(self, pattern):
        match = re.fullmatch(pattern, urlparse(self.path).path)
        return match.group(1) if match else None

    def serve_static_file(self, path, no_cache=False):
        """安全静态文件输出（用于 manifest / assetlinks 等路由直出的文件）。"""
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        ext = os.path.splitext(path)[1]
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream"))
        if no_cache:
            self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_site_config(self, t):
        """site.default.json 叠加 config/users/<uid>/site.json，包成 window.PORTAL_SITE。"""
        try:
            merged = dict(site_config(t))
        except (OSError, ValueError) as exc:
            self.send_json({"error": "site.json 读取失败：%s" % exc}, status=500)
            return
        merged["user"] = {"id": t.uid, "admin": t.is_admin}
        feats = t.features()
        merged["features"] = sorted(feats) if feats is not None else None
        body = (b"window.PORTAL_SITE = "
                + json.dumps(merged, ensure_ascii=False).encode("utf-8") + b";\n")
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES[".js"])
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_private_asset(self, t, name, example, as_script=False):
        """config/users/<uid>/<name> 存在时用它，否则用 web/assets/<example>。"""
        path = t.config(name)
        if not os.path.isfile(path):
            path = os.path.join(WEB_DIR, "assets", example)
        try:
            with open(path, "rb") as f:
                body = f.read()
            if as_script:
                json.loads(body)  # 配置写坏时返回 500，而不是一段语法错误的脚本
                body = b"window.PORTAL_SITE = " + body.strip() + b";\n"
        except (OSError, ValueError) as exc:
            self.send_json({"error": "%s 读取失败：%s" % (name, exc)}, status=500)
            return
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES[".js" if as_script else ".json"])
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_bookmarks(self, method, t):
        """/api/bookmarks 路由；命中返回 True。"""
        if not urlparse(self.path).path.startswith("/api/bookmarks"):
            return False
        if not self.require(t, "bookmarks"):
            return True
        BOOKMARKS = stores_for(t)[0]
        path = urlparse(self.path).path
        item_id = self.route_id(r"/api/bookmarks/([a-f0-9]{16})(?:/refresh)?")
        try:
            if path == "/api/bookmarks" and method == "GET":
                self.send_json(BOOKMARKS.listing())
            elif path == "/api/bookmarks" and method == "POST":
                item, created = BOOKMARKS.create(self.read_json())
                self.send_json({"bookmark": item, "created": created}, status=201 if created else 200)
            elif item_id and path.endswith("/refresh") and method == "POST":
                self.send_json(BOOKMARKS.refresh(item_id))
            elif item_id and method == "PUT":
                self.send_json(BOOKMARKS.update(item_id, self.read_json()))
            elif item_id and method == "DELETE":
                BOOKMARKS.delete(item_id)
                self.send_json({"deleted": True})
            else:
                self.send_json({"error": "not found"}, status=404)
        except bookmarks.BookmarkError as exc:
            self.send_json({"error": str(exc)}, status=exc.status)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, status=400)
        return True

    def handle_finance(self, method, t):
        """/api/finance 路由；命中返回 True。"""
        path = urlparse(self.path).path
        if not path.startswith("/api/finance"):
            return False
        if not self.require(t, "finance"):
            return True
        FINANCE = stores_for(t)[1]
        query = parse_qs(urlparse(self.path).query)
        try:
            if path == "/api/finance" and method == "GET":
                self.send_json(FINANCE.payload())
            elif path == "/api/finance/fx" and method == "GET":
                if query.get("refresh"):
                    self.send_json(finance.refresh_rates_throttled(FINANCE.fx_path))
                else:
                    self.send_json(finance.read_fx(FINANCE.fx_path))
            elif path == "/api/finance/subscriptions" and method == "POST":
                self.send_json(FINANCE.create_subscription(self.read_json()), status=201)
            elif path == "/api/finance/transactions" and method == "POST":
                self.send_json(FINANCE.create_transaction(self.read_json()), status=201)
            else:
                sub_id = self.route_id(r"/api/finance/subscriptions/([a-f0-9]{16})")
                txn_id = self.route_id(r"/api/finance/transactions/([a-f0-9]{16})")
                if sub_id and method == "PUT":
                    self.send_json(FINANCE.update_subscription(sub_id, self.read_json()))
                elif sub_id and method == "DELETE":
                    FINANCE.delete_subscription(sub_id, bool(query.get("with_transactions")))
                    self.send_json({"deleted": True})
                elif txn_id and method == "PUT":
                    self.send_json(FINANCE.update_transaction(txn_id, self.read_json()))
                elif txn_id and method == "DELETE":
                    FINANCE.delete_transaction(txn_id)
                    self.send_json({"deleted": True})
                else:
                    self.send_json({"error": "not found"}, status=404)
        except finance.FinanceError as exc:
            self.send_json({"error": str(exc)}, status=exc.status)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, status=400)
        except RuntimeError as exc:  # 手动刷新汇率失败
            self.send_json({"error": str(exc)}, status=502)
        return True

    def note_id(self):
        return self.route_id(r"/api/notes/([a-f0-9]{16})")

    def drain_body(self, length):
        """拒绝上传前读掉请求体，否则 Caddy 可能把提前关闭的连接报成 502。"""
        remaining = min(max(length, 0), 1024 * 1024 * 1024)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1024 * 1024))
            if not chunk:
                break
            remaining -= len(chunk)

    def send_attachment(self, t, attachment_id):
        # 元数据和文件都只在该租户目录下找，别人的 id 在这里天然查不到
        with ATTACH_LOCK:
            meta = next((a for a in read_attachments(t) if a.get("id") == attachment_id), None)
        path = os.path.join(t.attach_dir, attachment_id)
        if not meta or not os.path.isfile(path):
            self.send_json({"error": "附件不存在"}, status=404)
            return
        mime = meta.get("mime", "application/octet-stream")
        inline = mime in INLINE_TYPES
        name = meta.get("name", "attachment")
        self.send_response(200)
        self.send_header("Content-Type", mime if inline else "application/octet-stream")
        self.send_header("Content-Disposition", "%s; filename=\"attachment\"; filename*=UTF-8''%s"
                         % ("inline" if inline else "attachment", quote(name, safe="")))
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
        if mime != "application/pdf":
            self.send_header("Content-Security-Policy",
                             "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'")
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def upload_attachment(self, t, note_id):
        cfg = parse_notebook_config(t)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        name = unquote(self.headers.get("X-File-Name", ""))
        name = re.sub(r"[\x00-\x1f\x7f/\\]", "_", os.path.basename(name)).strip()[:120] or "附件"
        mime = self.headers.get_content_type().lower()
        if not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", mime):
            mime = "application/octet-stream"

        def reject(message, status):
            self.drain_body(length)
            self.send_json({"error": message}, status=status)

        if length < 1:
            reject("文件为空", 400)
            return
        if length > cfg["max_file"]:
            reject("单个文件不能超过 %d MB" % (cfg["max_file"] // 1048576), 413)
            return
        with NOTES_LOCK:
            exists = any(n.get("id") == note_id for n in read_notes(t))
        if not exists:
            reject("笔记不存在", 404)
            return
        with ATTACH_LOCK:
            used = attachment_storage(t, read_attachments(t))["used"]
        if used + length > cfg["quota"]:
            reject("附件空间不足（配额 %d MB）" % (cfg["quota"] // 1048576), 413)
            return

        os.makedirs(t.attach_dir, mode=0o700, exist_ok=True)
        attachment_id = secrets.token_hex(8)
        tmp = os.path.join(t.attach_dir, ".upload-" + attachment_id)
        received = 0
        try:
            with open(tmp, "wb") as f:
                os.chmod(tmp, 0o600)
                while received < length:
                    chunk = self.rfile.read(min(length - received, 1024 * 1024))
                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)
            if received != length:
                raise OSError("上传中断")
            with NOTES_LOCK, ATTACH_LOCK:
                if not any(n.get("id") == note_id for n in read_notes(t)):
                    raise LookupError("笔记不存在")
                attachments = read_attachments(t)
                storage = attachment_storage(t, attachments)
                if storage["used"] + length > storage["quota"]:
                    raise LookupError("附件空间不足")
                if disk_stats(DATA_DIR)["available"] < cfg["min_free"]:
                    raise LookupError("服务器磁盘剩余空间不足，已拒绝写入")
                os.replace(tmp, os.path.join(t.attach_dir, attachment_id))
                meta = {"id": attachment_id, "note_id": note_id, "name": name, "mime": mime,
                        "size": length, "url": "/api/attachments/" + attachment_id,
                        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                attachments.append(meta)
                write_private_json(t.attach_meta, attachments)
                storage["used"] += length
        except (OSError, LookupError) as exc:
            try:
                os.remove(tmp)
            except OSError:
                pass
            self.send_json({"error": str(exc)}, status=507 if isinstance(exc, LookupError) else 400)
            return
        self.send_json({"attachment": meta, "storage": storage}, status=201)

    def do_GET(self):
        path = urlparse(self.path).path
        if self.serve_public(path):
            return
        t = self.begin()
        if t is None:
            return
        if self.handle_finance("GET", t) or self.handle_bookmarks("GET", t):
            return
        if path == "/assets/site-config.js":
            self.send_site_config(t)
            return
        if path == "/assets/schedule.json":
            # 没配课表就给空表，别拿示例里的假课糊弄人
            if not os.path.isfile(t.config("schedule.json")):
                self.send_json({"semester": "", "title": "", "week_one_monday": None,
                                "max_week": 0, "source": "", "exceptions": {},
                                "courses": []})
                return
            self.send_private_asset(t, "schedule.json", "schedule.example.json")
            return
        if path == "/api/server-stats":
            # 这是服务器本身的 CPU/内存/磁盘，只给管理员
            if not self.require_admin(t):
                return
            try:
                self.send_json(server_stats())
            except Exception as exc:  # noqa: BLE001
                self.send_json({"status": "error", "error": str(exc)}, status=500)
            return
        if path == "/api/oneapi":
            if not self.require_admin(t):
                return
            try:
                self.send_json(oneapi_ledger())
            except (OSError, ValueError) as exc:
                self.send_json({"error": "台账读取失败：%s" % exc}, status=500)
            return
        if path == "/api/translate/extension.zip":
            if not self.require_admin(t):
                return
            body = translate.extension_zip()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="codex-translate.zip"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/ipinfo":
            # 访客 IP 归属地（X-Forwarded-For 由 Caddy 注入；归属地查 ip-api.com，内存缓存 10 分钟）
            ip = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip() or self.client_address[0]
            import ipaddress as _ipa
            def _priv(a):
                try:
                    return _ipa.ip_address(a).is_private or a in ("127.0.0.1", "::1")
                except ValueError:
                    return True
            now = time.time()
            hit = IPINFO_CACHE.get(ip)
            if hit and now - hit[0] < 600:
                self.send_json(hit[1])
                return
            if _priv(ip):
                data = {"ip": ip, "location": "内网 / 本机", "isp": "-", "private": True}
                IPINFO_CACHE[ip] = (now, data)
                self.send_json(data)
                return
            try:
                req = urllib.request.Request(
                    "http://ip-api.com/json/" + ip + "?lang=zh-CN&fields=status,country,regionName,city,isp,query",
                    headers={"User-Agent": "home-portal/1.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    d = json.loads(resp.read().decode("utf-8"))
                if d.get("status") == "success":
                    loc = " / ".join(filter(None, [d.get("country"), d.get("regionName"), d.get("city")]))
                    data = {"ip": ip, "location": loc or "未知", "isp": d.get("isp", "-"), "private": False}
                else:
                    data = {"ip": ip, "location": "查询失败", "isp": "-", "private": False}
            except Exception:  # noqa: BLE001
                data = {"ip": ip, "location": "查询失败", "isp": "-", "private": False}
            IPINFO_CACHE[ip] = (now, data)
            self.send_json(data)
            return
        if path == "/api/news-blocked":
            # 屏蔽列表（管理员；影响共享热榜的准入）
            if not self.require_admin(t):
                return
            self.send_json({"items": news_digest.list_blocked()})
            return
        if path == "/api/news-saved":
            if not self.require(t, "news"):
                return
            self.send_json({"items": news_saved.list_saved(t)})
            return
        if path == "/api/news":
            # 库存制（2026-10-03）：展示集按人保存；?refresh=1 从库存随机换一批（不碰上游，无冷却）
            if not self.require(t, "news"):
                return
            q = parse_qs(urlparse(self.path).query)
            items = news_stock.view(t, refresh=bool(q.get("refresh")))
            self.send_json({"items": items, "count": len(items), "stock": news_stock.size()})
            return
        if path == "/api/mail":
            if not self.require(t, "mail"):
                return
            if parse_qs(urlparse(self.path).query).get("sync"):
                sync_read_state_throttled(t)
            with MAIL_LOCK:
                self.send_json(read_mail_data(t))
            return
        if path == "/api/oc-tasks":
            if not self.require(t, "canvas"):
                return
            if parse_qs(urlparse(self.path).query).get("refresh"):
                with canvas_lock(t.uid):
                    try:
                        self.send_json(
                            canvas_sync.sync_once(t.canvas_config, t.canvas_data))
                    except Exception as exc:  # noqa: BLE001
                        self.send_json({"configured": True, "tasks": [], "messages": [],
                                        "errors": [str(exc)]}, status=502)
                return
            with canvas_lock(t.uid):
                self.send_json(canvas_sync.read_cached(t.canvas_data))
            return
        if path == "/api/notes":
            if not self.require(t, "notebook"):
                return
            with NOTES_LOCK:
                notes = read_notes(t)
            with ATTACH_LOCK:
                attachments = read_attachments(t)
            notes.sort(key=lambda note: note.get("updated_at", ""), reverse=True)
            self.send_json({"notes": notes, "attachments": attachments,
                            "storage": attachment_storage(t, attachments)})
            return
        attachment_id = self.route_id(r"/api/attachments/([a-f0-9]{16})")
        if attachment_id:
            if not self.require(t, "notebook"):
                return
            self.send_attachment(t, attachment_id)
            return

        # 功能没开的子页连静态文件都不发（前端隐藏不算安全）
        need = PAGE_FEATURES.get(os.path.basename(path))
        if need and not t.has(need):
            self.send_error(404)
            return
        if os.path.basename(path) in ADMIN_PAGES and not t.is_admin:
            self.send_error(404)
            return

        static_path = safe_static_path(path)
        if static_path and os.path.isfile(static_path):
            try:
                with open(static_path, "rb") as f:
                    body = f.read()
            except OSError:
                self.send_error(500)
                return
            ext = os.path.splitext(static_path)[1]
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream"))
            if ext in (".html",):
                self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # 未命中 API 的未知路径交给 index.html，保证后续前端路由可用
        index = os.path.join(WEB_DIR, "index.html")
        if os.path.isfile(index):
            with open(index, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/workbench-crash":
            # 工作台 2.0 崩溃日志收集（免认证路径已在 Caddy 放行；只收文本追加）
            try:
                length = min(int(self.headers.get("Content-Length", "0") or 0), 64 * 1024)
                body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
                stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
                with open(os.path.join(DATA_DIR, "workbench_crash.log"), "a",
                          encoding="utf-8") as f:
                    f.write("\n===== %s =====\n%s\n" % (stamp, body))
                os.chmod(os.path.join(DATA_DIR, "workbench_crash.log"), 0o600)
                self.send_json({"ok": True})
            except Exception as exc:  # noqa: BLE001
                self.send_json({"error": str(exc)}, status=500)
            return
        t = self.begin()
        if t is None:
            return
        if self.handle_finance("POST", t) or self.handle_bookmarks("POST", t):
            return
        if path == "/api/news-blocked":
            if not self.require_admin(t):
                return
            try:
                d = self.read_json()
                news_digest.block_url(d.get("url", ""), d.get("title", ""))
                news_stock.drop(d.get("url", ""))
                self.send_json({"ok": True}, status=201)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
            return
        if path == "/api/news-saved":
            if not self.require(t, "news"):
                return
            try:
                news_saved.save(t, self.read_json())
                self.send_json({"items": news_saved.list_saved(t)}, status=201)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
            return
        if path == "/api/translate":
            # Edge 翻译插件（extension/），烧 Codex 订阅额度，只给站主
            if not self.require_admin(t):
                return
            try:
                self.send_json({"texts": translate.translate(self.read_json().get("texts"))})
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=502)
            return
        upload_note = self.route_id(r"/api/notes/([a-f0-9]{16})/attachments")
        if upload_note:
            if not self.require(t, "notebook"):
                return
            self.upload_attachment(t, upload_note)
            return
        if path != "/api/notes":
            self.send_json({"error": "not found"}, status=404)
            return
        if not self.require(t, "notebook"):
            return
        try:
            data = self.read_json()
            title = str(data.get("title", "")).strip()[:200] or "未命名笔记"
            body = str(data.get("body", ""))
            if len(body) > NOTE_BODY_MAX_CHARS:
                raise ValueError("正文不能超过 100000 字")
            tags = clean_tags(data.get("tags", []))
        except ValueError as exc:
            self.send_json({"error": str(exc)}, status=400)
            return
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        note = {"id": secrets.token_hex(8), "title": title, "body": body, "tags": tags,
                "pinned": False, "archived": False, "created_at": now, "updated_at": now}
        with NOTES_LOCK:
            notes = read_notes(t)
            notes.append(note)
            write_notes(t, notes)
        self.send_json(note, status=201)

    def do_PUT(self):
        """部分更新。title/body/tags 变化才刷新 updated_at；pinned/archived 只改标记。

        带 base_updated_at 时做乐观锁：服务器版本已被别处修改且内容不同则返回 409。
        """
        t = self.begin()
        if t is None:
            return
        if self.handle_finance("PUT", t) or self.handle_bookmarks("PUT", t):
            return
        note_id = self.note_id()
        if not note_id:
            self.send_json({"error": "not found"}, status=404)
            return
        if not self.require(t, "notebook"):
            return
        try:
            data = self.read_json()
            changes = {}
            if "title" in data:
                changes["title"] = str(data["title"]).strip()[:200] or "未命名笔记"
            if "body" in data:
                changes["body"] = str(data["body"])
                if len(changes["body"]) > NOTE_BODY_MAX_CHARS:
                    raise ValueError("正文不能超过 100000 字")
            if "tags" in data:
                changes["tags"] = clean_tags(data["tags"])
            flags = {key: bool(data[key]) for key in ("pinned", "archived") if key in data}
            if not changes and not flags:
                raise ValueError("没有可更新的字段")
        except ValueError as exc:
            self.send_json({"error": str(exc)}, status=400)
            return
        with NOTES_LOCK:
            notes = read_notes(t)
            note = next((item for item in notes if item.get("id") == note_id), None)
            if not note:
                self.send_json({"error": "笔记不存在"}, status=404)
                return
            changed = any(note.get(key) != value for key, value in changes.items())
            base = data.get("base_updated_at")
            if changed and base and base != note.get("updated_at"):
                self.send_json({"error": "这篇笔记已在其他页面或设备上修改", "note": note}, status=409)
                return
            if changed:
                note.update(changes)
                note["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            note.update(flags)
            if changed or flags:
                write_notes(t, notes)
        self.send_json(note)

    def do_DELETE(self):
        t = self.begin()
        if t is None:
            return
        if self.handle_finance("DELETE", t) or self.handle_bookmarks("DELETE", t):
            return
        if urlparse(self.path).path == "/api/news-saved":
            if not self.require(t, "news"):
                return
            q = parse_qs(urlparse(self.path).query)
            removed = news_saved.unsave(t, (q.get("url") or [""])[0])
            self.send_json({"removed": removed, "items": news_saved.list_saved(t)})
            return
        if urlparse(self.path).path == "/api/news-blocked":
            if not self.require_admin(t):
                return
            q = parse_qs(urlparse(self.path).query)
            removed = news_digest.unblock_url((q.get("url") or [""])[0])
            self.send_json({"removed": removed})
            return
        attachment_id = self.route_id(r"/api/attachments/([a-f0-9]{16})")
        if attachment_id:
            if not self.require(t, "notebook"):
                return
            with ATTACH_LOCK:
                attachments = read_attachments(t)
                kept = [a for a in attachments if a.get("id") != attachment_id]
                if len(kept) == len(attachments):
                    self.send_json({"error": "附件不存在"}, status=404)
                    return
                write_private_json(t.attach_meta, kept)
                remove_attachment_file(t, attachment_id)
            self.send_json({"deleted": True, "storage": attachment_storage(t, kept)})
            return
        note_id = self.note_id()
        if not note_id:
            self.send_json({"error": "not found"}, status=404)
            return
        if not self.require(t, "notebook"):
            return
        with NOTES_LOCK:
            notes = read_notes(t)
            kept = [item for item in notes if item.get("id") != note_id]
            if len(kept) == len(notes):
                self.send_json({"error": "笔记不存在"}, status=404)
                return
            write_notes(t, kept)
            with ATTACH_LOCK:
                attachments = read_attachments(t)
                remaining = [a for a in attachments if a.get("note_id") != note_id]
                if len(remaining) != len(attachments):
                    write_private_json(t.attach_meta, remaining)
                    for item in attachments:
                        if item.get("note_id") == note_id:
                            remove_attachment_file(t, item["id"])
        self.send_json({"deleted": True, "storage": attachment_storage(t, remaining)})

def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(USERS_DATA_DIR, mode=0o700, exist_ok=True)
    os.makedirs(USERS_CONFIG_DIR, mode=0o700, exist_ok=True)
    for t in iter_tenants():
        t.ensure_dirs()
    print("home-portal tenants:", ", ".join(t.uid for t in iter_tenants()) or "(none)",
          flush=True)
    poller = threading.Thread(target=mail_poller, name="mail-poller", daemon=True)
    poller.start()
    canvas_thread = threading.Thread(target=canvas_poller, name="canvas-poller", daemon=True)
    canvas_thread.start()
    threading.Thread(target=news_stock.poller, name="news-poller", daemon=True).start()
    threading.Thread(target=news_stock.tldr_poller, name="news-tldr", daemon=True).start()
    threading.Thread(target=read_sync_poller, name="mail-read-sync", daemon=True).start()
    threading.Thread(target=finance.fx_poller, args=(FX_DATA,), name="fx-poller", daemon=True).start()
    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"home-portal listening on http://{LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
