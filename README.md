![](https://raw.githubusercontent.com/boredland/boredland/master/generated/overview.svg#gh-dark-mode-only)
![](https://raw.githubusercontent.com/boredland/boredland/master/generated/overview.svg#gh-light-mode-only)
![](https://raw.githubusercontent.com/boredland/boredland/master/generated/languages.svg#gh-dark-mode-only)
![](https://raw.githubusercontent.com/boredland/boredland/master/generated/languages.svg#gh-light-mode-only)

---

## Generated values

`generate_images.py` runs daily (`.github/workflows/main.yml`) and owns every
number on the site. It writes four things from one API fetch:

| Output | What is replaced |
| --- | --- |
| `generated/overview.svg`, `generated/languages.svg` | whole file, from `templates/` |
| `index.html` | elements with `data-stat="..."`, the list between the `languages:start/end` comments, and the table between the `hiscores:start/end` comments |
| `llms.txt` | the block between the `metrics:start/end` comments, and the "N stars, M forks" clauses in the project prose |

Do not hand-edit those regions; the next run overwrites them. Everything else in
`index.html` and `llms.txt` is hand-maintained.

The high-score table is generated from `projects.json`, which holds the editorial
part: which projects appear, their label, link, initials and live-star. Score is
`stars * 1000 + forks * 100` and ordering follows it, so ranks track reality
instead of drifting. Entries whose `repo` is absent from the stats fetch (forks
and contributed repos are excluded by the query) fail the run loudly; set `repo`
to `null` and give explicit `stars`/`forks` for those.
Removing a `data-stat` attribute or a marker comment fails the run loudly rather
than silently skipping the update.

A GitHub API call that cannot be answered now aborts the run instead of
publishing zeros, so a failed workflow means "check the token or the API", not
"the site has no stars". Transient rate limits and 5xx responses are still
retried before giving up.

Run it locally with:

```sh
GITHUB_TOKEN="$(gh auth token)" GITHUB_ACTOR=boredland \
  EXCLUDED=boredland/boredland EXCLUDE_CONTRIBUTED_REPOS=false \
  uv run --with aiohttp python generate_images.py
```

| Variable | Purpose |
| --- | --- |
| `GITHUB_TOKEN` or `ACCESS_TOKEN` | personal access token; required |
| `GITHUB_ACTOR` | user to collect stats for; required |
| `EXCLUDED` | comma-separated `owner/repo` list to skip |
| `EXCLUDED_LANGS` | comma-separated language names to skip |
| `EXCLUDE_CONTRIBUTED_REPOS` | any value but `false` counts owned repos only |

Run the tests with:

```sh
uv run --with pytest --with aiohttp python -m pytest -q
```
