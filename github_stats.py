#!/usr/bin/python3

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any, cast

import aiohttp
import requests


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("github_stats")

CACHE_FILE = Path("generated/cache.json")


def _load_cache() -> Dict:
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE) as f:
                data = json.load(f)
            log.info("Cache loaded: %d repos cached", len(data.get("repos", {})))
            return data
        except Exception as e:
            log.warning("Failed to load cache: %s", e)
            return {}
    log.info("No cache found, starting fresh")
    return {}


###############################################################################
# Main
###############################################################################


class Queries(object):
    """
    Class with functions to query the GitHub GraphQL (v4) API and the REST (v3)
    API. Also includes functions to dynamically generate GraphQL queries.
    """

    def __init__(
        self,
        username: str,
        access_token: str,
        session: aiohttp.ClientSession,
        max_connections: int = 5,
    ):
        self.username = username
        self.access_token = access_token
        self.session = session
        self.semaphore = asyncio.Semaphore(max_connections)

    async def query(self, generated_query: str) -> Dict:
        """
        Make a request to the GraphQL API using the authentication token from
        the environment
        :param generated_query: string query to be sent to the API
        :return: decoded GraphQL JSON output
        """
        headers = {
            "Authorization": f"Bearer {self.access_token}",
        }
        log.debug("GraphQL query: %.120s", generated_query.strip())
        for attempt in range(10):
            try:
                async with self.semaphore:
                    r_async = await self.session.post(
                        "https://api.github.com/graphql",
                        headers=headers,
                        json={"query": generated_query},
                    )
                if r_async.status in (429, 403):
                    retry_after = int(r_async.headers.get("Retry-After", 60))
                    log.warning("GraphQL rate limited (HTTP %d). Waiting %ds... (attempt %d/10)", r_async.status, retry_after, attempt + 1)
                    await asyncio.sleep(retry_after)
                    continue
                if r_async.status == 401:
                    body = await r_async.text()
                    raise RuntimeError(f"GraphQL auth failed (HTTP 401): {body[:200]} — check that the GH_TOKEN secret is a valid, non-expired PAT")
                if r_async.status >= 500:
                    body = await r_async.text()
                    wait = min(60, 2 ** attempt)
                    log.warning("GraphQL HTTP %d (transient). Waiting %ds... (attempt %d/10): %s", r_async.status, wait, attempt + 1, body[:120])
                    await asyncio.sleep(wait)
                    continue
                if r_async.status >= 400:
                    body = await r_async.text()
                    log.error("GraphQL HTTP %d: %s", r_async.status, body[:200])
                    return dict()
                result = await r_async.json()
                if result is not None:
                    if "errors" in result:
                        log.warning("GraphQL response contains errors: %s", result["errors"])
                    return result
            except Exception as e:
                log.error("aiohttp failed for GraphQL query: %s — falling back to requests (attempt %d/10)", e, attempt + 1)
                try:
                    async with self.semaphore:
                        r_requests = requests.post(
                            "https://api.github.com/graphql",
                            headers=headers,
                            json={"query": generated_query},
                            timeout=30,
                        )
                    if r_requests.status_code in (429, 403):
                        retry_after = int(r_requests.headers.get("Retry-After", 60))
                        log.warning("GraphQL rate limited (HTTP %d). Waiting %ds... (attempt %d/10)", r_requests.status_code, retry_after, attempt + 1)
                        await asyncio.sleep(retry_after)
                        continue
                    if r_requests.status_code >= 500:
                        wait = min(60, 2 ** attempt)
                        log.warning("GraphQL fallback HTTP %d (transient). Waiting %ds... (attempt %d/10)", r_requests.status_code, wait, attempt + 1)
                        await asyncio.sleep(wait)
                        continue
                    if r_requests.status_code != 200:
                        log.error("GraphQL fallback HTTP %d: %s", r_requests.status_code, r_requests.text[:200])
                        return dict()
                    return r_requests.json()
                except Exception as inner:
                    wait = min(60, 2 ** attempt)
                    log.warning("GraphQL fallback also failed: %s. Waiting %ds... (attempt %d/10)", inner, wait, attempt + 1)
                    await asyncio.sleep(wait)
                    continue
            break

        log.error("GraphQL query failed after all attempts")
        return dict()

    async def query_rest(self, path: str, params: Optional[Dict] = None) -> Dict:
        """
        Make a request to the REST API
        :param path: API path to query
        :param params: Query parameters to be passed to the API
        :return: deserialized REST JSON output
        """
        if params is None:
            params = dict()
        if path.startswith("/"):
            path = path[1:]
        log.debug("REST GET %s", path)
        for attempt in range(60):
            headers = {
                "Authorization": f"token {self.access_token}",
            }
            try:
                async with self.semaphore:
                    r_async = await self.session.get(
                        f"https://api.github.com/{path}",
                        headers=headers,
                        params=tuple(params.items()),
                    )
                if r_async.status == 202:
                    log.info("REST %s returned 202 (stats computing). Retrying... (attempt %d/60)", path, attempt + 1)
                    await asyncio.sleep(2)
                    continue
                if r_async.status == 401:
                    body = await r_async.text()
                    raise RuntimeError(f"REST auth failed on {path} (HTTP 401): {body[:200]} — check that the GH_TOKEN secret is a valid, non-expired PAT")
                if r_async.status in (429, 403):
                    retry_after = r_async.headers.get("Retry-After")
                    if retry_after is None and r_async.status == 403:
                        body = await r_async.json()
                        log.warning("REST %s: permission denied (HTTP 403): %s", path, body.get("message", ""))
                        return dict()
                    wait = int(retry_after) if retry_after else 60
                    log.warning("REST rate limited on %s (HTTP %d). Waiting %ds... (attempt %d/60)", path, r_async.status, wait, attempt + 1)
                    await asyncio.sleep(wait)
                    continue
                if r_async.status >= 500:
                    wait = min(30, 2 ** min(attempt, 5))
                    log.warning("REST %s HTTP %d (transient). Waiting %ds... (attempt %d/60)", path, r_async.status, wait, attempt + 1)
                    await asyncio.sleep(wait)
                    continue
                if r_async.status >= 400:
                    body = await r_async.text()
                    log.error("REST %s HTTP %d: %s", path, r_async.status, body[:200])
                    return dict()
                result = await r_async.json()
                if result is not None:
                    return result
            except Exception as e:
                log.error("aiohttp failed for REST GET %s: %s — falling back to requests (attempt %d/60)", path, e, attempt + 1)
                try:
                    async with self.semaphore:
                        r_requests = requests.get(
                            f"https://api.github.com/{path}",
                            headers=headers,
                            params=tuple(params.items()),
                            timeout=30,
                        )
                    if r_requests.status_code == 202:
                        log.info("REST %s returned 202 (stats computing). Retrying... (attempt %d/60)", path, attempt + 1)
                        await asyncio.sleep(2)
                        continue
                    if r_requests.status_code in (429, 403):
                        retry_after = r_requests.headers.get("Retry-After")
                        if retry_after is None and r_requests.status_code == 403:
                            body = r_requests.json()
                            log.warning("REST %s: permission denied (HTTP 403): %s", path, body.get("message", ""))
                            return dict()
                        wait = int(retry_after) if retry_after else 60
                        log.warning("REST rate limited on %s (HTTP %d). Waiting %ds... (attempt %d/60)", path, r_requests.status_code, wait, attempt + 1)
                        await asyncio.sleep(wait)
                        continue
                    if r_requests.status_code >= 500:
                        wait = min(30, 2 ** min(attempt, 5))
                        log.warning("REST %s fallback HTTP %d (transient). Waiting %ds... (attempt %d/60)", path, r_requests.status_code, wait, attempt + 1)
                        await asyncio.sleep(wait)
                        continue
                    if r_requests.status_code == 200:
                        return r_requests.json()
                    log.error("REST %s fallback HTTP %d: %s", path, r_requests.status_code, r_requests.text[:200])
                    return dict()
                except Exception as inner:
                    wait = min(30, 2 ** min(attempt, 5))
                    log.warning("REST %s fallback also failed: %s. Waiting %ds... (attempt %d/60)", path, inner, wait, attempt + 1)
                    await asyncio.sleep(wait)
                    continue
        log.error("REST %s: too many retries, data will be incomplete", path)
        return dict()

    @staticmethod
    def repos_overview(
        contrib_cursor: Optional[str] = None, owned_cursor: Optional[str] = None
    ) -> str:
        """
        :return: GraphQL query with overview of user repositories
        """
        return f"""{{
  viewer {{
    login,
    name,
    repositories(
      first: 100,
      orderBy: {{
        field: UPDATED_AT,
        direction: DESC
      }},
      isFork: false,
      after: {"null" if owned_cursor is None else '"' + owned_cursor + '"'}
    ) {{
      pageInfo {{
        hasNextPage
        endCursor
      }}
      nodes {{
        nameWithOwner
        isArchived
        isFork
        stargazers {{
          totalCount
        }}
        forkCount
        pushedAt
        languages(first: 10, orderBy: {{field: SIZE, direction: DESC}}) {{
          edges {{
            size
            node {{
              name
              color
            }}
          }}
        }}
      }}
    }}
    repositoriesContributedTo(
      first: 100,
      includeUserRepositories: false,
      orderBy: {{
        field: UPDATED_AT,
        direction: DESC
      }},
      contributionTypes: [COMMIT, PULL_REQUEST, REPOSITORY, PULL_REQUEST_REVIEW],
      after: {"null" if contrib_cursor is None else '"' + contrib_cursor + '"'}
    ) {{
      pageInfo {{
        hasNextPage
        endCursor
      }}
      nodes {{
        nameWithOwner
        isArchived
        isFork
        stargazers {{
          totalCount
        }}
        forkCount
        pushedAt
        languages(first: 10, orderBy: {{field: SIZE, direction: DESC}}) {{
          edges {{
            size
            node {{
              name
              color
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""

    @staticmethod
    def contrib_years() -> str:
        """
        :return: GraphQL query to get all years the user has been a contributor
        """
        return """
query {
  viewer {
    contributionsCollection {
      contributionYears
    }
  }
}
"""

    @staticmethod
    def contribs_by_year(year: str) -> str:
        """
        :param year: year to query for
        :return: portion of a GraphQL query with desired info for a given year
        """
        return f"""
    year{year}: contributionsCollection(
      from: "{year}-01-01T00:00:00Z",
      to: "{int(year) + 1}-01-01T00:00:00Z"
    )
    {{
      contributionCalendar {{
        totalContributions
      }}
    }}
"""

    @classmethod
    def all_contribs(cls, years: List[str]) -> str:
        """
        :param years: list of years to get contributions for
        :return: query to retrieve contribution information for all user years
        """
        by_years = "\n".join(map(cls.contribs_by_year, years))
        return f"""
query {{
  viewer {{
    {by_years}
  }}
}}
"""


class Stats(object):
    """
    Retrieve and store statistics about GitHub usage.
    """

    def __init__(
        self,
        username: str,
        access_token: str,
        session: aiohttp.ClientSession,
        exclude_repos: Optional[Set] = None,
        exclude_langs: Optional[Set] = None,
        ignore_forked_repos: bool = False,
    ):

        self.username = username
        self._ignore_forked_repos = ignore_forked_repos
        self._exclude_repos = set() if exclude_repos is None else exclude_repos
        self._exclude_langs = set() if exclude_langs is None else exclude_langs
        self.queries = Queries(username, access_token, session)

        self._name: Optional[str] = None
        self._stargazers: Optional[int] = None
        self._forks: Optional[int] = None
        self._total_contributions: Optional[int] = None
        self._languages: Optional[Dict[str, Any]] = None
        self._repos: Optional[Set[str]] = None
        self._lines_changed: Optional[Tuple[int, int]] = None
        self._views: Optional[int] = None
        self._cache: Dict = _load_cache()
        self._repo_pushed_at: Dict[str, str] = {}
        self._repo_archived: Dict[str, bool] = {}
        self._repo_owned: Set[str] = set()
        self._new_cache_repos: Dict[str, Any] = {}

    async def to_str(self) -> str:
        """
        :return: summary of all available statistics
        """
        languages = await self.languages_proportional
        formatted_languages = "\n  - ".join(
            [f"{k}: {v:0.4f}%" for k, v in languages.items()]
        )
        lines_changed = await self.lines_changed
        return f"""Name: {await self.name}
Stargazers: {await self.stargazers:,}
Forks: {await self.forks:,}
All-time contributions: {await self.total_contributions:,}
Repositories with contributions: {len(await self.repos)}
Lines of code added: {lines_changed[0]:,}
Lines of code deleted: {lines_changed[1]:,}
Lines of code changed: {lines_changed[0] + lines_changed[1]:,}
Project page views: {await self.views:,}
Languages:
  - {formatted_languages}"""

    async def get_stats(self) -> None:
        """
        Get lots of summary statistics using one big query. Sets many attributes
        """
        log.info("Fetching repository overview for %s", self.username)
        self._stargazers = 0
        self._forks = 0
        self._languages = dict()
        self._repos = set()

        exclude_langs_lower = {x.lower() for x in self._exclude_langs}

        next_owned = None
        next_contrib = None
        page = 0
        while True:
            page += 1
            log.info("Fetching repos page %d", page)
            raw_results = await self.queries.query(
                Queries.repos_overview(
                    owned_cursor=next_owned, contrib_cursor=next_contrib
                )
            )
            raw_results = raw_results if raw_results is not None else {}

            viewer = raw_results.get("data", {}).get("viewer", {}) or {}
            self._name = viewer.get("name") or viewer.get("login") or "No Name"
            viewer_login = viewer.get("login")
            if viewer_login and viewer_login != self.username:
                log.info("Overriding username %r with token's viewer.login %r", self.username, viewer_login)
                self.username = viewer_login
                self.queries.username = viewer_login

            contrib_repos = (
                raw_results.get("data", {})
                .get("viewer", {})
                .get("repositoriesContributedTo", {})
            )
            owned_repos = (
                raw_results.get("data", {}).get("viewer", {}).get("repositories", {})
            )

            owned_nodes = owned_repos.get("nodes", [])
            contrib_nodes = contrib_repos.get("nodes", []) if not self._ignore_forked_repos else []
            owned_names = {r.get("nameWithOwner") for r in owned_nodes if r}

            for repo in owned_nodes + contrib_nodes:
                if repo is None:
                    continue
                name = repo.get("nameWithOwner")
                if name in self._repos or name in self._exclude_repos:
                    continue
                if repo.get("isFork"):
                    log.info("  Skipping fork: %s", name)
                    continue
                self._repos.add(name)
                if name in owned_names:
                    self._repo_owned.add(name)
                    self._stargazers += repo.get("stargazers").get("totalCount", 0)
                    self._forks += repo.get("forkCount", 0)
                if pushed_at := repo.get("pushedAt"):
                    self._repo_pushed_at[name] = pushed_at
                self._repo_archived[name] = bool(repo.get("isArchived"))
                log.info("  Found repo: %s (pushed: %s)", name, repo.get("pushedAt", "unknown"))

                for lang in repo.get("languages", {}).get("edges", []):
                    name = lang.get("node", {}).get("name", "Other")
                    languages = await self.languages
                    if name.lower() in exclude_langs_lower:
                        continue
                    if name in languages:
                        languages[name]["size"] += lang.get("size", 0)
                        languages[name]["occurrences"] += 1
                    else:
                        languages[name] = {
                            "size": lang.get("size", 0),
                            "occurrences": 1,
                            "color": lang.get("node", {}).get("color"),
                        }

            if owned_repos.get("pageInfo", {}).get(
                "hasNextPage", False
            ) or contrib_repos.get("pageInfo", {}).get("hasNextPage", False):
                next_owned = owned_repos.get("pageInfo", {}).get(
                    "endCursor", next_owned
                )
                next_contrib = contrib_repos.get("pageInfo", {}).get(
                    "endCursor", next_contrib
                )
            else:
                break

        log.info("Discovered %d repos total", len(self._repos))
        langs_total = sum([v.get("size", 0) for v in self._languages.values()])
        for k, v in self._languages.items():
            v["prop"] = 100 * (v.get("size", 0) / langs_total)

    @property
    async def name(self) -> str:
        """
        :return: GitHub user's name (e.g., Jacob Strieb)
        """
        if self._name is not None:
            return self._name
        await self.get_stats()
        assert self._name is not None
        return self._name

    @property
    async def stargazers(self) -> int:
        """
        :return: total number of stargazers on user's repos
        """
        if self._stargazers is not None:
            return self._stargazers
        await self.get_stats()
        assert self._stargazers is not None
        return self._stargazers

    @property
    async def forks(self) -> int:
        """
        :return: total number of forks on user's repos
        """
        if self._forks is not None:
            return self._forks
        await self.get_stats()
        assert self._forks is not None
        return self._forks

    @property
    async def languages(self) -> Dict:
        """
        :return: summary of languages used by the user
        """
        if self._languages is not None:
            return self._languages
        await self.get_stats()
        assert self._languages is not None
        return self._languages

    @property
    async def languages_proportional(self) -> Dict:
        """
        :return: summary of languages used by the user, with proportional usage
        """
        if self._languages is None:
            await self.get_stats()
        assert self._languages is not None

        return {k: v.get("prop", 0) for (k, v) in self._languages.items()}

    @property
    async def repos(self) -> Set[str]:
        """
        :return: list of names of user's repos
        """
        if self._repos is not None:
            return self._repos
        await self.get_stats()
        assert self._repos is not None
        return self._repos

    @property
    async def total_contributions(self) -> int:
        """
        :return: count of user's total contributions as defined by GitHub
        """
        if self._total_contributions is not None:
            return self._total_contributions

        self._total_contributions = 0
        log.info("Fetching contribution years")
        years = (
            (await self.queries.query(Queries.contrib_years()))
            .get("data", {})
            .get("viewer", {})
            .get("contributionsCollection", {})
            .get("contributionYears", [])
        )
        log.info("Fetching contributions for years: %s", years)
        by_year = (
            (await self.queries.query(Queries.all_contribs(years)))
            .get("data", {})
            .get("viewer", {})
            .values()
        )

        for year in by_year:
            self._total_contributions += year.get("contributionCalendar", {}).get(
                "totalContributions", 0
            )
        return cast(int, self._total_contributions)

    @property
    async def lines_changed(self) -> Tuple[int, int]:
        """
        :return: count of total lines added, removed, or modified by the user
        """
        if self._lines_changed is not None:
            return self._lines_changed
        repos = await self.repos
        cached_repos = self._cache.get("repos", {})
        log.info("Fetching contributor stats for %d repos (parallel)", len(repos))

        async def fetch_repo_lines(repo: str) -> Tuple[int, int]:
            pushed_at = self._repo_pushed_at.get(repo)
            archived = self._repo_archived.get(repo, False)
            cached = cached_repos.get(repo, {})

            if archived and cached:
                log.info("  [archived]   %s — using cached value", repo)
                self._new_cache_repos[repo] = {**cached, "archived": True}
                return cached.get("additions", 0), cached.get("deletions", 0)

            if pushed_at and cached.get("pushedAt") == pushed_at:
                log.info("  [cache hit]  %s", repo)
                self._new_cache_repos[repo] = cached
                return cached.get("additions", 0), cached.get("deletions", 0)

            log.info("  [fetching]   %s", repo)
            repo_additions = 0
            repo_deletions = 0
            r = await self.queries.query_rest(f"/repos/{repo}/stats/contributors")

            for author_obj in r:
                if not isinstance(author_obj, dict) or not isinstance(
                    author_obj.get("author", {}), dict
                ):
                    continue
                author = author_obj.get("author", {}).get("login", "")
                if author != self.username:
                    continue
                for week in author_obj.get("weeks", []):
                    repo_additions += week.get("a", 0)
                    repo_deletions += week.get("d", 0)

            log.info("  [done]       %s (+%d/-%d lines)", repo, repo_additions, repo_deletions)
            self._new_cache_repos[repo] = {
                "pushedAt": pushed_at,
                "additions": repo_additions,
                "deletions": repo_deletions,
                "archived": archived,
            }
            self.save_cache()
            return repo_additions, repo_deletions

        results = await asyncio.gather(*[fetch_repo_lines(r) for r in repos])
        additions = sum(a for a, _ in results)
        deletions = sum(d for _, d in results)
        self._lines_changed = (additions, deletions)
        return self._lines_changed

    def save_cache(self, *, final: bool = False) -> None:
        """
        Persist per-repo contributor stats so future runs can skip unchanged repos.
        """
        merged = {**self._cache.get("repos", {}), **self._new_cache_repos}
        CACHE_FILE.parent.mkdir(exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump({"repos": merged}, f, indent=2)
        if final:
            log.info("Cache saved: %d repos (%d refreshed this run)", len(merged), len(self._new_cache_repos))

    @property
    async def views(self) -> int:
        """
        Note: only returns views for the last 14 days (as-per GitHub API)
        :return: total number of page views the user's projects have received
        """
        if self._views is not None:
            return self._views

        async def fetch_repo_views(repo: str) -> int:
            r = await self.queries.query_rest(f"/repos/{repo}/traffic/views")
            return sum(view.get("count", 0) for view in r.get("views", []))

        await self.repos
        owned = self._repo_owned
        log.info("Fetching traffic views for %d owned repos", len(owned))
        results = await asyncio.gather(*[fetch_repo_views(r) for r in owned])
        self._views = sum(results)
        return self._views


###############################################################################
# Main
###############################################################################


async def main() -> None:
    """
    Used mostly for testing; this module is not usually run standalone
    """
    access_token = os.getenv("ACCESS_TOKEN")
    user = os.getenv("GITHUB_ACTOR")
    if access_token is None or user is None:
        raise RuntimeError(
            "ACCESS_TOKEN and GITHUB_ACTOR environment variables cannot be None!"
        )
    async with aiohttp.ClientSession() as session:
        s = Stats(user, access_token, session)
        print(await s.to_str())


if __name__ == "__main__":
    asyncio.run(main())
