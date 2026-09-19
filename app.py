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
  6. /api/health。

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

import bookmarks
import canvas_sync
import finance
import llm_triage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
CONFIG_DIR = os.path.join(BASE_DIR, "config")
DATA_DIR = os.path.join(BASE_DIR, "data")
MAIL_CONFIG = os.path.join(CONFIG_DIR, "mail.yml")
MAIL_DATA = os.path.join(DATA_DIR, "mail.json")
LLM_CONFIG = os.path.join(CONFIG_DIR, "llm.yml")
CANVAS_CONFIG = os.path.join(CONFIG_DIR, "canvas.yml")
CANVAS_DATA = os.path.join(DATA_DIR, "canvas.json")
NOTES_DATA = os.path.join(DATA_DIR, "notes.json")
ATTACH_DATA = os.path.join(DATA_DIR, "attachments.json")
ATTACH_DIR = os.path.join(DATA_DIR, "attachments")
NOTE_BODY_MAX_CHARS = 100_000
BOOKMARKS = bookmarks.Store(os.path.join(DATA_DIR, "bookmarks.json"))
FINANCE = finance.Store(os.path.join(DATA_DIR, "finance.json"), os.path.join(DATA_DIR, "fx.json"))

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
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

MAIL_LOCK = threading.Lock()
NOTES_LOCK = threading.Lock()
ATTACH_LOCK = threading.Lock()  # 加锁顺序：NOTES_LOCK -> ATTACH_LOCK


def read_notes():
    try:
        with open(NOTES_DATA, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [normalize_note(n) for n in data if isinstance(n, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def write_notes(notes):
    write_private_json(NOTES_DATA, notes)


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

def parse_notebook_config():
    cfg = {"quota_mb": 1024, "max_file_mb": 25, "min_free_mb": 2048}
    try:
        with open(os.path.join(CONFIG_DIR, "notebook.yml"), "r", encoding="utf-8") as f:
            for raw_line in f:
                key, sep, value = raw_line.split("#", 1)[0].partition(":")
                if sep and key.strip() in cfg and value.strip().isdigit():
                    cfg[key.strip()] = int(value.strip())
    except OSError:
        pass
    return {"quota": cfg["quota_mb"] * 1024 * 1024,
            "max_file": cfg["max_file_mb"] * 1024 * 1024,
            "min_free": cfg["min_free_mb"] * 1024 * 1024}


def read_attachments():
    try:
        with open(ATTACH_DATA, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def attachment_storage(attachments):
    cfg = parse_notebook_config()
    return {"used": sum(int(a.get("size", 0)) for a in attachments),
            "quota": cfg["quota"], "max_file": cfg["max_file"]}


def remove_attachment_file(attachment_id):
    try:
        os.remove(os.path.join(ATTACH_DIR, attachment_id))
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


def write_mail_data(payload):
    tmp = MAIL_DATA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MAIL_DATA)


def read_mail_data():
    if not os.path.exists(MAIL_DATA):
        return {"updated": None, "accounts": []}
    try:
        with open(MAIL_DATA, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"updated": None, "accounts": []}


def sync_read_state():
    """auto_remove_read 账号：只查当前未读 UID，把已读（或已删除）的邮件从主页数据中移除。"""
    accounts_cfg = {a["email"]: a for a in parse_simple_mail_config(MAIL_CONFIG)
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
        data = read_mail_data()
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
            write_mail_data(data)


READ_SYNC_LOCK = threading.Lock()
READ_SYNC_LAST = [0.0]


def sync_read_state_throttled(min_interval=5):
    """主页“立即刷新”/切回页面时调用；并发或 5 秒内重复的请求直接跳过。"""
    if not READ_SYNC_LOCK.acquire(blocking=False):
        return
    try:
        if time.monotonic() - READ_SYNC_LAST[0] >= min_interval:
            READ_SYNC_LAST[0] = time.monotonic()
            sync_read_state()
    finally:
        READ_SYNC_LOCK.release()


def read_sync_poller():
    time.sleep(15)
    while True:
        try:
            sync_read_state_throttled(min_interval=0)
        except Exception as exc:  # noqa: BLE001
            print("mail read-sync error:", exc, flush=True)
        time.sleep(MAIL_READ_SYNC_SECONDS)


def mail_poller():
    # 启动后稍等再拉，避免与服务启动抢资源
    time.sleep(5)
    triage_cache = llm_triage.load_cache()
    while True:
        accounts_cfg = parse_simple_mail_config(MAIL_CONFIG)
        llm_cfg = parse_llm_config(LLM_CONFIG)
        if accounts_cfg:
            results = [fetch_account_mail(a, llm_cfg, triage_cache)
                       for a in accounts_cfg]
            with MAIL_LOCK:
                write_mail_data({
                    "updated": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                    "configured": len(accounts_cfg),
                    "triage": "llm" if llm_cfg else "rules",
                    "accounts": results,
                })
        else:
            with MAIL_LOCK:
                write_mail_data({"updated": None, "configured": 0,
                                 "triage": "llm" if llm_cfg else "rules",
                                 "accounts": []})
        # 配置可能随时补凭证，每轮都重新读取
        time.sleep(MAIL_POLL_SECONDS)


# ---------------------------------------------------------------- canvas

CANVAS_LOCK = threading.Lock()


def canvas_poller():
    time.sleep(8)
    while True:
        with CANVAS_LOCK:
            try:
                canvas_sync.sync_once(CANVAS_CONFIG, CANVAS_DATA)
            except Exception as exc:  # noqa: BLE001
                print("canvas sync error:", exc, flush=True)
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

    def route_id(self, pattern):
        match = re.fullmatch(pattern, urlparse(self.path).path)
        return match.group(1) if match else None

    def send_private_asset(self, name, example, as_script=False):
        """config/<name> 存在时用它，否则用 web/assets/<example>。site.json 包成 window.PORTAL_SITE 脚本。"""
        path = os.path.join(CONFIG_DIR, name)
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

    def handle_bookmarks(self, method):
        """/api/bookmarks 路由；命中返回 True。"""
        if not urlparse(self.path).path.startswith("/api/bookmarks"):
            return False
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

    def handle_finance(self, method):
        """/api/finance 路由；命中返回 True。"""
        path = urlparse(self.path).path
        if not path.startswith("/api/finance"):
            return False
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

    def send_attachment(self, attachment_id):
        with ATTACH_LOCK:
            meta = next((a for a in read_attachments() if a.get("id") == attachment_id), None)
        path = os.path.join(ATTACH_DIR, attachment_id)
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

    def upload_attachment(self, note_id):
        cfg = parse_notebook_config()
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
            exists = any(n.get("id") == note_id for n in read_notes())
        if not exists:
            reject("笔记不存在", 404)
            return
        with ATTACH_LOCK:
            used = attachment_storage(read_attachments())["used"]
        if used + length > cfg["quota"]:
            reject("附件空间不足（配额 %d MB）" % (cfg["quota"] // 1048576), 413)
            return

        os.makedirs(ATTACH_DIR, mode=0o700, exist_ok=True)
        attachment_id = secrets.token_hex(8)
        tmp = os.path.join(ATTACH_DIR, ".upload-" + attachment_id)
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
                if not any(n.get("id") == note_id for n in read_notes()):
                    raise LookupError("笔记不存在")
                attachments = read_attachments()
                storage = attachment_storage(attachments)
                if storage["used"] + length > storage["quota"]:
                    raise LookupError("附件空间不足")
                if disk_stats(DATA_DIR)["available"] < cfg["min_free"]:
                    raise LookupError("服务器磁盘剩余空间不足，已拒绝写入")
                os.replace(tmp, os.path.join(ATTACH_DIR, attachment_id))
                meta = {"id": attachment_id, "note_id": note_id, "name": name, "mime": mime,
                        "size": length, "url": "/api/attachments/" + attachment_id,
                        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                attachments.append(meta)
                write_private_json(ATTACH_DATA, attachments)
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
        if self.handle_finance("GET") or self.handle_bookmarks("GET"):
            return
        if path == "/assets/site-config.js":
            self.send_private_asset("site.json", "site.example.json", as_script=True)
            return
        if path == "/assets/schedule.json":
            self.send_private_asset("schedule.json", "schedule.example.json")
            return
        if path == "/api/health":
            self.send_json({"status": "ok"})
            return
        if path == "/api/server-stats":
            try:
                self.send_json(server_stats())
            except Exception as exc:  # noqa: BLE001
                self.send_json({"status": "error", "error": str(exc)}, status=500)
            return
        if path == "/api/mail":
            if parse_qs(urlparse(self.path).query).get("sync"):
                sync_read_state_throttled()
            with MAIL_LOCK:
                self.send_json(read_mail_data())
            return
        if path == "/api/oc-tasks":
            if parse_qs(urlparse(self.path).query).get("refresh"):
                with CANVAS_LOCK:
                    try:
                        self.send_json(
                            canvas_sync.sync_once(CANVAS_CONFIG, CANVAS_DATA))
                    except Exception as exc:  # noqa: BLE001
                        self.send_json({"configured": True, "tasks": [], "messages": [],
                                        "errors": [str(exc)]}, status=502)
                return
            with CANVAS_LOCK:
                self.send_json(canvas_sync.read_cached(CANVAS_DATA))
            return
        if path == "/api/notes":
            with NOTES_LOCK:
                notes = read_notes()
            with ATTACH_LOCK:
                attachments = read_attachments()
            notes.sort(key=lambda note: note.get("updated_at", ""), reverse=True)
            self.send_json({"notes": notes, "attachments": attachments,
                            "storage": attachment_storage(attachments)})
            return
        attachment_id = self.route_id(r"/api/attachments/([a-f0-9]{16})")
        if attachment_id:
            self.send_attachment(attachment_id)
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
        if self.handle_finance("POST") or self.handle_bookmarks("POST"):
            return
        upload_note = self.route_id(r"/api/notes/([a-f0-9]{16})/attachments")
        if upload_note:
            self.upload_attachment(upload_note)
            return
        if urlparse(self.path).path != "/api/notes":
            self.send_json({"error": "not found"}, status=404)
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
            notes = read_notes()
            notes.append(note)
            write_notes(notes)
        self.send_json(note, status=201)

    def do_PUT(self):
        """部分更新。title/body/tags 变化才刷新 updated_at；pinned/archived 只改标记。

        带 base_updated_at 时做乐观锁：服务器版本已被别处修改且内容不同则返回 409。
        """
        if self.handle_finance("PUT") or self.handle_bookmarks("PUT"):
            return
        note_id = self.note_id()
        if not note_id:
            self.send_json({"error": "not found"}, status=404)
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
            notes = read_notes()
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
                write_notes(notes)
        self.send_json(note)

    def do_DELETE(self):
        if self.handle_finance("DELETE") or self.handle_bookmarks("DELETE"):
            return
        attachment_id = self.route_id(r"/api/attachments/([a-f0-9]{16})")
        if attachment_id:
            with ATTACH_LOCK:
                attachments = read_attachments()
                kept = [a for a in attachments if a.get("id") != attachment_id]
                if len(kept) == len(attachments):
                    self.send_json({"error": "附件不存在"}, status=404)
                    return
                write_private_json(ATTACH_DATA, kept)
                remove_attachment_file(attachment_id)
            self.send_json({"deleted": True, "storage": attachment_storage(kept)})
            return
        note_id = self.note_id()
        if not note_id:
            self.send_json({"error": "not found"}, status=404)
            return
        with NOTES_LOCK:
            notes = read_notes()
            kept = [item for item in notes if item.get("id") != note_id]
            if len(kept) == len(notes):
                self.send_json({"error": "笔记不存在"}, status=404)
                return
            write_notes(kept)
            with ATTACH_LOCK:
                attachments = read_attachments()
                remaining = [a for a in attachments if a.get("note_id") != note_id]
                if len(remaining) != len(attachments):
                    write_private_json(ATTACH_DATA, remaining)
                    for item in attachments:
                        if item.get("note_id") == note_id:
                            remove_attachment_file(item["id"])
        self.send_json({"deleted": True, "storage": attachment_storage(remaining)})


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    poller = threading.Thread(target=mail_poller, name="mail-poller", daemon=True)
    poller.start()
    canvas_thread = threading.Thread(target=canvas_poller, name="canvas-poller", daemon=True)
    canvas_thread.start()
    threading.Thread(target=read_sync_poller, name="mail-read-sync", daemon=True).start()
    threading.Thread(target=finance.fx_poller, args=(FINANCE.fx_path,), name="fx-poller", daemon=True).start()
    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"home-portal listening on http://{LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
