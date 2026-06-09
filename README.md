# FAC Group Exercise Schedule

A small scraper + static site that reformats the **Fayetteville Athletic Club**
group-exercise schedule into an attractive, easy-to-browse page you can publish on
GitHub Pages.

Data is scraped from the official FAC page:
<https://www.fayac.com/strength-and-cardio/>

This is an **unofficial** reformatting for easier browsing. FAC is the source of truth.

## What you get

- **`build.py`** — scrapes the schedule table, normalizes the data, and regenerates the
  site. Python 3, standard library only (no `pip install`).
- **`index.html`** — the published page. Fully self-contained: data, CSS, and JS are all
  embedded, so it works on GitHub Pages *and* when opened directly from disk.
- **`data.json`** — machine-readable schedule + a "last updated" timestamp.
- **`.github/workflows/scrape.yml`** — weekly job that re-scrapes and auto-commits.
- **`fayac_raw.html`** — the most recent raw page snapshot, used as an offline fallback.

## The views

Tabs across the top switch between:

- **By Day** — a calendar-like 7-column grid (Mon→Sun) where every start time is a row,
  so the same time lines up across all days. Within a time slot, each activity type gets
  a fixed lane, so a recurring class (e.g. noon WAYMO HYROX, or Fast Feed across the week)
  lines up horizontally across the days it runs.
- **By Class** — grouped by class type (e.g. all *Les Mills BODYPUMP* together). Related
  variants are merged into families (Yoga, Cycling, Pilates, Barre, BODYPUMP…); each
  session still shows its specific style. Click a group header to collapse it.
- **By Instructor** — grouped alphabetically by instructor, for when you have a favorite.
  Co-taught classes appear under each instructor.
- **By Location** — grouped by studio/location (Large, Small, Cycle, WAYMO, Pool, Tennis,
  Court), then broken down by day, so you can see everything happening in one space.

Each activity type has its own **color** (shown as the card's left edge everywhere). A
**Class colors** key at the top lists every type — click one to spotlight all of that
class's sessions across every tab (e.g. find every BODYPUMP at a glance).

A **Highlight** bar (persistent across tabs) lets you spotlight **Morning / Midday /
Evening** classes — matching cards stay vivid while the rest gray out. It combines with
the color key (e.g. morning + BODYPUMP).

- Morning: before 11:00 AM
- Midday: 11:00 AM – 4:00 PM (includes Noon)
- Evening: 4:00 PM and later

Classes marked with a **Reserve \*** badge require a reservation.

## Refreshing the schedule

### Manually

```bash
python3 build.py
```

This fetches the live FAC page, rewrites `index.html` + `data.json`, and refreshes the
`fayac_raw.html` snapshot. Commit and push to update the live site:

```bash
git add index.html data.json fayac_raw.html
git commit -m "Refresh schedule"
git push
```

If the network fetch fails, the script automatically falls back to the local
`fayac_raw.html` snapshot so a rebuild still works.

### Automatically

`.github/workflows/scrape.yml` runs every **Monday** (and on-demand from the **Actions**
tab via *Run workflow*). It runs `build.py` and commits any changes. Change the `cron:`
line to adjust the schedule.

## Publishing on GitHub Pages

1. Push this folder to a GitHub repository.
2. Repo **Settings → Pages**.
3. Under **Build and deployment**, set **Source: Deploy from a branch**, choose your
   default branch and the **`/ (root)`** folder, then **Save**.
4. Your site appears at `https://<user>.github.io/<repo>/` — `index.html` is served
   automatically.

> The weekly Action needs write access to push. That's covered by the workflow's
> `permissions: contents: write`. If pushes are blocked, check
> **Settings → Actions → General → Workflow permissions** and allow read/write.

## Credit

Schedule © Fayetteville Athletic Club. Source: <https://www.fayac.com/strength-and-cardio/>
