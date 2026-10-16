"""估热量用的模型: 任何 OpenAI 兼容的 /chat/completions 都行(官方、各家中转、本地 Ollama/vLLM)。
用便宜的小模型就够, 它只需要吐一行 JSON。

环境变量:
  LLM_BASE_URL  比如 https://api.openai.com/v1 或 http://127.0.0.1:11434/v1 (要带 /v1 这一截)
  LLM_API_KEY   没有就留空
  LLM_MODEL     模型名
"""
from __future__ import annotations
import os

import httpx


class NotConfigured(RuntimeError):
    pass


async def complete(system: str, user: str, timeout: float = 40.0) -> str:
    base = (os.environ.get('LLM_BASE_URL') or '').rstrip('/')
    model = os.environ.get('LLM_MODEL') or ''
    key = os.environ.get('LLM_API_KEY') or ''
    if not base or not model:
        raise NotConfigured('LLM_BASE_URL / LLM_MODEL 没设')
    headers = {'Authorization': f'Bearer {key}'} if key else {}
    payload = {'model': model, 'temperature': 0.2, 'max_tokens': 300,
               'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]}
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as cl:
        r = await cl.post(base + '/chat/completions', json=payload, headers=headers)
        r.raise_for_status()
        msg = (r.json().get('choices') or [{}])[0].get('message') or {}
        return msg.get('content') or ''
