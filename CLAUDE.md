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

## FAC (non-Les-Mills) class descriptions — same workflow, FAC-sourced

`fac_descriptions.json` does for the club's own classes what `descriptions.json`
does for Les Mills, with the **identical summary/reconcile contract** — only the
source differs. `fetch_fac_descriptions.py` scrapes each class's bullet list off
the FAC Strength & Cardio page into `source_bullets` (verbatim, reference-only),
and **you (the agent) write the published `summary` from those bullets**. The
tooltip footer credits "Fayetteville Athletic Club" and links back to that page.

Records are keyed by **build.py `family`** (e.g. `BARRE`, `DANCEFIIT`), and the
`CLASSES` registry in `fetch_fac_descriptions.py` anchors each family's bullet
list by either a text `heading` or, for sections that head with a brand logo
image instead of a heading (e.g. WAYMO → `marker: "of-cover waymo"`), an HTML
`marker`. A class only gets the FAC tooltip if Les Mills doesn't already cover it
(Les Mills wins for e.g. `SHAPES`). The stale check compares `source_bullets`
against `summary_source` (a list), level-triggered just like Les Mills.

```bash
python3 fetch_fac_descriptions.py            # scrape bullets into fac_descriptions.json
# ...edit each `summary` from `source_bullets`, then:
python3 fetch_fac_descriptions.py --reconcile DANCEFIIT BARRE   # or no args = all
python3 build.py
```

To add a class, add a `family -> {heading|marker}` entry to `CLASSES`, re-run the
fetcher, write a summary, reconcile, rebuild.

## CI

`.github/workflows/scrape.yml` runs weekly: `fetch_descriptions.py` →
`fetch_fac_descriptions.py` → `build.py` → commit. A `notify` job opens/updates a
GitHub issue (label `automation-failure`) on build failure, snapshot fallback, or
Les Mills / FAC description drift. `build.py` validates the parse and writes
output only when healthy, so a broken scrape leaves the published site untouched.
The live FAC fetch in `build.py` retries (and rejects a 200 that's missing the
schedule table) before falling back to the committed snapshot, so a transient
truncated/blocked response no longer fails the run.
