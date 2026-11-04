"""News command — top headlines via RSS feeds (no API key)."""

import urllib.request
import re

NEWS_FEEDS = {
    'top':   'https://feeds.bbci.co.uk/news/rss.xml',
    'us':    'https://feeds.nbcnews.com/nbcnews/public/news',
    'world': 'https://feeds.bbci.co.uk/news/world/rss.xml',
    'tech':  'https://feeds.bbci.co.uk/news/technology/rss.xml',
    'sci':   'https://feeds.bbci.co.uk/news/science_and_environment/rss.xml',
}
DEFAULT_COUNT = 3
MAX_TITLE_LEN = 80


def _parse_titles(xml: str, count: int) -> list:
    titles = re.findall(r'<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>', xml, re.DOTALL)
    return [t.strip() for t in titles[1:count + 1] if t.strip()]


def news_command(args: str, context: dict) -> str:
    arg = args.strip().lower()
    feed_key = 'top'
    for key in NEWS_FEEDS:
        if arg.startswith(key):
            feed_key = key
            break

    feed_url = NEWS_FEEDS[feed_key]
    try:
        req = urllib.request.Request(feed_url, headers={'User-Agent': 'MeshBot/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml = resp.read().decode('utf-8', errors='replace')
        titles = _parse_titles(xml, DEFAULT_COUNT)
        if not titles:
            return "News: No headlines found"
        lines = [f"News ({feed_key.upper()}):"]
        for i, title in enumerate(titles, 1):
            if len(title) > MAX_TITLE_LEN:
                title = title[:MAX_TITLE_LEN - 3] + "..."
            lines.append(f"{i}. {title}")
        return "\n".join(lines)
    except urllib.error.URLError:
        return "News: Network error"
    except Exception as e:
        return f"News error: {e}"
