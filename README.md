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
| `index.html` | elements with `data-stat="..."`, and the list between the `languages:start/end` comments |
| `llms.txt` | the block between the `metrics:start/end` comments |

Do not hand-edit those regions; the next run overwrites them. Everything else in
`index.html` and `llms.txt`, including the high-score table, is hand-maintained.
Removing a `data-stat` attribute or a marker comment fails the run loudly rather
than silently skipping the update.

Run it locally with:

```sh
GITHUB_TOKEN="$(gh auth token)" GITHUB_ACTOR=boredland \
  EXCLUDED=boredland/boredland EXCLUDE_CONTRIBUTED_REPOS=false \
  uv run --with aiohttp --with requests python generate_images.py
```
