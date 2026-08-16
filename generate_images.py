#!/usr/bin/python3

import asyncio
import os
import re

import aiohttp

from github_stats import Stats

LANGUAGE_ROWS = 8


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
            generate_index(s),
            generate_llms_txt(s),
        )


if __name__ == "__main__":
    asyncio.run(main())
