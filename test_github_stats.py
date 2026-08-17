#!/usr/bin/python3

"""Regression tests for the failure modes that used to publish zeroed stats.

A GitHub API call that cannot be answered must raise, so the daily workflow
goes red, rather than returning an empty result that reads as "this user has
nothing" and gets committed to the live site.

These fake the HTTP layer rather than Queries.query, so the retry, status
handling and no-data guards inside query are the code actually under test.
"""

import asyncio
from typing import Any, Dict, List, Optional

import aiohttp
import pytest

import generate_images
import github_stats


class _FakeResponse:
    def __init__(self, status: int, payload: Optional[Dict], text: str = "") -> None:
        self.status = status
        self.headers: Dict[str, str] = {"Retry-After": "0"}
        self._payload = payload
        self._text = text

    async def json(self) -> Optional[Dict]:
        return self._payload

    async def text(self) -> str:
        return self._text


class _FakeSession:
    """Answers every post with the next queued response, repeating the last."""

    def __init__(self, responses: List[Any]) -> None:
        self._responses = responses
        self.calls = 0

    async def post(self, *args: Any, **kwargs: Any) -> _FakeResponse:
        index = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        queued = self._responses[index]
        if isinstance(queued, Exception):
            raise queued
        return queued


def _stats(*responses: Any) -> github_stats.Stats:
    return github_stats.Stats("someone", "token", _FakeSession(list(responses)))


def _ok(payload: Dict) -> _FakeResponse:
    return _FakeResponse(200, payload)


def _repo_payload(repo: Dict) -> Dict:
    return {
        "data": {
            "viewer": {
                "login": "someone",
                "name": "Someone",
                "repositories": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [repo],
                },
                "repositoriesContributedTo": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [],
                },
            }
        }
    }


def _language_edge(size: int) -> Dict:
    return {"size": size, "node": {"name": "Python", "color": "#3572A5"}}


def test_http_error_raises_instead_of_publishing_zeros() -> None:
    stats = _stats(_FakeResponse(404, None, text="Not Found"))

    with pytest.raises(RuntimeError):
        asyncio.run(_read(stats, "stargazers"))


def test_http_error_raises_for_contributions() -> None:
    """The contributions query feeds a published number and needs the same guard."""
    stats = _stats(_FakeResponse(404, None, text="Not Found"))

    with pytest.raises(RuntimeError):
        asyncio.run(_read(stats, "total_contributions"))


def test_null_data_key_raises_runtime_error() -> None:
    """GitHub answers some errors with HTTP 200 and a null data key."""
    stats = _stats(_ok({"data": None, "errors": [{"message": "boom"}]}))

    with pytest.raises(RuntimeError):
        asyncio.run(_read(stats, "stargazers"))


def test_exhausted_retries_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ten 5xx responses must end in a raise, not an empty result."""
    monkeypatch.setattr(github_stats.asyncio, "sleep", _no_sleep)
    stats = _stats(_FakeResponse(500, None, text="Server Error"))

    with pytest.raises(RuntimeError):
        asyncio.run(_read(stats, "stargazers"))


def test_transport_error_is_retried_then_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transport failures retry, and a persistent one ends in a raise."""
    monkeypatch.setattr(github_stats.asyncio, "sleep", _no_sleep)
    session = _FakeSession([aiohttp.ClientError("connection reset")])
    stats = github_stats.Stats("someone", "token", session)

    with pytest.raises(RuntimeError):
        asyncio.run(_read(stats, "stargazers"))

    assert session.calls == 10


def test_transport_error_recovers_when_the_retry_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github_stats.asyncio, "sleep", _no_sleep)
    repo = {
        "nameWithOwner": "someone/repo",
        "isFork": False,
        "stargazers": {"totalCount": 5},
        "forkCount": 0,
        "languages": {"edges": [_language_edge(10)]},
    }
    stats = _stats(aiohttp.ClientError("flaky"), _ok(_repo_payload(repo)))

    assert asyncio.run(_read(stats, "stargazers")) == 5


def test_zero_size_languages_do_not_divide_by_zero() -> None:
    repo = {
        "nameWithOwner": "someone/repo",
        "isFork": False,
        "stargazers": {"totalCount": 3},
        "forkCount": 1,
        "languages": {"edges": [_language_edge(0)]},
    }
    stats = _stats(_ok(_repo_payload(repo)))

    languages = asyncio.run(_read(stats, "languages"))

    assert languages["Python"]["prop"] == 0


def test_null_stargazers_are_treated_as_zero() -> None:
    repo = {
        "nameWithOwner": "someone/repo",
        "isFork": False,
        "stargazers": None,
        "forkCount": 2,
        "languages": {"edges": [_language_edge(100)]},
    }
    stats = _stats(_ok(_repo_payload(repo)))

    assert asyncio.run(_read(stats, "stargazers")) == 0


def test_healthy_response_still_aggregates() -> None:
    repo = {
        "nameWithOwner": "someone/repo",
        "isFork": False,
        "stargazers": {"totalCount": 7},
        "forkCount": 2,
        "languages": {"edges": [_language_edge(100)]},
    }
    stats = _stats(_ok(_repo_payload(repo)))

    assert asyncio.run(_read(stats, "stargazers")) == 7


async def _no_sleep(seconds: float) -> None:
    """Collapse the retry backoff so the retry test stays fast."""


async def _read(stats: github_stats.Stats, attribute: str) -> Any:
    """Await one Stats property.

    Stats builds asyncio.Lock objects in __init__, so each instance is driven
    by exactly one asyncio.run to keep every lock on a single event loop.
    """
    return await getattr(stats, attribute)


def test_ordinal_labels_match_arcade_format() -> None:
    labels = [generate_images.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 23)]

    assert labels == [
        "1ST",
        "2ND",
        "3RD",
        "4TH",
        "11TH",
        "12TH",
        "13TH",
        "21ST",
        "22ND",
        "23RD",
    ]


def test_repo_counts_are_recorded_per_repo() -> None:
    repo = {
        "nameWithOwner": "someone/repo",
        "isFork": False,
        "stargazers": {"totalCount": 9},
        "forkCount": 4,
        "languages": {"edges": [_language_edge(10)]},
    }
    stats = _stats(_ok(_repo_payload(repo)))

    counts = asyncio.run(_read(stats, "repo_counts"))

    assert counts["someone/repo"] == {"stars": 9, "forks": 4}


def test_llms_prose_counts_are_refreshed() -> None:
    text = (
        "### thing (a project)\n"
        "Does things. 806 stars, 30 forks.\n"
        "- Source: https://github.com/someone/repo\n"
        "\n"
        "### other (untouched)\n"
        "Also does things. 5 stars, 1 forks.\n"
        "- Source: https://github.com/someone/other\n"
    )

    refreshed = generate_images._refresh_repo_counts_prose(
        text, {"someone/repo": {"stars": 805, "forks": 30}}
    )

    assert "805 stars, 30 forks." in refreshed
    assert "5 stars, 1 forks." in refreshed
