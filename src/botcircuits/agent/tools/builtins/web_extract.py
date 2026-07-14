"""Web extract builtin — fetches a URL and returns readable text content.

Strips HTML tags and returns a clean markdown-ish representation of the
page so the model can read it without dealing with raw HTML.
"""

from __future__ import annotations

import json
import re

from botcircuits.agent.tools.registry import LocalTool, ToolRegistry

_DEFAULT_MAX_CHARS = 20_000
_TIMEOUT = 30


def web_extract_tool(*, max_chars: int = _DEFAULT_MAX_CHARS) -> LocalTool:
    return LocalTool(
        name="web_extract",
        description=(
            "Fetch a URL and extract its readable text content. "
            "Returns the page title and cleaned text (HTML stripped). "
            "Use web_search first to find relevant URLs, then web_extract to read them."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch and extract content from.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": f"Maximum characters to return (default {max_chars}).",
                    "default": max_chars,
                },
            },
            "required": ["url"],
        },
        handler=_make_handler(max_chars),
    )


def _make_handler(default_max: int):
    async def _handle(args: dict) -> str:
        url: str = (args.get("url") or "").strip()
        if not url:
            return json.dumps({"error": "url is required"})
        if not url.startswith(("http://", "https://")):
            return json.dumps({"error": f"invalid URL (must start with http/https): {url}"})
        limit = int(args.get("max_chars") or default_max)
        limit = max(500, min(limit, 100_000))
        return await _extract(url, limit)
    return _handle


async def _extract(url: str, max_chars: int) -> str:
    import httpx

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; BotCircuitsAgent/1.0; "
            "+https://github.com/botcircuits-ai)"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT,
            follow_redirects=True,
            headers=headers,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "text/html" not in content_type and "text/plain" not in content_type:
                return json.dumps({
                    "url": url,
                    "error": f"unsupported content-type: {content_type}",
                })
            text = _html_to_text(resp.text, max_chars)
    except Exception as exc:
        return json.dumps({"error": f"web_extract failed: {exc}", "url": url})

    return json.dumps({"url": url, "content": text}, ensure_ascii=False)


def _html_to_text(html: str, max_chars: int) -> str:
    """Strip HTML and return readable text."""
    # Remove <script>, <style>, <head> blocks entirely
    html = re.sub(r"<(script|style|head)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    # Convert block-level elements to newlines
    html = re.sub(r"<(br|p|div|h[1-6]|li|tr)[^>]*>", "\n", html, flags=re.IGNORECASE)
    # Strip remaining tags
    html = re.sub(r"<[^>]+>", "", html)
    # Decode common HTML entities
    entities = {
        "&amp;": "&", "&lt;": "<", "&gt;": ">",
        "&quot;": '"', "&#39;": "'", "&nbsp;": " ",
        "&mdash;": "—", "&ndash;": "–", "&hellip;": "…",
    }
    for ent, char in entities.items():
        html = html.replace(ent, char)
    # Normalise whitespace
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    html = html.strip()
    return html[:max_chars]


def register(reg: ToolRegistry, *, max_chars: int = _DEFAULT_MAX_CHARS) -> None:
    reg.register(web_extract_tool(max_chars=max_chars))
