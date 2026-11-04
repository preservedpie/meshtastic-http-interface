"""Flask blueprint exposing the bot's control APIs to the React UI."""

import logging

from flask import Blueprint, abort, jsonify, request

from bot import db as bot_db
from bot import settings as bot_settings
from bot import modules as bot_modules
from bot import engine as bot_engine

logger = logging.getLogger('mesh-bot.api')

bot_bp = Blueprint('bot', __name__, url_prefix='/api/bot')


# ---- dashboard ----------------------------------------------------------

@bot_bp.route('/status')
def bot_status():
    return jsonify({
        'enabled': bot_engine.is_enabled(),
        'uptime': bot_engine.uptime_str(),
        'bot_node': bot_engine.bot_node_number(),
        'llm': bot_engine.llm_health(),
        'llm_sessions': bot_engine.llm_sessions(),
        'module_count': len(bot_db.list_modules()),
        'heard_nodes': len(bot_engine.snapshot_nodes()),
    })


@bot_bp.route('/heard-nodes')
def bot_heard_nodes():
    return jsonify(bot_engine.snapshot_nodes())


# ---- settings -----------------------------------------------------------

@bot_bp.route('/settings', methods=['GET'])
def settings_list():
    items = bot_settings.all_with_values()
    groups: dict = {}
    for item in items:
        groups.setdefault(item['group'], []).append(item)
    return jsonify({'groups': groups, 'items': items})


@bot_bp.route('/settings', methods=['POST'])
def settings_update():
    data = request.get_json(force=True) or {}
    for key, val in data.items():
        if key not in {s['key'] for s in bot_settings.SCHEMA}:
            continue
        bot_settings.set(key, '' if val is None else str(val))
    return jsonify({'ok': True})


@bot_bp.route('/settings/<key>', methods=['DELETE'])
def settings_reset(key):
    bot_settings.reset(key)
    return jsonify({'ok': True})


# ---- ignored nodes ------------------------------------------------------

@bot_bp.route('/ignored', methods=['GET'])
def ignored_list():
    return jsonify(sorted(bot_settings.get_ignored_nodes()))


@bot_bp.route('/ignored/<int:node_id>', methods=['POST'])
def ignored_add(node_id):
    bot_settings.add_ignored_node(node_id)
    return jsonify({'ok': True})


@bot_bp.route('/ignored/<int:node_id>', methods=['DELETE'])
def ignored_remove(node_id):
    bot_settings.remove_ignored_node(node_id)
    return jsonify({'ok': True})


# ---- channel policy -----------------------------------------------------

@bot_bp.route('/channels', methods=['GET'])
def channels_list():
    return jsonify(bot_db.list_channel_policies())


@bot_bp.route('/channels', methods=['POST'])
def channels_upsert():
    data = request.get_json(force=True) or {}
    try:
        channel = int(data['channel'])
    except (KeyError, ValueError, TypeError):
        return jsonify({'error': 'channel (int) required'}), 400
    bot_db.upsert_channel_policy(
        channel=channel,
        name=data.get('name', '') or '',
        listen=bool(data.get('listen', True)),
        respond=bool(data.get('respond', True)),
        is_response_ch=bool(data.get('is_response_ch', False)),
        allowed_cmds=data.get('allowed_cmds') or [],
    )
    return jsonify(bot_db.get_channel_policy(channel))


@bot_bp.route('/channels/<int:channel>', methods=['DELETE'])
def channels_delete(channel):
    bot_db.delete_channel_policy(channel)
    return jsonify({'ok': True})


# ---- modules ------------------------------------------------------------

@bot_bp.route('/modules', methods=['GET'])
def modules_list():
    return jsonify(bot_db.list_modules())


@bot_bp.route('/modules', methods=['POST'])
def module_create():
    data = request.get_json(force=True) or {}
    return _save_module(data, module_id=None)


@bot_bp.route('/modules/<int:module_id>', methods=['PUT'])
def module_update(module_id):
    data = request.get_json(force=True) or {}
    if not bot_db.get_module(module_id):
        abort(404)
    return _save_module(data, module_id=module_id)


@bot_bp.route('/modules/<int:module_id>', methods=['DELETE'])
def module_delete(module_id):
    bot_db.delete_module(module_id)
    return jsonify({'ok': True})


@bot_bp.route('/modules/<int:module_id>/toggle', methods=['POST'])
def module_toggle(module_id):
    mod = bot_db.get_module(module_id)
    if not mod:
        abort(404)
    bot_db.set_module_enabled(module_id, not mod['enabled'])
    return jsonify(bot_db.get_module(module_id))


@bot_bp.route('/modules/<int:module_id>/test', methods=['POST'])
def module_test(module_id):
    mod = bot_db.get_module(module_id)
    if not mod:
        abort(404)
    data = request.get_json(silent=True) or {}
    args = data.get('args', '')
    try:
        return jsonify({'ok': True,
                        'result': bot_modules.run_module(mod, args, {'from': 0})})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


def _save_module(data: dict, module_id):
    name = (data.get('name') or '').strip().lower()
    kind = (data.get('kind') or '').strip()
    description = (data.get('description') or '').strip()
    aliases = data.get('aliases') or []
    if isinstance(aliases, str):
        aliases = [a.strip() for a in aliases.split(',') if a.strip()]
    enabled = bool(data.get('enabled', True))
    config = data.get('config') or {}

    if not name:
        return jsonify({'error': "Module name is required."}), 400
    if ' ' in name:
        return jsonify({'error': "Module name cannot contain spaces."}), 400

    err = bot_modules.validate_config(kind, config)
    if err:
        return jsonify({'error': err}), 400
    if kind == 'python' and not bot_settings.get_bool('ALLOW_PYTHON_MODULES', False):
        return jsonify({'error': "Python modules are disabled in Settings."}), 400

    existing = bot_db.get_module_by_name(name)
    if existing and existing['id'] != module_id:
        return jsonify({'error': f"A module named '{name}' already exists."}), 400

    new_id = bot_db.upsert_module(
        name, kind, description, aliases, config,
        enabled=enabled, module_id=module_id,
    )
    return jsonify(bot_db.get_module(new_id)), (200 if module_id else 201)


# ---- message log --------------------------------------------------------

@bot_bp.route('/messages')
def msglog_list():
    try:
        limit = min(int(request.args.get('limit', 200)), 1000)
    except (TypeError, ValueError):
        limit = 200
    search = request.args.get('q', '').strip()
    return jsonify(bot_db.recent_messages(limit=limit, search=search))


# ---- LLM ---------------------------------------------------------------

@bot_bp.route('/llm/health')
def llm_health():
    return jsonify(bot_engine.llm_health())


@bot_bp.route('/llm/sessions')
def llm_sessions():
    return jsonify(bot_engine.llm_sessions())


@bot_bp.route('/llm/test', methods=['POST'])
def llm_test():
    data = request.get_json(silent=True) or {}
    prompt = data.get('prompt', 'Say hello in 5 words.')
    try:
        return jsonify({'ok': True, 'answer': bot_engine.llm_test(prompt)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


# ---- manual test actions -------------------------------------------------

@bot_bp.route('/actions/weather-check', methods=['POST'])
def action_weather_check():
    try:
        return jsonify({'ok': True, **bot_engine.run_weather_check_now()})
    except Exception as e:
        logger.error(f"Manual weather check failed: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@bot_bp.route('/actions/weather-check-per-node', methods=['POST'])
def action_weather_check_per_node():
    try:
        result = bot_engine.run_per_node_weather_check_now()
        if 'error' in result:
            return jsonify({'ok': False, **result}), 400
        return jsonify({'ok': True, **result})
    except Exception as e:
        logger.error(f"Manual per-node weather check failed: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


# ---- manual send (bot-attributed; uses channel rules etc.) -------------

@bot_bp.route('/send', methods=['POST'])
def bot_send():
    data = request.get_json(force=True) or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'text required'}), 400
    try:
        channel = int(data.get('channel') or 0)
    except (TypeError, ValueError):
        return jsonify({'error': 'channel must be an integer'}), 400
    to_node = None
    raw_to = data.get('to_node')
    if raw_to not in (None, ''):
        # Accept decimal, bare hex, or !hex — int(raw_to) alone 500'd on !hex.
        to_node = bot_engine._coerce_int_node(raw_to)
        if to_node is None:
            return jsonify({'error': 'invalid to_node'}), 400
    bot_engine.queue_send(text, channel=channel, to_node=to_node)
    return jsonify({'ok': True})
