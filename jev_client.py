#!/usr/bin/env python3
"""jev_client.py — TypeSafe Jev (System One) 极薄客户端。仅标准库。

- key：/root/.hermes/.env 的 TYPESAFE_API_KEY（600）
- ask(state, questions)：一次往返；questions 为 {id: {type, instructions, criteria}}
- 失败返回 None（调用方自行回退），不抛异常
"""
import json
import os
import urllib.request
import urllib.error

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
ENV_PATH = "/root/.hermes/.env"
TIMEOUT = 15


def _key():
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("TYPESAFE_API_KEY="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def ask(state, questions, timeout=TIMEOUT):
    """返回 {"answers": {id: {...}}, "usage": {...}} 或 None。"""
    k = _key()
    if not k:
        return None
    payload = {"model": MODEL, "state": state, "questions": questions}
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + k,
            "User-Agent": "jev-client/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict) and data.get("answers"):
            return data
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            TimeoutError, OSError):
        pass
    return None
