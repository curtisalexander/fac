# Working in this repo

Static site for the Fayetteville Athletic Club (FAC) group-exercise schedule.
`build.py` scrapes the FAC schedule and regenerates a self-contained
`index.html` + `data.json`. `index.html` is **generated** — never hand-edit it;
edit the template inside `build.py` and re-run `python3 build.py`.

## Les Mills class descriptions — required workflow

`descriptions.json` holds, per program:

- `summary` — **our own words**, the ONLY text published in tooltips.
- `source_text` — verbatim copy fetched from lesmills.com, **reference only**.
- `summary_source` — the `source_text` the current `summary` was reconciled
  against. Drives the **level-triggered** stale check: a summary is flagged
  stale whenever `source_text != summary_source`, and stays flagged every run
  until reconciled (it does **not** self-clear).

**When you (re)write a `summary`, you MUST reconcile it — do not hand-edit
`summary_source`/`summary_updated`:**

```bash
# 1. Edit the `summary` text in descriptions.json.
# 2. Mark it current (sets summary_source = source_text, bumps summary_updated):
python3 fetch_descriptions.py --reconcile BODYPUMP CEREMONY   # or no args = all
# 3. Rebuild:
python3 build.py
```

If you skip step 2, the program stays flagged stale on every refresh and the CI
notify job keeps re-opening the tracking issue — annoying but self-correcting.
The reminder is also printed by `fetch_descriptions.py` whenever it reports a
program that needs/❲may have a stale❳ summary.

Set `summary_source` to the **fetched `source_text`** (what the report shows),
even if you drew the prose from richer on-page copy — the automated comparison
must match what the fetcher produces, or it will flag stale forever.
`--reconcile` does this for you.

## CI

`.github/workflows/scrape.yml` runs weekly: `fetch_descriptions.py` → `build.py`
→ commit. A `notify` job opens/updates a GitHub issue (label
`automation-failure`) on build failure, snapshot fallback, or description drift.
`build.py` validates the parse and writes output only when healthy, so a broken
scrape leaves the published site untouched.
