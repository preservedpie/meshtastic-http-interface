"""Bot subpackage — ported from meshtastic-mesh-bot.

All top-level imports inside this package (e.g. ``import db``) resolve to
modules in this directory because ``bot/`` is added to ``sys.path`` by the
host app on startup. That preserves the original mesh-bot source layout
without rewriting every import.
"""
