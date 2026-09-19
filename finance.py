"""Finance：订阅与记账（收入/支出），CNY/USD/GBP/HKD 按实时汇率折算人民币。仅使用标准库。

数据 data/finance.json（600）：
  {"subscriptions": [...], "transactions": [...]}
汇率缓存 data/fx.json：
  {"rates": {"USD": 0.14, ...}, "updated": ISO, "source": "...", "history": {"2026-03-01": {"USD": 0.14}}}
  rates 为「1 CNY 可换多少外币」；折算人民币：amount / rates[cur]。

订阅字段：
  id, name, amount, currency, cycle(day|week|month|year), interval, start_date,
  category, account, url, note, active, auto_log, log_from, logged_through,
  price_history[{date, amount, currency}], created_at, updated_at；读取时附带计算字段 next_date。
  auto_log 为真时，log_from 起（含）到今天的每次扣款自动生成一条支出流水（sub_id 关联），
  logged_through 记录已生成到的扣款日，删掉生成的流水不会再补。

流水字段：
  id, type(income|expense), date, amount, currency, rate(1 外币 = ? CNY), cny, category, account,
  note, sub_id, created_at, updated_at。rate 按流水日期取汇率（历史日期查 fawazahmed0 历史快照），
  保存后固定，不随日后汇率变化。
"""

import json
import os
import re
import secrets
import threading
import time
import urllib.request
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone

FX_TIMEOUT = 10
FX_REFRESH_SECONDS = 6 * 3600
FX_RETRY_SECONDS = 600
FX_MANUAL_MIN_INTERVAL = 60
PRIMARY_URL = "https://open.er-api.com/v6/latest/CNY"
# fawazahmed0/exchange-api：主源失败时的备用源，并按日期提供历史快照；jsDelivr 失败时用 Cloudflare 镜像
FALLBACK_URLS = (
    "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{tag}/v1/currencies/cny.min.json",
    "https://{tag}.currency-api.pages.dev/v1/currencies/cny.min.json",
)

CURRENCIES = ("CNY", "USD", "GBP", "HKD")  # 需要新币种时加在这里，前端同步修改 finance.html
CYCLES = ("day", "week", "month", "year")
TYPES = ("income", "expense")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
MAX_AMOUNT = 1e12

LOCK = threading.Lock()   # 保护 finance.json
FX_LOCK = threading.Lock()  # 保护 fx.json
_last_manual_refresh = 0.0


class FinanceError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def today():
    return datetime.now().date()  # 按服务器本地时区


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def write_private_json(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        os.chmod(tmp, 0o600)
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------- 汇率

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "home-portal/1.0"})
    with urllib.request.urlopen(req, timeout=FX_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_fallback(tag):
    """返回 {币种大写: 1 CNY 可换数量}；tag 为 latest 或 YYYY-MM-DD。"""
    last_error = None
    for template in FALLBACK_URLS:
        try:
            data = fetch_json(template.format(tag=tag))
            rates = {k.upper(): float(v) for k, v in (data.get("cny") or {}).items()
                     if k.upper() in CURRENCIES and isinstance(v, (int, float)) and v > 0}
            if len(rates) == len(CURRENCIES):
                return rates, data.get("date")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise RuntimeError("fallback 汇率源不可用：%s" % last_error)


def refresh_rates(fx_path):
    """主源 open.er-api.com，失败时用 fawazahmed0。"""
    try:
        data = fetch_json(PRIMARY_URL)
        if data.get("result") != "success":
            raise RuntimeError(data.get("error-type") or "未知错误")
        rates = {cur: float(data["rates"][cur]) for cur in CURRENCIES}
        source = "open.er-api.com"
        updated = datetime.fromtimestamp(int(data["time_last_update_unix"]), timezone.utc).isoformat()
    except Exception as exc:  # noqa: BLE001
        print("fx primary error:", exc, flush=True)
        rates, updated = fetch_fallback("latest")  # 两个源都失败时抛出，保留旧缓存
        source = "fawazahmed0/exchange-api"
    rates["CNY"] = 1.0
    with FX_LOCK:
        fx = read_json(fx_path, {})
        fx.update({"rates": rates, "updated": updated, "fetched_at": now_iso(), "source": source})
        write_private_json(fx_path, fx)
    return fx


def read_fx(fx_path):
    with FX_LOCK:
        fx = read_json(fx_path, {})
    fx.setdefault("rates", {"CNY": 1.0})
    fx.pop("history", None)
    return fx


def refresh_rates_throttled(fx_path):
    global _last_manual_refresh
    if time.time() - _last_manual_refresh < FX_MANUAL_MIN_INTERVAL:
        return read_fx(fx_path)
    _last_manual_refresh = time.time()
    refresh_rates(fx_path)
    return read_fx(fx_path)


def fx_poller(fx_path):
    time.sleep(5)
    while True:
        try:
            refresh_rates(fx_path)
            delay = FX_REFRESH_SECONDS
        except Exception as exc:  # noqa: BLE001
            print("fx refresh error:", exc, flush=True)
            delay = FX_RETRY_SECONDS
        time.sleep(delay)


def cny_rate(fx_path, currency, on_date, allow_fetch=True):
    """1 单位 currency 折合多少 CNY。近两天用最新汇率；更早的日期查历史快照（allow_fetch 为假时只查缓存），
    查不到退回最新。"""
    if currency == "CNY":
        return 1.0
    if on_date < today() - timedelta(days=2):
        key = on_date.isoformat()
        with FX_LOCK:
            cached = read_json(fx_path, {}).get("history", {}).get(key, {}).get(currency)
        if cached:
            return 1.0 / cached
        try:
            if not allow_fetch:
                raise RuntimeError("本次补记的历史汇率查询次数已用完")
            rates, _ = fetch_fallback(key)
            if rates.get(currency):
                with FX_LOCK:
                    fx = read_json(fx_path, {})
                    fx.setdefault("history", {}).setdefault(key, {})[currency] = rates[currency]
                    write_private_json(fx_path, fx)
                return 1.0 / rates[currency]
        except Exception as exc:  # noqa: BLE001
            print("fx history error:", key, currency, exc, flush=True)
    per_cny = read_fx(fx_path)["rates"].get(currency)
    if not per_cny:
        raise FinanceError("暂时没有 %s 的汇率，请稍后再试" % currency, 503)
    return 1.0 / per_cny


# ---------------------------------------------------------------- 周期计算

def parse_date(value, field):
    value = str(value or "").strip()
    try:
        if not DATE_RE.fullmatch(value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError as exc:
        raise FinanceError("%s格式应为 YYYY-MM-DD" % field) from exc


def add_months(start, months):
    y, m = divmod(start.month - 1 + months, 12)
    y += start.year
    return date(y, m + 1, min(start.day, monthrange(y, m + 1)[1]))


def charge_date(sub, n):
    """第 n 次扣款日（n=0 为 start_date）。按月/年始终从起始日推算，1 月 31 日起的月付在 2 月落到月末。"""
    start = date.fromisoformat(sub["start_date"])
    step = n * sub["interval"]
    if sub["cycle"] == "day":
        return start + timedelta(days=step)
    if sub["cycle"] == "week":
        return start + timedelta(weeks=step)
    if sub["cycle"] == "month":
        return add_months(start, step)
    return add_months(start, 12 * step)


def first_charge_index_on_or_after(sub, target):
    start = date.fromisoformat(sub["start_date"])
    if target <= start:
        return 0
    # 按每期最长天数估算下界，再逐次推进，保证不会跳过
    days_per = {"day": 1, "week": 7, "month": 31, "year": 366}[sub["cycle"]] * sub["interval"]
    n = max(0, (target - start).days // days_per - 1)
    while charge_date(sub, n) < target:
        n += 1
    return n


def next_charge(sub, on_or_after=None):
    if not sub.get("active"):
        return None
    return charge_date(sub, first_charge_index_on_or_after(sub, on_or_after or today()))


# ---------------------------------------------------------------- 校验

def clean_text(value, limit):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def clean_amount(value):
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise FinanceError("金额必须是数字") from exc
    if not (0 < amount < MAX_AMOUNT):
        raise FinanceError("金额必须大于 0")
    return round(amount, 8)


def clean_currency(value):
    cur = str(value or "CNY").strip().upper()
    if cur not in CURRENCIES:
        raise FinanceError("币种只支持 %s" % " / ".join(CURRENCIES))
    return cur


def clean_url(value):
    url = str(value or "").strip()[:500]
    if url and not re.match(r"https?://", url, re.I):
        url = "https://" + url
    return url


def subscription_fields(data, fx_path, partial=False):
    out = {}

    def has(key):
        return key in data or not partial

    if has("name"):
        out["name"] = clean_text(data.get("name"), 80)
        if not out["name"]:
            raise FinanceError("请填写订阅名称")
    if has("amount"):
        out["amount"] = clean_amount(data.get("amount"))
    if has("currency"):
        out["currency"] = clean_currency(data.get("currency"))
    if has("cycle"):
        out["cycle"] = str(data.get("cycle") or "month")
        if out["cycle"] not in CYCLES:
            raise FinanceError("周期必须是 day/week/month/year")
    if has("interval"):
        try:
            out["interval"] = int(data.get("interval") or 1)
        except (TypeError, ValueError) as exc:
            raise FinanceError("周期间隔必须是整数") from exc
        if not 1 <= out["interval"] <= 120:
            raise FinanceError("周期间隔应在 1–120 之间")
    if has("start_date"):
        out["start_date"] = parse_date(data.get("start_date"), "首次扣款日期").isoformat()
    for key, limit in (("category", 30), ("account", 40), ("note", 500)):
        if has(key):
            out[key] = clean_text(data.get(key), limit)
    if has("url"):
        out["url"] = clean_url(data.get("url"))
    for key, default in (("active", True), ("auto_log", True)):
        if has(key):
            out[key] = bool(data.get(key, default))
    if has("log_from"):
        out["log_from"] = parse_date(data.get("log_from") or today().isoformat(), "自动记账起始日期").isoformat()
    return out


def transaction_fields(data, fx_path, partial=False):
    out = {}

    def has(key):
        return key in data or not partial

    if has("type"):
        out["type"] = str(data.get("type") or "expense")
        if out["type"] not in TYPES:
            raise FinanceError("类型必须是 income 或 expense")
    if has("date"):
        out["date"] = parse_date(data.get("date") or today().isoformat(), "日期").isoformat()
    if has("amount"):
        out["amount"] = clean_amount(data.get("amount"))
    if has("currency"):
        out["currency"] = clean_currency(data.get("currency"))
    for key, limit in (("category", 30), ("account", 40), ("note", 300)):
        if has(key):
            out[key] = clean_text(data.get(key), limit)
    if "rate" in data and data["rate"] not in (None, ""):
        try:
            out["rate"] = float(data["rate"])
        except (TypeError, ValueError) as exc:
            raise FinanceError("汇率必须是数字") from exc
        if not 0 < out["rate"] < 1e9:
            raise FinanceError("汇率必须大于 0")
    return out


# ---------------------------------------------------------------- 存储

class Store:
    def __init__(self, data_path, fx_path):
        self.data_path = data_path
        self.fx_path = fx_path

    def load(self):
        data = read_json(self.data_path, {})
        data.setdefault("subscriptions", [])
        data.setdefault("transactions", [])
        return data

    def save(self, data):
        write_private_json(self.data_path, data)

    # ---- 订阅自动记账
    def auto_log(self, data):
        """把 log_from..今天之间尚未记录的扣款写成支出流水。返回是否有改动。"""
        now = today()
        changed = False
        history_budget = 24  # 一次补记最多联网查 24 个历史日期的汇率，其余用缓存或最新汇率
        for sub in data["subscriptions"]:
            if not (sub.get("active") and sub.get("auto_log")):
                continue
            begin = date.fromisoformat(sub.get("log_from") or sub["created_at"][:10])
            if sub.get("logged_through"):
                begin = max(begin, date.fromisoformat(sub["logged_through"]) + timedelta(days=1))
            n = first_charge_index_on_or_after(sub, begin)
            added = 0
            while added < 400:
                when = charge_date(sub, n)
                if when > now:
                    break
                old = when < now - timedelta(days=2) and sub["currency"] != "CNY"
                try:
                    rate = cny_rate(self.fx_path, sub["currency"], when, allow_fetch=history_budget > 0)
                    history_budget -= 1 if old else 0
                except FinanceError as exc:
                    print("auto_log skipped:", sub["name"], exc, flush=True)
                    break
                stamp = now_iso()
                data["transactions"].append({
                    "id": secrets.token_hex(8), "type": "expense", "date": when.isoformat(),
                    "amount": sub["amount"], "currency": sub["currency"], "rate": rate,
                    "cny": round(sub["amount"] * rate, 2), "category": sub.get("category") or "订阅",
                    "account": sub.get("account", ""), "note": "订阅：" + sub["name"],
                    "sub_id": sub["id"], "created_at": stamp, "updated_at": stamp})
                sub["logged_through"] = when.isoformat()
                changed = True
                added += 1
                n += 1
        return changed

    def payload(self):
        with LOCK:
            data = self.load()
            if self.auto_log(data):
                self.save(data)
        for sub in data["subscriptions"]:
            nxt = next_charge(sub)
            sub["next_date"] = nxt.isoformat() if nxt else None
        data["transactions"].sort(key=lambda t: (t["date"], t["created_at"]), reverse=True)
        data["fx"] = read_fx(self.fx_path)
        data["today"] = today().isoformat()
        return data

    # ---- 订阅 CRUD
    def create_subscription(self, body):
        fields = subscription_fields(body, self.fx_path)
        stamp = now_iso()
        sub = dict(fields, id=secrets.token_hex(8), logged_through=None, created_at=stamp, updated_at=stamp,
                   price_history=[{"date": today().isoformat(), "amount": fields["amount"],
                                   "currency": fields["currency"]}])
        with LOCK:
            data = self.load()
            data["subscriptions"].append(sub)
            self.auto_log(data)
            self.save(data)
        return sub

    def update_subscription(self, sub_id, body):
        fields = subscription_fields(body, self.fx_path, partial=True)
        if not fields:
            raise FinanceError("没有可更新的字段")
        with LOCK:
            data = self.load()
            sub = self._find(data["subscriptions"], sub_id, "订阅")
            price_changed = (fields.get("amount", sub["amount"]) != sub["amount"]
                             or fields.get("currency", sub["currency"]) != sub["currency"])
            sub.update(fields)
            if price_changed:
                history = sub.setdefault("price_history", [])
                entry = {"date": today().isoformat(), "amount": sub["amount"], "currency": sub["currency"]}
                if history and history[-1]["date"] == entry["date"]:
                    history[-1] = entry  # 同一天多次改价只留最后一次
                else:
                    history.append(entry)
            sub["updated_at"] = now_iso()
            self.auto_log(data)
            self.save(data)
        return sub

    def delete_subscription(self, sub_id, drop_transactions=False):
        with LOCK:
            data = self.load()
            self._find(data["subscriptions"], sub_id, "订阅")
            data["subscriptions"] = [s for s in data["subscriptions"] if s["id"] != sub_id]
            if drop_transactions:
                data["transactions"] = [t for t in data["transactions"] if t.get("sub_id") != sub_id]
            self.save(data)

    # ---- 流水 CRUD
    def _rate_for(self, txn):
        if "rate" in txn:
            return txn["rate"]
        return cny_rate(self.fx_path, txn["currency"], date.fromisoformat(txn["date"]))

    def create_transaction(self, body):
        fields = transaction_fields(body, self.fx_path)
        fields["rate"] = self._rate_for(fields)
        fields["cny"] = round(fields["amount"] * fields["rate"], 2)
        stamp = now_iso()
        txn = dict(fields, id=secrets.token_hex(8), sub_id=None, created_at=stamp, updated_at=stamp)
        with LOCK:
            data = self.load()
            data["transactions"].append(txn)
            self.save(data)
        return txn

    def update_transaction(self, txn_id, body):
        fields = transaction_fields(body, self.fx_path, partial=True)
        if not fields:
            raise FinanceError("没有可更新的字段")
        with LOCK:
            current = dict(self._find(self.load()["transactions"], txn_id, "流水"))
        merged = dict(current, **fields)
        if "rate" not in fields and (merged["currency"], merged["date"]) != (current["currency"], current["date"]):
            merged.pop("rate", None)
            fields["rate"] = self._rate_for(merged)  # 网络请求放在锁外
        with LOCK:
            data = self.load()
            txn = self._find(data["transactions"], txn_id, "流水")
            txn.update(fields)
            txn["cny"] = round(txn["amount"] * txn["rate"], 2)
            txn["updated_at"] = now_iso()
            self.save(data)
        return txn

    def delete_transaction(self, txn_id):
        with LOCK:
            data = self.load()
            self._find(data["transactions"], txn_id, "流水")
            data["transactions"] = [t for t in data["transactions"] if t["id"] != txn_id]
            self.save(data)

    @staticmethod
    def _find(items, item_id, label):
        item = next((x for x in items if x.get("id") == item_id), None)
        if not item:
            raise FinanceError("%s不存在" % label, 404)
        return item
