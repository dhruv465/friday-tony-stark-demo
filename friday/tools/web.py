"""
Web tools — search, fetch pages, and global news briefings.
"""

import httpx
import xml.etree.ElementTree as ET
import asyncio  # Required for parallel execution
import re
import html as _html
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlparse

_DDG_ANCHOR = re.compile(
    r'<a[^>]*class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.DOTALL,
)
_DDG_SNIPPET = re.compile(
    r'class="result__snippet"[^>]*>(?P<snippet>.*?)</a>', re.DOTALL
)
_TAGS = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    return _html.unescape(_TAGS.sub("", text)).strip()


def _resolve_ddg_link(href: str) -> str:
    """DuckDuckGo wraps results in /l/?uddg=<encoded-url>. Unwrap it."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.path.endswith("/l/"):
        params = parse_qs(parsed.query)
        if "uddg" in params:
            return unquote(params["uddg"][0])
    return href


def _parse_ddg_html(html: str, limit: int) -> list[tuple[str, str, str]]:
    anchors = list(_DDG_ANCHOR.finditer(html))
    snippets = [_clean(m.group("snippet")) for m in _DDG_SNIPPET.finditer(html)]
    out: list[tuple[str, str, str]] = []
    for i, match in enumerate(anchors[:limit]):
        title = _clean(match.group("title"))
        link = _resolve_ddg_link(match.group("href"))
        snippet = snippets[i] if i < len(snippets) else ""
        if title and link:
            out.append((title, link, snippet))
    return out

SEED_FEEDS = [
    'https://feeds.bbci.co.uk/news/world/rss.xml',
    'https://www.cnbc.com/id/100727362/device/rss/rss.html',
    'https://rss.nytimes.com/services/xml/rss/nyt/World.xml',
    'https://www.aljazeera.com/xml/rss/all.xml'
]

FINANCE_SEED_FEEDS = [
    'https://www.cnbc.com/id/10000664/device/rss/rss.html',       # CNBC Finance
    'https://feeds.bloomberg.com/markets/news.rss',                # Bloomberg Markets
    'https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best',  # Reuters
    'https://feeds.marketwatch.com/marketwatch/topstories/',       # MarketWatch
    'https://rss.nytimes.com/services/xml/rss/nyt/Business.xml',  # NYT Business
]

async def fetch_and_parse_feed(client, url):
    """Helper function to handle a single feed request and parse its XML."""
    try:
        response = await client.get(url, headers={'User-Agent': 'Friday-AI/1.0'}, timeout=5.0)
        if response.status_code != 200:
            return []

        root = ET.fromstring(response.content)
        # Extract source name from URL (e.g., 'BBC' or 'NYTIMES')
        source_name = url.split('.')[1].upper()
        
        feed_items = []
        # Get top 5 items per feed
        items = root.findall(".//item")[:5]
        for item in items:
            title = item.findtext("title")
            description = item.findtext("description")
            link = item.findtext("link")
            
            if description:
                description = re.sub('<[^<]+?>', '', description).strip()

            feed_items.append({
                "source": source_name,
                "title": title,
                "summary": description[:200] + "..." if description else "",
                "link": link
            })
        return feed_items
    except Exception:
        # If one feed fails, return an empty list so others can still succeed
        return []

def register(mcp):

    @mcp.tool()
    async def get_world_news() -> str:
        """
        Fetches the latest global headlines from major news outlets simultaneously.
        Use this when the user asks 'What's going on in the world?' or for recent events.
        """
        
        async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
            # 1. Create a list of 'tasks' (one for each URL)
            tasks = [fetch_and_parse_feed(client, url) for url in SEED_FEEDS]
            
            # 2. Fire them all at once and wait for the results
            # results will be a list of lists: [[news from bbc], [news from nyt], ...]
            results_of_lists = await asyncio.gather(*tasks)
            
            # 3. Flatten the list of lists into a single list of articles
            all_articles = [item for sublist in results_of_lists for item in sublist]

        if not all_articles:
            return "The global news grid is unresponsive, sir. I'm unable to pull headlines."

        # 4. Format the final briefing
        report = ["### GLOBAL NEWS BRIEFING (LIVE)\n"]
        # Limit to top 12 items so the AI doesn't get overwhelmed
        for entry in all_articles[:12]:
            report.append(f"**[{entry['source']}]** {entry['title']}")
            report.append(f"{entry['summary']}")
            report.append(f"Link: {entry['link']}\n")

        return "\n".join(report)

    @mcp.tool()
    async def get_world_finance_news() -> str:
        """
        Fetches the latest finance and market headlines from major financial outlets simultaneously.
        Use this when the user asks about finance news, market updates, or economic developments.
        """

        async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
            tasks = [fetch_and_parse_feed(client, url) for url in FINANCE_SEED_FEEDS]
            results_of_lists = await asyncio.gather(*tasks)
            all_articles = [item for sublist in results_of_lists for item in sublist]

        if not all_articles:
            return "The financial feeds are unresponsive right now, sir. I can't pull market headlines."

        report = ["### FINANCE BRIEFING (LIVE)\n"]
        for entry in all_articles[:12]:
            report.append(f"**[{entry['source']}]** {entry['title']}")
            report.append(f"{entry['summary']}")
            report.append(f"Link: {entry['link']}\n")

        return "\n".join(report)

    @mcp.tool()
    async def search_web(query: str, max_results: int = 6) -> str:
        """
        Search the web and return the top results (title, URL, snippet).
        Uses DuckDuckGo's HTML endpoint, so no search API key is required.
        """
        max_results = max(1, min(max_results, 15))
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
                response = await client.post(
                    "https://html.duckduckgo.com/html/",
                    data={"q": query},
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                        "Accept": "text/html,application/xhtml+xml",
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                )
                response.raise_for_status()
                html = response.text
        except Exception as exc:
            return f"Web grid's unreachable right now, boss: {exc}"

        results = _parse_ddg_html(html, max_results)
        if not results:
            return f"No results came back for {query!r}, boss."

        report = [f"### WEB SEARCH — {query}\n"]
        for index, (title, link, snippet) in enumerate(results, 1):
            report.append(f"{index}. {title}")
            if snippet:
                report.append(f"   {snippet}")
            report.append(f"   {link}\n")
        return "\n".join(report)

    @mcp.tool()
    async def fetch_url(url: str) -> str:
        """Fetch the raw text content of a URL."""
        async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text[:4000]
    
    def _schedule_open(url: str, delay_seconds: float) -> None:
        """Defer ``webbrowser.open`` until FRIDAY is done speaking.

        The LLM emits the news brief text and this tool call in the
        same turn. LiveKit fires the tool call the moment it parses
        the function call delta from the LLM — that's usually BEFORE
        the brief's TTS playback even starts. A fixed clock delay
        can't fix this because the brief's TTS length varies wildly
        (3-5 sentences = 8 to 18 seconds).

        Real signal: ``agent_state_changed`` events in the desktop
        event log. While the LiveKit agent is speaking, ``state`` is
        ``"speaking"``. When TTS finishes, it flips to ``"idle"``.
        We tail the log, wait for ``state`` to leave ``speaking`` and
        stay out of it for ``settle_seconds``, then open the browser
        with a tiny additional buffer so the final spoken phrase
        ("Let me open up the world monitor for you.") clears too.

        ``delay_seconds`` is now a *cap*, not a fixed wait. If no
        state signal arrives within that window, we fall back to
        opening anyway — keeps us robust if the voice agent is dead.
        """
        import json
        import threading
        import time
        import webbrowser

        from friday.desktop.events import event_log_path

        settle_seconds = 0.6   # how long state must stay non-speaking
        post_open_buffer = 0.4 # extra silence after settle
        poll_interval = 0.08

        max_wait = max(1.0, float(delay_seconds))

        def _worker():
            path = event_log_path()
            # Seek to end of log so we only see future events. If file
            # doesn't exist yet, treat that as "not speaking" and fall
            # back to a minimal delay.
            try:
                offset = path.stat().st_size
            except FileNotFoundError:
                time.sleep(min(1.5, max_wait))
                webbrowser.open(url)
                return

            deadline = time.monotonic() + max_wait
            saw_speaking = False
            last_state = None
            last_change = time.monotonic()

            while time.monotonic() < deadline:
                # Tail new lines.
                try:
                    with path.open("r", encoding="utf-8") as fh:
                        fh.seek(offset)
                        for line in fh:
                            try:
                                ev = json.loads(line)
                            except (json.JSONDecodeError, ValueError):
                                continue
                            if ev.get("type") != "state":
                                continue
                            new_state = ev.get("state")
                            if new_state != last_state:
                                last_state = new_state
                                last_change = time.monotonic()
                                if new_state == "speaking":
                                    saw_speaking = True
                        offset = fh.tell()
                except OSError:
                    pass

                # Conditions to fire:
                #  - we saw speaking AND state has been non-speaking
                #    for settle_seconds (TTS done)
                if (
                    saw_speaking
                    and last_state not in ("speaking", None)
                    and time.monotonic() - last_change >= settle_seconds
                ):
                    time.sleep(post_open_buffer)
                    webbrowser.open(url)
                    return

                time.sleep(poll_interval)

            # Timeout — open anyway. Better than not opening.
            webbrowser.open(url)

        threading.Thread(target=_worker, name="friday-deferred-open", daemon=True).start()

    @mcp.tool()
    async def open_world_monitor(delay_seconds: float = 30.0) -> str:
        """
        Opens the World Monitor dashboard AFTER FRIDAY has finished
        speaking. The tool tails the LiveKit agent's state events and
        waits for the speaking state to end before opening the browser.

        ``delay_seconds`` is a *cap*, not a fixed wait. If no state
        signal arrives within that window, the browser opens anyway.
        Default 30s comfortably covers any plausible spoken brief.

        Call immediately after delivering a world news brief, in the
        same turn as the spoken "Let me open up the world monitor for
        you." line.
        """
        url = "https://worldmonitor.app/"
        try:
            _schedule_open(url, delay_seconds)
            return "World Monitor queued — will open when speech ends."
        except Exception as e:
            return f"I'm unable to initialize the visual monitor: {str(e)}"

    @mcp.tool()
    async def open_finance_world_monitor(delay_seconds: float = 30.0) -> str:
        """
        Opens the Finance World Monitor AFTER FRIDAY has finished
        speaking. Tails the LiveKit agent's state events and waits for
        the speaking state to end before opening the browser.

        ``delay_seconds`` is a fallback cap, not a fixed wait.

        Call immediately after delivering a finance brief, in the same
        turn as the "Let me pull up the finance monitor for you." line.
        """
        url = "https://finance.worldmonitor.app/"
        try:
            _schedule_open(url, delay_seconds)
            return "Finance Monitor queued — will open when speech ends."
        except Exception as e:
            return f"I'm unable to initialize the finance monitor: {str(e)}"
