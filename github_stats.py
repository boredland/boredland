#!/usr/bin/python3

import asyncio
import logging
import os
from typing import Dict, List, Optional, Set, Any, cast

import aiohttp
import requests


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("github_stats")


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
        isFork
        stargazers {{
          totalCount
        }}
        forkCount
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
        isFork
        stargazers {{
          totalCount
        }}
        forkCount
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
        exclude_contributed_repos: bool = False,
    ):

        self.username = username
        self._exclude_contributed_repos = exclude_contributed_repos
        self._exclude_repos = set() if exclude_repos is None else exclude_repos
        self._exclude_langs = set() if exclude_langs is None else exclude_langs
        self.queries = Queries(username, access_token, session)

        self._name: Optional[str] = None
        self._stargazers: Optional[int] = None
        self._forks: Optional[int] = None
        self._total_contributions: Optional[int] = None
        self._languages: Optional[Dict[str, Any]] = None
        self._repos: Optional[Set[str]] = None
        self._stats_lock = asyncio.Lock()
        self._stats_ready = False
        self._contributions_lock = asyncio.Lock()

    async def to_str(self) -> str:
        """
        :return: summary of all available statistics
        """
        languages = await self.languages_proportional
        formatted_languages = "\n  - ".join(
            [f"{k}: {v:0.4f}%" for k, v in languages.items()]
        )
        return f"""Name: {await self.name}
Stargazers: {await self.stargazers:,}
Forks: {await self.forks:,}
All-time contributions: {await self.total_contributions:,}
Repositories with contributions: {len(await self.repos)}
Languages:
  - {formatted_languages}"""

    async def get_stats(self) -> None:
        """
        Get lots of summary statistics using one big query. Sets many attributes

        Guarded by a lock: the accumulators are zeroed on entry, so a second
        concurrent caller would otherwise read a half-filled total (or 0) via
        the properties, which check for None rather than completeness.
        """
        async with self._stats_lock:
            if self._stats_ready:
                return
            await self._fetch_stats()
            self._stats_ready = True

    async def _fetch_stats(self) -> None:
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
            contrib_nodes = contrib_repos.get("nodes", []) if not self._exclude_contributed_repos else []
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
                log.info("  Found repo: %s", name)
                if name not in owned_names:
                    continue
                self._stargazers += repo.get("stargazers").get("totalCount", 0)
                self._forks += repo.get("forkCount", 0)

                for lang in repo.get("languages", {}).get("edges", []):
                    lang_name = lang.get("node", {}).get("name", "Other")
                    languages = self._languages
                    if lang_name.lower() in exclude_langs_lower:
                        continue
                    if lang_name in languages:
                        languages[lang_name]["size"] += lang.get("size", 0)
                        languages[lang_name]["occurrences"] += 1
                    else:
                        languages[lang_name] = {
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
        if not self._stats_ready:
            await self.get_stats()
        assert self._stargazers is not None
        return self._stargazers

    @property
    async def forks(self) -> int:
        """
        :return: total number of forks on user's repos
        """
        if not self._stats_ready:
            await self.get_stats()
        assert self._forks is not None
        return self._forks

    @property
    async def languages(self) -> Dict:
        """
        :return: summary of languages used by the user
        """
        if not self._stats_ready:
            await self.get_stats()
        assert self._languages is not None
        return self._languages

    @property
    async def languages_proportional(self) -> Dict:
        """
        :return: summary of languages used by the user, with proportional usage
        """
        if not self._stats_ready:
            await self.get_stats()
        assert self._languages is not None

        return {k: v.get("prop", 0) for (k, v) in self._languages.items()}

    @property
    async def repos(self) -> Set[str]:
        """
        :return: list of names of user's repos
        """
        if not self._stats_ready:
            await self.get_stats()
        assert self._repos is not None
        return self._repos

    @property
    async def total_contributions(self) -> int:
        """
        :return: count of user's total contributions as defined by GitHub

        Same locking rationale as get_stats: the counter is zeroed before the
        awaits that fill it, so an unguarded concurrent reader sees 0.
        """
        async with self._contributions_lock:
            if self._total_contributions is not None:
                return self._total_contributions
            return await self._fetch_contributions()

    async def _fetch_contributions(self) -> int:
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
