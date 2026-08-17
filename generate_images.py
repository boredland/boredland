#!/usr/bin/python3

import asyncio
import json
import os
import re

import aiohttp

from typing import Dict

from github_stats import Stats

LANGUAGE_ROWS = 8
STARS_WEIGHT = 1000
FORKS_WEIGHT = 100


################################################################################
# Helper Functions
################################################################################


def generate_output_folder() -> None:
    """
    Create the output folder if it does not already exist
    """
    if not os.path.isdir("generated"):
        os.mkdir("generated")


################################################################################
# Individual Image Generation Functions
################################################################################


async def generate_overview(s: Stats) -> None:
    """
    Generate an SVG badge with summary statistics
    :param s: Represents user's GitHub statistics
    """
    with open("templates/overview.svg", "r") as f:
        output = f.read()

    output = re.sub("{{ name }}", await s.name, output)
    output = re.sub("{{ stars }}", f"{await s.stargazers:,}", output)
    output = re.sub("{{ forks }}", f"{await s.forks:,}", output)
    output = re.sub("{{ contributions }}", f"{await s.total_contributions:,}", output)
    output = re.sub("{{ repos }}", f"{len(await s.repos):,}", output)

    generate_output_folder()
    with open("generated/overview.svg", "w") as f:
        f.write(output)


async def generate_languages(s: Stats) -> None:
    """
    Generate an SVG badge with summary languages used
    :param s: Represents user's GitHub statistics
    """
    with open("templates/languages.svg", "r") as f:
        output = f.read()

    progress = ""
    lang_list = ""
    sorted_languages = sorted(
        (await s.languages).items(), reverse=True, key=lambda t: t[1].get("size")
    )
    delay_between = 150

    for i, (lang, data) in enumerate(sorted_languages):
        color = data.get("color")
        color = color if color is not None else "#000000"
        progress += (
            f'<span style="background-color: {color};'
            f'width: {data.get("prop", 0):0.3f}%;" '
            f'class="progress-item"></span>'
        )
        lang_list += f"""
        <li style="animation-delay: {i * delay_between}ms;">
<svg xmlns="http://www.w3.org/2000/svg" class="octicon" style="fill:{color};" viewBox="0 0 16 16" version="1.1" width="16" height="16"><path fill-rule="evenodd" d="M8 4a4 4 0 100 8 4 4 0 000-8z"></path></svg>
<span class="lang">{lang}</span>
<span class="percent">{data.get("prop", 0):0.2f}%</span>
</li>

"""

    output = re.sub(r"{{ progress }}", progress, output)
    output = re.sub(r"{{ lang_list }}", lang_list, output)

    generate_output_folder()
    with open("generated/languages.svg", "w") as f:
        f.write(output)


def _refresh_repo_counts_prose(text: str, counts: Dict[str, Dict[str, int]]) -> str:
    """Update the "N stars, M forks" clauses in llms.txt project prose.

    The prose is hand-written; only the counts are owned by the pipeline, so
    each clause is rewritten in place next to the Source link naming its repo.
    """
    for repo, data in counts.items():
        pattern = (
            r"(\d+) stars, (\d+) forks\.((?:(?!\n### ).)*?Source: https://github\.com/"
            + re.escape(repo)
            + r")"
        )
        replacement = (
            f"{data['stars']} stars, {data['forks']} forks." + r"\3"
        )
        text = re.sub(pattern, replacement, text, flags=re.DOTALL)
    return text


async def generate_index(s: Stats) -> None:
    """
    Write the aggregate counters and language shares into index.html.

    The page is hand-written, so instead of templating it we patch only the
    values the stats pipeline owns: elements carrying a data-stat attribute,
    and the language list fenced between the languages:start/end markers.
    :param s: Represents user's GitHub statistics
    """
    with open("index.html", "r", encoding="utf-8") as f:
        output = f.read()

    stats = {
        "stars": f"{await s.stargazers:,}",
        "forks": f"{await s.forks:,}",
        "contributions": f"{await s.total_contributions:,}",
        "repos": f"{len(await s.repos):,}",
    }
    for key, value in stats.items():
        output, count = re.subn(
            rf'(<span[^>]*data-stat="{key}"[^>]*>)[^<]*(</span>)',
            lambda m: f"{m.group(1)}{value}{m.group(2)}",
            output,
        )
        if not count:
            raise RuntimeError(f'index.html has no data-stat="{key}" element')

    languages = sorted(
        (await s.languages).items(), reverse=True, key=lambda t: t[1].get("size")
    )[:LANGUAGE_ROWS]
    rows = ['      <ul class="loadout">']
    for lang, data in languages:
        prop = data.get("prop", 0)
        color = data.get("color") or "#000000"
        label = f"{prop:0.0f}%" if prop >= 1 else "&lt;1%"
        rows.append(
            f'        <li><span class="lang-name">{lang}</span>'
            f'<span class="lang-bar"><span class="fill" '
            f'style="width:{prop:0.2f}%;background:{color};color:{color}"></span></span>'
            f'<span class="lang-pct">{label}</span></li>'
        )
    rows.append("      </ul>")

    output, count = re.subn(
        r"(<!-- languages:start[^>]*-->\n).*?(\s*<!-- languages:end -->)",
        lambda m: m.group(1) + "\n".join(rows) + m.group(2),
        output,
        flags=re.DOTALL,
    )
    if not count:
        raise RuntimeError("index.html has no languages:start/end markers")

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(output)


def ordinal(position: int) -> str:
    """Render an arcade-style rank label: 1 -> 1ST, 22 -> 22ND."""
    if position % 100 in (11, 12, 13):
        suffix = "TH"
    else:
        suffix = {1: "ST", 2: "ND", 3: "RD"}.get(position % 10, "TH")
    return f"{position}{suffix}"


async def generate_hiscores(s: Stats) -> None:
    """
    Rebuild the high-score table in index.html from live star and fork counts.

    Rank and score were hand-maintained and drifted from reality. projects.json
    holds the editorial part (which projects appear, their labels and links);
    the score is stars * STARS_WEIGHT + forks * FORKS_WEIGHT, matching the
    formula the hand-written table used.
    :param s: Represents user's GitHub statistics
    """
    with open("projects.json", "r", encoding="utf-8") as f:
        projects = json.load(f)

    counts = await s.repo_counts
    ranked = []
    for order, project in enumerate(projects):
        repo = project.get("repo")
        if repo and repo not in counts:
            raise RuntimeError(
                f"projects.json maps {project['name']} to {repo}, which the stats "
                "fetch did not return (forks and contributed repos are excluded). "
                "Fix the mapping or set repo to null."
            )
        stats = counts.get(repo) if repo else None
        stars = stats["stars"] if stats else project.get("stars", 0)
        forks = stats["forks"] if stats else project.get("forks", 0)
        ranked.append((-(stars * STARS_WEIGHT + forks * FORKS_WEIGHT), order, project))
    ranked.sort()

    rows = ['      <ol class="hsc">']
    for position, (negative_score, _, project) in enumerate(ranked, start=1):
        classes = "hsc-row hsc-1" if position == 1 else "hsc-row"
        star = '<span class="live-star">&#9733;</span>' if project.get("live") else ""
        rows.append(
            f'        <li class="{classes}">\n'
            f'          <a href="{project["url"]}" target="_blank" rel="noopener">\n'
            f'            <span class="col-rank">{ordinal(position)}</span>\n'
            f'            <span class="col-score">{-negative_score:07,}</span>\n'
            f'            <span class="col-name">{star}{project["name"]}</span>\n'
            f'            <span class="col-init">{project["init"]}</span>\n'
            f"          </a>\n"
            f"        </li>"
        )
    rows.append("      </ol>")

    with open("index.html", "r", encoding="utf-8") as f:
        output = f.read()

    output, count = re.subn(
        r"(<!-- hiscores:start[^>]*-->\n).*?(\s*<!-- hiscores:end -->)",
        lambda m: m.group(1) + "\n".join(rows) + m.group(2),
        output,
        flags=re.DOTALL,
    )
    if not count:
        raise RuntimeError("index.html has no hiscores:start/end markers")

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(output)


async def generate_index_html(s: Stats) -> None:
    """
    Apply both index.html rewrites in sequence.
    :param s: Represents user's GitHub statistics
    """
    await generate_index(s)
    await generate_hiscores(s)


async def generate_llms_txt(s: Stats) -> None:
    """
    Refresh the aggregate metrics block in llms.txt.
    :param s: Represents user's GitHub statistics
    """
    with open("llms.txt", "r", encoding="utf-8") as f:
        output = f.read()

    languages = sorted(
        (await s.languages).items(), reverse=True, key=lambda t: t[1].get("size")
    )[:LANGUAGE_ROWS]
    shares = ", ".join(
        f"{lang} {data.get('prop', 0):0.0f}%"
        if data.get("prop", 0) >= 1
        else f"{lang} <1%"
        for lang, data in languages
    )
    lines = [
        f"- {await s.stargazers:,} stargazers across all owned repositories",
        f"- {await s.forks:,} forks",
        f"- {await s.total_contributions:,} lifetime contributions",
        f"- {len(await s.repos):,} repositories with contributions",
        f"- Languages by share of code: {shares}",
    ]

    output = _refresh_repo_counts_prose(output, await s.repo_counts)

    output, count = re.subn(
        r"(<!-- metrics:start[^>]*-->\n).*?(<!-- metrics:end -->)",
        lambda m: m.group(1) + "\n".join(lines) + "\n" + m.group(2),
        output,
        flags=re.DOTALL,
    )
    if not count:
        raise RuntimeError("llms.txt has no metrics:start/end markers")

    with open("llms.txt", "w", encoding="utf-8") as f:
        f.write(output)


################################################################################
# Main Function
################################################################################


async def main() -> None:
    """
    Generate all badges
    """
    access_token = os.getenv("ACCESS_TOKEN")
    if not access_token:
        access_token = os.getenv("GITHUB_TOKEN")
        if not access_token:
            raise Exception("A personal access token is required to proceed!")
    user = os.getenv("GITHUB_ACTOR")
    if user is None:
        raise RuntimeError("Environment variable GITHUB_ACTOR must be set.")
    exclude_repos = os.getenv("EXCLUDED")
    excluded_repos = (
        {x.strip() for x in exclude_repos.split(",")} if exclude_repos else None
    )
    exclude_langs = os.getenv("EXCLUDED_LANGS")
    excluded_langs = (
        {x.strip() for x in exclude_langs.split(",")} if exclude_langs else None
    )
    raw = os.getenv("EXCLUDE_CONTRIBUTED_REPOS", "")
    exclude_contributed_repos = bool(raw) and raw.strip().lower() != "false"
    async with aiohttp.ClientSession() as session:
        s = Stats(
            user,
            access_token,
            session,
            exclude_repos=excluded_repos,
            exclude_langs=excluded_langs,
            exclude_contributed_repos=exclude_contributed_repos,
        )
        await asyncio.gather(
            generate_languages(s),
            generate_overview(s),
            generate_index_html(s),
            generate_llms_txt(s),
        )


if __name__ == "__main__":
    asyncio.run(main())
