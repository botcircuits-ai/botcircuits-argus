"""Deterministic coding-request detection (`is_coding_request`).

The gate that decides whether a native turn is routed to the coding
pipeline. Conservative by design: an explicit build/modify imperative on a
software noun routes; questions about code, and non-software imperatives, do
not.
"""

from __future__ import annotations

import pytest

from botcircuits.agent.workflow.coding_route import is_coding_request


@pytest.mark.parametrize("text", [
    "add a dark-mode toggle component",
    "implement a rate-limit middleware",
    "fix the bug in the parser",
    "refactor the auth module",
    "write unit tests for the slot resolver",
    "can you add a --verbose flag to the cli",
    "please implement the retry logic in the http client",
    "create a new endpoint for user profiles",
    "rename the function foo to bar",
    "update the database schema migration",
    "remove the deprecated api route",
])
def test_routes_coding_requests(text):
    assert is_coding_request(text) is True


@pytest.mark.parametrize("text", [
    "how do I add a route in flask?",
    "what does the parser do?",
    "why is this function slow?",
    "explain the auth module",
    "which file has the config?",
    "is the endpoint working?",
])
def test_ignores_questions_about_code(text):
    assert is_coding_request(text) is False


@pytest.mark.parametrize("text", [
    "add two numbers for me",
    "create a reminder for tomorrow",
    "write a poem about the ocean",
    "what's the weather today",
    "summarize this article",
    "",
    "   ",
    "thanks!",
])
def test_ignores_non_coding(text):
    assert is_coding_request(text) is False


def test_polite_wrapper_stripped():
    # The imperative is behind a politeness wrapper; still routes.
    assert is_coding_request("could you please refactor the handler module")
    # Wrapper alone, no coding verb → no route.
    assert not is_coding_request("could you please help me understand this")


def test_verb_must_lead():
    # A coding verb buried mid-sentence in a question is not a request.
    assert not is_coding_request(
        "tell me the best way to implement a cache")
