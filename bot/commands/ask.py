"""Ask command — query the LLM via Ollama (async via llm_worker)."""

import logging

from .. import llm_worker
from ..llm_worker import LLMJob, clear_history  # noqa: F401

logger = logging.getLogger('mesh-bot.ask')


def query_llm(prompt: str, node_id=None) -> str:
    return llm_worker.query_sync(prompt, node_id=node_id)


def ask_command(args: str, context: dict):
    if not args.strip():
        return "Usage: ask <question> | ask clear"

    node_id = context.get('from')
    if args.strip().lower() in ('clear', 'reset', 'forget'):
        clear_history(node_id)
        return "Conversation history cleared."

    send = context.get('send')
    channel = context.get('response_channel', 0)

    if send is None:
        return llm_worker.query_sync(args, node_id=node_id)

    llm_worker.submit(LLMJob(
        prompt=args,
        node_id=node_id,
        channel=channel,
        reply=send,
    ))
    return "Thinking..."
