import logging

from pushover import send_pushover

logger = logging.getLogger(__name__)


def evaluate_rules(packet, rules):
    """
    Evaluate all enabled alert rules against a packet.
    NULL/empty fields in a rule act as wildcards.
    Fires send_pushover for each matching rule.
    Returns the names of the rules that matched (for logging/display in the
    web UI — see app.py's _update_message_flags).
    """
    matched = []
    logger.info(f"Evaluating {len(rules)} rules against packet type={packet.get('type')} channel={packet.get('channel')!r} from={packet.get('from')}")
    for rule in rules:
        if not rule.get('enabled'):
            continue
        if rule.get('packet_type') and packet.get('type') != rule['packet_type']:
            logger.debug(f"Rule '{rule.get('name')}' skip: type {packet.get('type')!r} != {rule['packet_type']!r}")
            continue
        if rule.get('from_node') and packet.get('from') != rule['from_node']:
            logger.debug(f"Rule '{rule.get('name')}' skip: from_node mismatch")
            continue
        if rule.get('channel') and str(packet.get('channel', '')) != str(rule['channel']):
            logger.info(f"Rule '{rule.get('name')}' skip: channel {packet.get('channel')!r} != {rule['channel']!r}")
            continue
        if rule.get('keyword') and rule['keyword'].lower() not in packet.get('text', '').lower():
            logger.debug(f"Rule '{rule.get('name')}' skip: keyword not found")
            continue

        logger.info(f"Rule '{rule.get('name')}' matched packet type={packet.get('type')} from={packet.get('from')}")
        send_pushover(rule, packet)
        matched.append(rule.get('name') or f"rule#{rule.get('id')}")

    return matched
