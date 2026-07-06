"""Web search builtin — queries DuckDuckGo Instant Answer API (no key needed).

Falls back to a plain HTML scrape of DuckDuckGo search results when the
Instant Answer API returns no organic hits, keeping the tool functional
without any API key.
"""

from __future__ import annotations

import json
import urllib.parse

from botcircuits.agent.tools.registry import LocalTool, ToolRegistry

_DEFAULT_MAX_RESULTS = 5
_TIMEOUT = 15


def web_search_tool(*, max_results: int = _DEFAULT_MAX_RESULTS) -> LocalTool:
    return LocalTool(
        name="web_search",
        description=(
            "Search the web for up-to-date information. "
            "Returns a list of results with titles, URLs, and descriptions. "
            "Use web_extract to fetch the full content of a specific URL."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query.",
                },
                "max_results": {
                    "type": "integer",
                    "description": f"Maximum number of results (default {max_results}).",
                    "default": max_results,
                },
            },
            "required": ["query"],
        },
        handler=_make_handler(max_results),
    )


def _make_handler(default_max: int):
    async def _handle(args: dict) -> str:
        query: str = args.get("query", "").strip()
        if not query:
            return json.dumps({"error": "query is required"})
        limit = int(args.get("max_results") or default_max)
        limit = max(1, min(limit, 20))
        return await _search(query, limit)
    return _handle


async def _search(query: str, limit: int) -> str:
    import httpx

    encoded = urllib.parse.quote_plus(query)

    # Try DuckDuckGo HTML search (most reliable, no key)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; BotCircuitsAgent/1.0; "
            "+https://github.com/botcircuits-ai)"
        ),
    }
    url = f"https://html.duckduckgo.com/html/?q={encoded}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            results = _parse_ddg_html(resp.text, limit)
    except Exception as exc:
        return json.dumps({"error": f"web_search failed: {exc}"})

    return json.dumps({"results": results, "query": query}, ensure_ascii=False)


def _parse_ddg_html(html: str, limit: int) -> list[dict]:
    """Minimal HTML parser — extracts result titles, URLs and snippets."""
    import re

    results: list[dict] = []

    # DuckDuckGo HTML results look like:
    #   <a class="result__a" href="//duckduckgo.com/l/?uddg=<URL>&...">Title</a>
    #   <a class="result__snippet">Snippet text</a>
    link_re = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    snippet_re = re.compile(
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        re.DOTALL,
    )

    links = link_re.findall(html)
    snippets = [m.group(1) for m in snippet_re.finditer(html)]

    for i, (href, title) in enumerate(links):
        if len(results) >= limit:
            break
        # Resolve DuckDuckGo redirect URLs
        url = _resolve_ddg_url(href)
        if not url:
            continue
        title_clean = re.sub(r"<[^>]+>", "", title).strip()
        snippet_clean = re.sub(r"<[^>]+>", "", snippets[i] if i < len(snippets) else "").strip()
        results.append({
            "title": title_clean,
            "url": url,
            "description": snippet_clean,
            "position": i + 1,
        })

    return results


def _resolve_ddg_url(href: str) -> str | None:
    """Extract the real URL from a DuckDuckGo redirect href."""
    import urllib.parse

    if href.startswith("//duckduckgo.com/l/"):
        qs = urllib.parse.urlparse("https:" + href).query
        params = urllib.parse.parse_qs(qs)
        uddg = params.get("uddg", [None])[0]
        if uddg:
            return urllib.parse.unquote(uddg)
    if href.startswith("http"):
        return href
    return None


def register(reg: ToolRegistry, *, max_results: int = _DEFAULT_MAX_RESULTS) -> None:
    reg.register(web_search_tool(max_results=max_results))
