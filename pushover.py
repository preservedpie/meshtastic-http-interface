import os
import logging

import requests

logger = logging.getLogger(__name__)

PUSHOVER_TOKEN = os.environ.get('PUSHOVER_TOKEN', '')
PUSHOVER_USER = os.environ.get('PUSHOVER_USER', '')


def send_pushover(rule, packet):
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        logger.warning("Pushover not configured (PUSHOVER_TOKEN/PUSHOVER_USER missing), skipping")
        return

    ptype = packet.get('type', 'unknown')
    from_node = packet.get('from', 'unknown')
    rule_name = rule.get('name', 'Alert')

    if ptype == 'text':
        message = f"[{from_node}] {packet.get('text', '')}"
    else:
        message = f"Packet type '{ptype}' from {from_node}"

    try:
        priority = int(rule.get('priority', 0))
    except (TypeError, ValueError):
        priority = 0

    data = {
        'token': PUSHOVER_TOKEN,
        'user': PUSHOVER_USER,
        'title': f"Meshtastic: {rule_name}",
        'message': message,
        'priority': priority,
    }
    if priority == 2:
        # Emergency priority requires retry/expire or the API rejects with 400.
        data['retry'] = 60      # re-alert every 60s
        data['expire'] = 3600   # give up after an hour
    if rule.get('sound'):
        data['sound'] = rule['sound']

    try:
        resp = requests.post(
            'https://api.pushover.net/1/messages.json',
            data=data,
            timeout=10,
        )
        if not resp.ok:
            logger.error(f"Pushover error {resp.status_code}: {resp.text}")
            return
        logger.info(f"Pushover sent for rule '{rule_name}'")
    except Exception as e:
        logger.error(f"Pushover error: {e}")
