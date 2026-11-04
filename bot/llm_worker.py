"""Async LLM worker — Ollama queue + per-node history. Ported verbatim."""

import queue
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

from . import settings

logger = logging.getLogger('mesh-bot.llm')


@dataclass
class LLMJob:
    prompt: str
    node_id: Optional[int]
    channel: int
    reply: Callable[[str, int], None]
    use_history: bool = True
    enqueued_at: float = field(default_factory=time.time)


_sessions: dict = {}
_sessions_lock = threading.Lock()
_job_queue: "queue.Queue[Optional[LLMJob]]" = queue.Queue()
_worker_thread: Optional[threading.Thread] = None

_health = {
    'last_ok': None,
    'last_error': None,
    'last_latency_ms': None,
    'total_ok': 0,
    'total_failed': 0,
    'queue_depth': 0,
}
_health_lock = threading.Lock()


def _get_history(node_id):
    with _sessions_lock:
        s = _sessions.get(node_id)
        return list(s['messages']) if s else []


def _is_expired(node_id):
    with _sessions_lock:
        s = _sessions.get(node_id)
        if not s or not s['last_active']:
            return False
        timeout = settings.get_int('HISTORY_TIMEOUT_MINUTES', 5) * 60
        return (datetime.now(timezone.utc) - s['last_active']).total_seconds() > timeout


def clear_history(node_id):
    with _sessions_lock:
        _sessions.pop(node_id, None)


def active_sessions():
    with _sessions_lock:
        return [
            {
                'node_id': nid,
                'turns': len(s['messages']) // 2,
                'last_active': s['last_active'].isoformat() if s['last_active'] else None,
            }
            for nid, s in _sessions.items()
        ]


def _update_history(node_id, user_msg, assistant_msg):
    with _sessions_lock:
        if node_id not in _sessions:
            _sessions[node_id] = {'messages': [], 'last_active': None}
        s = _sessions[node_id]
        s['messages'].append({"role": "user", "content": user_msg})
        s['messages'].append({"role": "assistant", "content": assistant_msg})
        s['last_active'] = datetime.now(timezone.utc)
        messages = list(s['messages'])
        max_turns = settings.get_int('MAX_HISTORY_TURNS', 10)

    if len(messages) >= max_turns * 2:
        logger.info(f"History limit reached for node {node_id}, summarizing")
        summary = _summarize_history(messages)
        with _sessions_lock:
            if node_id in _sessions:
                if summary:
                    _sessions[node_id]['messages'] = [
                        {"role": "system",
                         "content": f"Earlier conversation: {summary}"}
                    ]
                else:
                    _sessions[node_id]['messages'] = _sessions[node_id]['messages'][2:]


def _summarize_history(messages):
    max_len = settings.get_int('MAX_RESPONSE_LENGTH', 200)
    summary_messages = [
        {"role": "system",
         "content": "Summarize the following conversation in 1-2 sentences, "
                    "capturing key topics and conclusions. Be brief."},
        *messages,
        {"role": "user", "content": "Summarize the above conversation briefly."},
    ]
    result = _call_ollama(summary_messages)
    return result[:max_len] if result else None


def _call_ollama(messages):
    host = settings.get('LLM_HOST', 'localhost')
    port = settings.get('LLM_PORT', '11434')
    model = settings.get('LLM_MODEL', 'llama3')
    timeout = settings.get_int('LLM_TIMEOUT', 60)
    url = f"http://{host}:{port}/api/chat"
    payload = {"model": model, "messages": messages, "stream": False}
    started = time.time()
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        # A 404 from Ollama means the model isn't pulled on the server — surface
        # that clearly rather than letting it look like a generic request error.
        if resp.status_code == 404:
            logger.error(f"LLM model {model!r} not found on {host}:{port} "
                         f"(run `ollama pull {model}` on the server)")
            _record_health(ok=False, error=f"model '{model}' not installed")
            return None
        resp.raise_for_status()
        data = resp.json()
        content = data.get('message', {}).get('content', '').strip()
        _record_health(ok=True, latency_ms=(time.time() - started) * 1000)
        return content
    except requests.Timeout:
        logger.error("LLM request timed out")
        _record_health(ok=False, error='timeout')
        return None
    except requests.RequestException as e:
        logger.error(f"LLM request failed: {e}")
        _record_health(ok=False, error=str(e))
        return None


def _record_health(ok, latency_ms=None, error=None):
    with _health_lock:
        if ok:
            _health['last_ok'] = datetime.now(timezone.utc).isoformat()
            _health['last_latency_ms'] = round(latency_ms or 0)
            _health['total_ok'] += 1
        else:
            _health['last_error'] = error
            _health['total_failed'] += 1


def health():
    with _health_lock:
        snap = dict(_health)
    snap['queue_depth'] = _job_queue.qsize()
    return snap


def _build_system_prompt():
    persona  = settings.get('LLM_SYSTEM_PROMPT', '')
    per_msg  = settings.get_int('MAX_RESPONSE_LENGTH', 200)
    max_msgs = max(1, min(settings.get_int('MAX_REPLY_MESSAGES', 4), 4))
    total    = per_msg * max_msgs
    return f"{persona}\nKeep responses under {total} characters."


def query_sync(prompt, node_id=None, use_history=True):
    if node_id is not None and use_history and _is_expired(node_id):
        clear_history(node_id)

    per_msg   = settings.get_int('MAX_RESPONSE_LENGTH', 200)
    max_msgs  = max(1, min(settings.get_int('MAX_REPLY_MESSAGES', 4), 4))
    total_max = per_msg * max_msgs

    messages = [{"role": "system", "content": _build_system_prompt()}]
    if node_id is not None and use_history:
        messages.extend(_get_history(node_id))
    messages.append({"role": "user", "content": prompt})

    result = _call_ollama(messages)
    if result is None:
        return "LLM timeout - try a simpler question"

    if len(result) > total_max:
        shorten = messages + [
            {"role": "assistant", "content": result},
            {"role": "user",
             "content": f"Shorten to under {total_max} characters, keep only "
                        f"essential info."},
        ]
        shortened = _call_ollama(shorten)
        if shortened and len(shortened) <= total_max:
            result = shortened
        elif shortened:
            result = shortened[:total_max - 3] + '...'
        else:
            result = result[:total_max - 3] + '...'

    if node_id is not None and use_history:
        _update_history(node_id, prompt, result)
    return result


def submit(job: LLMJob):
    _job_queue.put(job)


def _worker_loop():
    logger.info("LLM worker thread started")
    while True:
        job = _job_queue.get()
        if job is None:
            break
        try:
            waited = time.time() - job.enqueued_at
            if waited > 5:
                logger.info(f"LLM job waited {waited:.1f}s in queue")
            answer = query_sync(job.prompt, node_id=job.node_id,
                                use_history=job.use_history)
            if answer:
                job.reply(answer, job.channel)
        except Exception as e:
            logger.error(f"LLM worker error: {e}")
            try:
                job.reply("LLM error - try again", job.channel)
            except Exception:
                pass
        finally:
            _job_queue.task_done()
    logger.info("LLM worker thread stopped")


def start():
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _worker_thread = threading.Thread(target=_worker_loop,
                                      name='llm-worker', daemon=True)
    _worker_thread.start()


def stop():
    _job_queue.put(None)
