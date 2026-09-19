"""Canvas LMS 任务抓取（学校地址由 config/canvas.yml 的 base_url 指定）。

用「个人访问令牌」(Personal Access Token) 调官方 REST API，
不经过学校统一身份认证登录。仅使用标准库。

对外主接口：
  sync_once(config_path, data_path) -> dict  # 立即抓取并写缓存，返回负载
  read_cached(data_path) -> dict             # 读上次缓存（无则空负载）

归一化后的任务结构（供前端与本地任务合并）：
  {
    "id": "oc-12345",                 # 稳定唯一 id
    "source": "canvas",
    "title": "作业标题",
    "course": "课程名",
    "url": "https://oc.../assignments/...",
    "due_at": "2026-09-10T23:59:00+08:00" | null,
    "due_local_date": "2026-09-10",
    "kind": "todo|missing|planner",
    "plannable_type": "assignment|planner_note|...",
    "points": 10.0 | null,
    "submitted": false
  }
"""

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

CANVAS_TIMEOUT = 20
PER_PAGE = 100


# ---------------------------------------------------------------- config

def parse_config(path):
    """读取极简 key: value 配置。无文件或无 token 返回 {}。"""
    cfg = {}
    if not os.path.exists(path):
        return cfg
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, _, value = line.partition(":")
            cfg[key.strip()] = value.strip()
    if not cfg.get("token") or cfg["token"].startswith("在此粘贴"):
        return {}
    if not cfg.get("base_url"):
        return {}
    cfg["base_url"] = cfg["base_url"].rstrip("/")
    def on(k, default):
        v = cfg.get(k, default)
        return str(v).lower() != "false" if v is not None else False
    cfg["fetch_todo"] = on("fetch_todo", "true")
    cfg["fetch_planner"] = on("fetch_planner", "true")
    cfg["fetch_missing"] = on("fetch_missing", "true")
    cfg["fetch_conversations"] = on("fetch_conversations", "true")
    cfg["fetch_assignments"] = on("fetch_assignments", "true")
    try:
        cfg["planner_days"] = int(cfg.get("planner_days", "400"))
    except ValueError:
        cfg["planner_days"] = 400
    try:
        cfg["planner_back_days"] = int(cfg.get("planner_back_days", "400"))
    except ValueError:
        cfg["planner_back_days"] = 400
    return cfg


# ---------------------------------------------------------------- http

class CanvasError(RuntimeError):
    pass


def api_get(base_url, token, path, params=None):
    url = base_url + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "User-Agent": "home-portal/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=CANVAS_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise CanvasError(f"HTTP {e.code}: {body}") from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        raise CanvasError(f"{type(e).__name__}: {e}") from e


def api_get_paged(base_url, token, path, params=None, max_pages=10):
    """跟随 Link 头翻页，返回累计的全部记录。"""
    url = base_url + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    out = []
    for _ in range(max_pages):
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "User-Agent": "home-portal/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=CANVAS_TIMEOUT) as resp:
                out.extend(json.loads(resp.read().decode("utf-8")))
                link = resp.getheader("Link") or ""
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:200]
            raise CanvasError(f"HTTP {e.code}: {body}") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            raise CanvasError(f"{type(e).__name__}: {e}") from e
        nxt = None
        for part in link.split(","):
            if 'rel="next"' in part:
                s = part.find("<")
                e2 = part.find(">")
                if s != -1 and e2 != -1:
                    nxt = part[s + 1:e2]
        if not nxt:
            break
        url = nxt
    return out


# ---------------------------------------------------------------- helpers

def local_date(iso):
    """ISO8601 -> 本地时区 YYYY-MM-DD；空/异常返回 None。"""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().date().isoformat()
    except ValueError:
        return None


def normalize(base_url, item_id, title, url, due_at, kind, course,
              points=None, plannable_type=None, submitted=False):
    if url and url.startswith("/"):
        url = base_url + url
    return {
        "id": "oc-" + str(item_id),
        "source": "canvas",
        "title": title or "(未命名)",
        "course": course or "",
        "url": url or "",
        "due_at": due_at,
        "due_local_date": local_date(due_at),
        "kind": kind,
        "plannable_type": plannable_type,
        "points": points,
        "submitted": bool(submitted),
    }


def course_map(base_url, token):
    """返回 {课程 id: 课程名}，用于补全上下文名。失败返回空表。"""
    out = {}
    try:
        data = api_get(base_url, token, "/api/v1/courses",
                       {"per_page": PER_PAGE, "enrollment_state": "active"})
        for c in data:
            if c.get("id") and c.get("name"):
                out[c["id"]] = c["name"]
    except CanvasError:
        pass
    return out


def all_course_map(base_url, token):
    """所有学期课程（含已结束，排除无效），{id: name}。"""
    out = {}
    try:
        data = api_get_paged(base_url, token, "/api/v1/courses",
                             {"per_page": PER_PAGE})
        for c in data:
            if c.get("id") and c.get("name"):
                out[c["id"]] = c["name"]
    except CanvasError:
        pass
    return out


def context_course_name(context_code, courses):
    """context_code 形如 'course_12345'。"""
    if not context_code:
        return ""
    if context_code.startswith("course_"):
        try:
            return courses.get(int(context_code[7:]), "")
        except ValueError:
            return ""
    return ""


# ---------------------------------------------------------------- fetch

def fetch_tasks(cfg):
    base_url, token = cfg["base_url"], cfg["token"]
    tasks = []
    seen = set()
    assignment_content = set()
    errors = []

    def add(t):
        if t["id"] in seen:
            return
        seen.add(t["id"])
        tasks.append(t)

    courses = course_map(base_url, token)

    # 0) 所有学期课程的全部作业（tasks 页“所有时期任务”数据源）
    if cfg["fetch_assignments"]:
        all_courses = all_course_map(base_url, token)
        for cid, cname in all_courses.items():
            try:
                rows = api_get_paged(base_url, token,
                    f"/api/v1/courses/{cid}/assignments",
                    {"per_page": PER_PAGE, "order_by": "due_at",
                     "include[]": ["submission"]})
            except CanvasError as e:
                errors.append(f"assignments {cid}: " + str(e))
                continue
            for a in rows:
                aid = a.get("id")
                if aid is None:
                    continue
                sub = a.get("submission") or {}
                wf = sub.get("workflow_state")
                submitted = wf in ("submitted", "graded", "pending_review")
                task = normalize(
                    base_url,
                    "asg-" + str(aid),
                    a.get("name") or "(未命名作业)",
                    a.get("html_url"),
                    a.get("due_at"),
                    "assignment",
                    cname,
                    points=a.get("points_possible"),
                    plannable_type="assignment",
                    submitted=bool(submitted),
                )
                add(task)
                if task["url"]:
                    assignment_content.add((task["url"], task["title"].strip()))

    # 1) 待办：待提交作业 / 待查看反馈
    if cfg["fetch_todo"]:
        try:
            data = api_get(base_url, token, "/api/v1/users/self/todo",
                           {"per_page": PER_PAGE})
            for row in data:
                a = row.get("assignment") or {}
                assignment_id = a.get("id") or row.get("assignment_id")
                needs = row.get("needs_grading")
                if assignment_id is None:
                    continue
                course = row.get("context_name") or context_course_name(
                    row.get("context_code"), courses)
                title = a.get("name") or row.get("title") or ""
                if needs:
                    title = "【有评分反馈】" + title
                task = normalize(
                    base_url,
                    "todo-" + str(assignment_id),
                    title,
                    a.get("html_url") or row.get("html_url"),
                    a.get("due_at"),
                    "todo",
                    course,
                    points=a.get("points_possible"),
                )
                # assignments 端点已收录链接与标题相同的作业时，不重复展示待办。
                if task["url"] and (task["url"], task["title"].strip()) in assignment_content:
                    continue
                add(task)
        except CanvasError as e:
            errors.append("todo: " + str(e))

    # 2) Planner 日程（前后窗口由配置控制，覆盖跨学期日程）
    if cfg["fetch_planner"]:
        try:
            now = datetime.now(timezone.utc).astimezone()
            start = (now - timedelta(days=cfg["planner_back_days"])).date().isoformat()
            end = (now + timedelta(days=cfg["planner_days"])).date().isoformat()
            data = api_get(base_url, token, "/api/v1/planner/items", {
                "start_date": start,
                "end_date": end,
                "per_page": PER_PAGE,
                "order": "asc",
            })
            for row in data:
                ptype = row.get("plannable_type") or ""
                p = row.get("plannable") or {}
                pid = row.get("plannable_id") or p.get("id")
                if pid is None:
                    continue
                # 全课程 assignments 端点已收录同一作业，避免 Planner 重复展示。
                if ptype == "assignment" and "oc-asg-" + str(pid) in seen:
                    continue
                submissions = row.get("submissions") or {}
                submitted = bool(submissions.get("submitted"))
                due = (p.get("due_at") or row.get("plannable_date")
                       or p.get("todo_date") or p.get("event_start_at"))
                title = (p.get("title") or p.get("name")
                         or {"planner_note": "备忘", "announcement": "公告"}.get(ptype, ptype))
                course = row.get("context_name") or context_course_name(
                    row.get("context_code"), courses)
                url = row.get("html_url") or p.get("html_url") or p.get("url")
                if url and url.startswith("/"):
                    url = base_url + url
                if ptype == "planner_note" and not url:
                    url = base_url + "/#view_name=planner"
                add(normalize(
                    base_url,
                    "planner-" + ptype + "-" + str(pid),
                    title, url, due, "planner", course,
                    points=p.get("points_possible"),
                    plannable_type=ptype,
                    submitted=submitted,
                ))
        except CanvasError as e:
            errors.append("planner: " + str(e))

    # 3) 逾期未交
    if cfg["fetch_missing"]:
        try:
            data = api_get(base_url, token,
                           "/api/v1/users/self/missing_submissions",
                           {"per_page": PER_PAGE,
                            "include[]": ["course", "planner_overrides"]})
            for row in data:
                assignment_id = row.get("assignment_id") or row.get("id")
                if assignment_id is None:
                    continue
                course = (row.get("course_name")
                          or courses.get(row.get("course_id"), ""))
                add(normalize(
                    base_url,
                    "missing-" + str(assignment_id),
                    row.get("title") or row.get("assignment_name") or "逾期作业",
                    row.get("html_url") or row.get("submission_html_url"),
                    row.get("due_at"),
                    "missing",
                    course,
                    points=row.get("points_possible"),
                ))
        except CanvasError as e:
            errors.append("missing: " + str(e))

    tasks.sort(key=lambda t: (t["due_at"] is None, t["due_at"] or "", t["title"]))
    return tasks, errors


def fetch_conversations(cfg):
    """Canvas 站内信收件箱（对应网页 conversations#filter=type=inbox）。

    返回 (messages, errors)。每条含标题、课程、发件人、摘要、未读标记、直达链接。
    """
    base_url, token = cfg["base_url"], cfg["token"]
    messages, errors = [], []
    try:
        data = api_get_paged(base_url, token, "/api/v1/conversations", {
            "scope": "inbox",
            "per_page": PER_PAGE,
        })
        for row in data:
            cid = row.get("id")
            if cid is None:
                continue
            audience = set(row.get("audience") or [])
            senders = [p.get("name") or p.get("full_name") or ""
                       for p in (row.get("participants") or [])
                       if p.get("id") in audience]
            messages.append({
                "id": "oc-msg-" + str(cid),
                "subject": row.get("subject") or "(无主题)",
                "course": row.get("context_name") or "",
                "sender": "、".join(x for x in senders if x),
                "last_message": (row.get("last_message") or "")[:400],
                "last_message_at": row.get("last_message_at"),
                "unread": str(row.get("workflow_state", "")).lower() == "unread",
                "message_count": row.get("message_count") or 0,
                "url": base_url + "/conversations/" + str(cid),
            })
        messages.sort(key=lambda m: m.get("last_message_at") or "", reverse=True)
    except CanvasError as e:
        errors.append("conversations: " + str(e))
    return messages, errors


# ---------------------------------------------------------------- cache

def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def write_data(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def sync_once(config_path, data_path):
    cfg = parse_config(config_path)
    if not cfg:
        payload = {"updated": None, "configured": False, "tasks": [],
                   "messages": [], "errors": []}
        write_data(data_path, payload)
        return payload
    try:
        tasks, errors = fetch_tasks(cfg)
        messages, msg_errors = fetch_conversations(cfg) if cfg["fetch_conversations"] else ([], [])
        payload = {
            "updated": _now_iso(),
            "configured": True,
            "base_url": cfg["base_url"],
            "tasks": tasks,
            "messages": messages,
            "errors": errors + msg_errors,
        }
    except CanvasError as e:
        payload = {
            "updated": _now_iso(),
            "configured": True,
            "tasks": [],
            "messages": [],
            "errors": ["fatal: " + str(e)],
        }
    write_data(data_path, payload)
    return payload


def read_cached(data_path):
    if not os.path.exists(data_path):
        return {"updated": None, "configured": False, "tasks": [],
                "messages": [], "errors": []}
    try:
        with open(data_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
            payload.setdefault("messages", [])
            payload.setdefault("tasks", [])
            payload.setdefault("errors", [])
            return payload
    except (OSError, json.JSONDecodeError):
        return {"updated": None, "configured": False, "tasks": [],
                "messages": [], "errors": []}
