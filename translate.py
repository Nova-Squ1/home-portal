#!/usr/bin/env python3
"""translate.py — Edge 翻译插件（extension/）的后端：把一批段落译成简体中文。

走 Codex 订阅 gpt-5.6-luna（news_summary.codex_complete，凭据 /root/.codex/auth.json）。
插件把每段里的链接、加粗等行内元素换成 <g0>…</g0>、<g1/> 占位，这里原样交给模型保留。
译文按原文缓存在内存里（重启清空），同一段只花一次额度。只给管理员用，鉴权在 app.py。
"""
import io
import json
import os
import threading
import zipfile
from collections import OrderedDict

import news_summary

EXT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extension")
MAX_ITEMS = 60
MAX_CHARS = 20_000
CACHE_MAX = 5000

PROMPT = (
    "You are a translation engine. The input is a JSON array of strings taken from a web page. "
    "Translate every string into natural, fluent Simplified Chinese. "
    "Output ONLY a JSON array of strings with exactly the same length and order. "
    "Strings may contain placeholder tags such as <g0>…</g0> and <g1/>: keep every tag, "
    "wrap the matching translated words with it, and never translate or drop them. "
    "Keep &lt; &gt; &amp; entities, URLs, code, numbers and product names as they are. "
    "A string that is already Chinese or cannot be translated is returned unchanged."
)

_cache = OrderedDict()
_lock = threading.Lock()


def _parse(raw, n):
    """模型输出 -> n 个字符串；格式不对抛 RuntimeError。"""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        out = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("模型没有返回 JSON 数组") from exc
    if not isinstance(out, list) or len(out) != n or not all(isinstance(x, str) for x in out):
        raise RuntimeError("译文段数对不上，请重试")
    return out


def translate(texts):
    """texts: 字符串列表 -> 同序译文列表。参数错抛 ValueError，Codex 失败抛 RuntimeError。"""
    if (not isinstance(texts, list) or not texts or len(texts) > MAX_ITEMS
            or not all(isinstance(x, str) for x in texts)):
        raise ValueError("texts 必须是 1~%d 个字符串" % MAX_ITEMS)
    if sum(len(x) for x in texts) > MAX_CHARS:
        raise ValueError("单次不能超过 %d 字" % MAX_CHARS)
    with _lock:
        todo = list(dict.fromkeys(x for x in texts if x not in _cache))
    if todo:
        raw = news_summary.codex_complete(PROMPT, json.dumps(todo, ensure_ascii=False),
                                          effort="low")
        if raw is None:
            raise RuntimeError("Codex 调用失败；令牌过期的话在服务器上跑一次 codex 续期")
        with _lock:
            for src, dst in zip(todo, _parse(raw, len(todo))):
                _cache[src] = dst
            while len(_cache) > CACHE_MAX:
                _cache.popitem(last=False)
    with _lock:
        # 刚写进的条目理论上可能被别的线程挤出缓存，挤掉就原文返回
        return [_cache.get(x, x) for x in texts]


def extension_zip():
    """把 extension/ 现场打成 zip（bytes），解压后就是 Edge「加载解压缩的扩展」要的目录。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(EXT_DIR):
            for name in files:
                path = os.path.join(root, name)
                zf.write(path, os.path.join("codex-translate", os.path.relpath(path, EXT_DIR)))
    return buf.getvalue()


if __name__ == "__main__":
    assert _parse('```json\n["你好", "<g0>世界</g0>"]\n```', 2) == ["你好", "<g0>世界</g0>"]
    for bad in ('["a"]', "not json", '[1, 2]'):
        try:
            _parse(bad, 2)
            raise AssertionError(bad)
        except RuntimeError:
            pass
    print("ok")
