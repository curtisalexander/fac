#!/usr/bin/env python3
"""
FAC Group Exercise Schedule — scraper + static-site generator.

Scrapes the group-exercise schedule table from the Fayetteville Athletic Club
"Strength & Cardio" page, normalizes the (messy) data, and regenerates a single
self-contained `index.html` plus a machine-readable `data.json`.

Usage:
    python3 build.py            # fetch live page, rebuild index.html + data.json

Stdlib only — no `pip install` required.
"""

import html
import json
import re
import sys
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

SOURCE_URL = "https://www.fayac.com/strength-and-cardio/"
HERE = Path(__file__).resolve().parent
RAW_FALLBACK = HERE / "fayac_raw.html"
DATA_JSON = HERE / "data.json"
INDEX_HTML = HERE / "index.html"
TABLE_ID = "tablepress-3"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# --------------------------------------------------------------------------- #
# 1. Fetch
# --------------------------------------------------------------------------- #
def fetch_html() -> str:
    """Fetch the live page; fall back to the local raw HTML snapshot offline."""
    try:
        req = urllib.request.Request(SOURCE_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read().decode("utf-8", "replace")
        print(f"Fetched live page ({len(data):,} bytes) from {SOURCE_URL}")
        # Keep a fresh snapshot for offline rebuilds.
        try:
            RAW_FALLBACK.write_text(data, encoding="utf-8")
        except OSError:
            pass
        return data
    except Exception as exc:  # noqa: BLE001 — any network error → fall back
        if RAW_FALLBACK.exists():
            print(f"Live fetch failed ({exc}); using local {RAW_FALLBACK.name}")
            return RAW_FALLBACK.read_text(encoding="utf-8")
        print(f"ERROR: live fetch failed ({exc}) and no local fallback exists.")
        sys.exit(1)


# --------------------------------------------------------------------------- #
# 2. Parse the TablePress table
# --------------------------------------------------------------------------- #
class ScheduleTableParser(HTMLParser):
    """Pull the 5 cells of every <tr> inside the target <table id=...>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.in_cell = False
        self._depth = 0
        self.rows: list[list[str]] = []
        self._cur: list[str] | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table" and attrs.get("id") == TABLE_ID:
            self.in_table = True
            self._depth = 1
            return
        if not self.in_table:
            return
        if tag == "table":
            self._depth += 1
        elif tag == "tr":
            self._cur = []
        elif tag in ("td", "th"):
            self.in_cell = True
            self._buf = []

    def handle_endtag(self, tag):
        if not self.in_table:
            return
        if tag in ("td", "th") and self.in_cell:
            self.in_cell = False
            if self._cur is not None:
                self._cur.append("".join(self._buf).strip())
        elif tag == "tr":
            if self._cur:
                self.rows.append(self._cur)
            self._cur = None
        elif tag == "table":
            self._depth -= 1
            if self._depth == 0:
                self.in_table = False

    def handle_data(self, data):
        if self.in_cell:
            self._buf.append(data)


def parse_rows(page: str) -> list[list[str]]:
    p = ScheduleTableParser()
    p.feed(page)
    # Drop the header row (DAY/TIME/CLASS/INSTRUCTOR/STUDIO).
    rows = [r for r in p.rows if len(r) >= 5]
    if rows and rows[0][0].strip().upper() == "DAY":
        rows = rows[1:]
    return rows


# --------------------------------------------------------------------------- #
# 3. Normalize
# --------------------------------------------------------------------------- #
def tidy(s: str) -> str:
    """Collapse internal whitespace and strip."""
    return re.sub(r"\s+", " ", s).strip()


DAYS = {
    "MON": ("Monday", 0),
    "TUE": ("Tuesday", 1),
    "WED": ("Wednesday", 2),
    "THU": ("Thursday", 3),
    "FRI": ("Friday", 4),
    "SAT": ("Saturday", 5),
    "SUN": ("Sunday", 6),
}


def parse_day(raw: str):
    key = tidy(raw).upper()[:3]
    full, idx = DAYS.get(key, (tidy(raw).title(), 99))
    return key, full, idx


def parse_time(raw: str):
    """Return (minutes_since_midnight, clean_label). Handles 'Noon' and bare 12:15."""
    t = tidy(raw)
    if not t:
        return 9999, ""
    if t.lower() == "noon":
        return 12 * 60, "Noon"
    m = re.match(r"^(\d{1,2}):(\d{2})\s*([AaPp][Mm])?", t)
    if not m:
        return 9999, t
    hour = int(m.group(1))
    minute = int(m.group(2))
    mer = (m.group(3) or "").upper()
    if not mer:
        # Bare time on this page (THU 12:15) is a lunch-block PM entry.
        mer = "PM" if hour == 12 or hour < 6 else "AM"
    h24 = hour % 12
    if mer == "PM":
        h24 += 12
    minutes = h24 * 60 + minute
    label = f"{hour}:{minute:02d} {mer}"
    if minutes == 12 * 60:
        label = "Noon"
    return minutes, label


def bucket_for(minutes: int) -> str:
    if minutes < 11 * 60:
        return "morning"
    if minutes < 16 * 60:
        return "midday"
    return "evening"


STUDIO_FIX = {
    "CYLE": "Cycle",
    "CYCLE": "Cycle",
    "WAYMO": "WAYMO",
    "LARGE": "Large",
    "SMALL": "Small",
    "POOL": "Pool",
    "TENNIS": "Tennis",
    "COURT": "Court",
}


def normalize_studio(raw: str) -> str:
    t = tidy(raw)
    return STUDIO_FIX.get(t.upper(), t)


# Explicit class mapping: normalized-upper raw name -> (family, familyLabel, displayName)
# (the trailing reservation '*' is stripped before lookup).
_M = {
    # --- BODYPUMP ---------------------------------------------------------- #
    "LM BODY PUMP": ("BODYPUMP", "Les Mills BODYPUMP", "Les Mills BODYPUMP"),
    "LM BP HEAVY": ("BODYPUMP", "Les Mills BODYPUMP", "Les Mills BODYPUMP — Heavy"),
    "LM PUMP EXPRESS/LM CORE": ("BODYPUMP", "Les Mills BODYPUMP", "Les Mills BODYPUMP Express + CORE"),
    "LM PUMP EXPRESS/CORE": ("BODYPUMP", "Les Mills BODYPUMP", "Les Mills BODYPUMP Express + CORE"),
    # --- BODYATTACK -------------------------------------------------------- #
    "LM BODY ATTACK": ("BODYATTACK", "Les Mills BODYATTACK", "Les Mills BODYATTACK"),
    "LM ATTACK 45": ("BODYATTACK", "Les Mills BODYATTACK", "Les Mills BODYATTACK (45)"),
    # --- CYCLING ----------------------------------------------------------- #
    "LM RPM": ("CYCLING", "Cycling", "Les Mills RPM"),
    "LM SPRINT": ("CYCLING", "Cycling", "Les Mills SPRINT"),
    "LM SPRINT (30MIN)": ("CYCLING", "Cycling", "Les Mills SPRINT (30 min)"),
    "PRECISION CYCLE": ("CYCLING", "Cycling", "Precision Cycling"),
    "PRECISION CYCLING": ("CYCLING", "Cycling", "Precision Cycling"),
    # --- Other Les Mills --------------------------------------------------- #
    "LM GRIT": ("GRIT", "Les Mills GRIT", "Les Mills GRIT"),
    "LM CORE": ("CORE", "Les Mills CORE", "Les Mills CORE"),
    "LM SHAPES": ("SHAPES", "Les Mills SHAPES", "Les Mills SHAPES"),
    "LM SHAPES (HOT)": ("SHAPES", "Les Mills SHAPES", "Les Mills SHAPES (Hot)"),
    "LM STRENGTH DEVELOPMENT": ("STRENGTH_DEV", "Les Mills Strength Development", "Les Mills Strength Development"),
    "LM TONE": ("TONE", "Les Mills TONE", "Les Mills TONE"),
    "LM CEREMONY H": ("CEREMONY", "Les Mills Ceremony", "Les Mills Ceremony — Hyrox"),
    "LM CEREMONY S": ("CEREMONY", "Les Mills Ceremony", "Les Mills Ceremony — Stations"),
    # --- WAYMO HYROX ------------------------------------------------------- #
    "WAYMO HYROX": ("WAYMO_HYROX", "WAYMO HYROX", "WAYMO HYROX"),
    # --- Fast Feed --------------------------------------------------------- #
    "FAST FEED": ("FAST_FEED", "Fast Feed", "Fast Feed"),
    # --- DanceFIIT --------------------------------------------------------- #
    "DANCEFIIT": ("DANCEFIIT", "DanceFIIT", "DanceFIIT"),
    # --- Young At Heart ---------------------------------------------------- #
    "YAH": ("YAH", "Young At Heart", "Young At Heart"),
    # --- Yoga (merged) ----------------------------------------------------- #
    "FLOW YOGA": ("YOGA", "Yoga", "Flow Yoga"),
    "WARM FLOW YOGA": ("YOGA", "Yoga", "Warm Flow Yoga"),
    "WARM MORNING FLOW": ("YOGA", "Yoga", "Warm Morning Flow"),
    "HOT FLOW YOGA": ("YOGA", "Yoga", "Hot Flow Yoga"),
    "HOT FLOW YOGA (90MIN)": ("YOGA", "Yoga", "Hot Flow Yoga (90 min)"),
    "HOT YOGA FLOW": ("YOGA", "Yoga", "Hot Yoga Flow"),
    "HOT POWER FLOW": ("YOGA", "Yoga", "Hot Power Flow"),
    # --- Pilates (merged) -------------------------------------------------- #
    "PILATES PLUS": ("PILATES", "Pilates", "Pilates Plus"),
    "MAT PILATES": ("PILATES", "Pilates", "Mat Pilates"),
    # --- Hot Cardio Sculpt ------------------------------------------------- #
    "HOT CARDIO SCULPT": ("HOT_CARDIO_SCULPT", "Hot Cardio Sculpt", "Hot Cardio Sculpt"),
    "HOT CARDIO SCULPT (45)": ("HOT_CARDIO_SCULPT", "Hot Cardio Sculpt", "Hot Cardio Sculpt (45)"),
    # --- Barre (merged) ---------------------------------------------------- #
    "BARRE": ("BARRE", "Barre", "Barre"),
    "BARRE INTENSITY": ("BARRE", "Barre", "Barre Intensity"),
    # --- Aqua -------------------------------------------------------------- #
    "AQUA AEROBICS": ("AQUA", "Aqua Aerobics", "Aqua Aerobics"),
    "AQUA AEROBICS (75MIN)": ("AQUA", "Aqua Aerobics", "Aqua Aerobics (75 min)"),
    # --- Full Body Tone ---------------------------------------------------- #
    "FULL BODY TONE": ("FULL_BODY_TONE", "Full Body Tone", "Full Body Tone"),
}


def map_class(raw: str):
    """Return (family, familyLabel, displayName, reservationRequired)."""
    cleaned = tidy(raw)
    reservation = cleaned.endswith("*")
    if reservation:
        cleaned = cleaned.rstrip("*").strip()
    key = cleaned.upper()
    if key in _M:
        fam, label, disp = _M[key]
    else:
        # Fallback: unmapped class becomes its own family, shown as-is.
        fam = key
        label = cleaned
        disp = cleaned
    return fam, label, disp, reservation


def normalize(rows: list[list[str]]) -> list[dict]:
    out = []
    for r in rows:
        day_raw, time_raw, class_raw, instr_raw, studio_raw = r[:5]
        day, day_full, day_idx = parse_day(day_raw)
        minutes, time_label = parse_time(time_raw)
        family, family_label, display_name, reservation = map_class(class_raw)
        instr = tidy(instr_raw)
        # Split co-taught entries ("Brooke R./Kate", "Kim P/Shannon") for the
        # instructor view; keep the full string for display.
        instructors = [p.strip() for p in re.split(r"\s*/\s*", instr) if p.strip()] or [instr]
        out.append(
            {
                "day": day,
                "dayFull": day_full,
                "dayIdx": day_idx,
                "time": time_label,
                "timeMin": minutes,
                "bucket": bucket_for(minutes),
                "classRaw": tidy(class_raw).rstrip("*").strip(),
                "family": family,
                "familyLabel": family_label,
                "displayName": display_name,
                "reservationRequired": reservation,
                "instructor": instr,
                "instructors": instructors,
                "studio": normalize_studio(studio_raw),
            }
        )
    out.sort(key=lambda c: (c["dayIdx"], c["timeMin"], c["familyLabel"]))
    return out


# --------------------------------------------------------------------------- #
# 3b. Per-family colors (so each activity type has a stable, distinct color)
# --------------------------------------------------------------------------- #
# Bright, distinct hues tuned to read well as left-borders/swatches on the dark
# theme. There are ~19 families, comfortably under the palette size.
PALETTE = [
    "#ef4444", "#f97316", "#f59e0b", "#eab308", "#84cc16", "#22c55e",
    "#10b981", "#14b8a6", "#06b6d4", "#0ea5e9", "#3b82f6", "#6366f1",
    "#8b5cf6", "#a855f7", "#d946ef", "#ec4899", "#f43f5e", "#fb7185",
    "#34d399", "#facc15", "#a3e635", "#2dd4bf",
]


def assign_family_colors(classes: list[dict]) -> list[dict]:
    """Assign each family a color and attach it to every class; return a legend."""
    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    for c in classes:
        counts[c["family"]] = counts.get(c["family"], 0) + 1
        labels[c["family"]] = c["familyLabel"]
    # Assign colors busiest-first, so the most common types get the leading colors.
    order = sorted(counts, key=lambda f: (-counts[f], labels[f].lower()))
    color_of = {f: PALETTE[i % len(PALETTE)] for i, f in enumerate(order)}
    for c in classes:
        c["color"] = color_of[c["family"]]
    # ...but present the legend alphabetically by label for easy scanning.
    legend_order = sorted(counts, key=lambda f: labels[f].lower())
    return [
        {"family": f, "label": labels[f], "color": color_of[f], "count": counts[f]}
        for f in legend_order
    ]


# --------------------------------------------------------------------------- #
# 4. Emit
# --------------------------------------------------------------------------- #
def render_index(payload: dict, updated_human: str) -> str:
    data_js = json.dumps(payload, ensure_ascii=False, indent=2)
    return INDEX_TEMPLATE.replace("/*__DATA__*/", data_js).replace(
        "__UPDATED__", html.escape(updated_human)
    )


def main() -> None:
    page = fetch_html()
    rows = parse_rows(page)
    if not rows:
        print(f"ERROR: no rows found in table #{TABLE_ID}.")
        sys.exit(1)
    classes = normalize(rows)
    families = assign_family_colors(classes)

    now = datetime.now()
    payload = {
        "updated": now.isoformat(timespec="seconds"),
        "updatedHuman": now.strftime("%B %-d, %Y at %-I:%M %p"),
        "source": SOURCE_URL,
        "families": families,
        "classes": classes,
    }

    DATA_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    INDEX_HTML.write_text(
        render_index(payload, payload["updatedHuman"]), encoding="utf-8"
    )

    fams = sorted({c["familyLabel"] for c in classes})
    instrs = sorted({c["instructor"] for c in classes})
    print(f"Parsed {len(classes)} classes.")
    print(f"  {len(fams)} class families, {len(instrs)} instructors.")
    print(f"  Wrote {DATA_JSON.name} and {INDEX_HTML.name}.")
    print(f"  Last updated: {payload['updatedHuman']}")


# --------------------------------------------------------------------------- #
# index.html template (self-contained: CSS + JS + data all inline)
# --------------------------------------------------------------------------- #
INDEX_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FAC Group Exercise Schedule</title>
<style>
  :root {
    --bg: #0f1722;
    --panel: #18222f;
    --panel-2: #1f2c3c;
    --line: #2b3a4d;
    --text: #e8eef5;
    --muted: #9fb0c3;
    --accent: #2f80ed;
    --accent-2: #56ccf2;
    --morning: #f2c94c;
    --midday: #6fcf97;
    --evening: #bb6bd9;
    --shadow: 0 6px 20px rgba(0,0,0,.35);
    --radius: 14px;
    --band: #212e3e;          /* alternating time-slot band on the day grid */
    --divider: #4a5f82;       /* heavier line between time slots */
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; }
  body {
    background: linear-gradient(180deg, #0c131c 0%, var(--bg) 100%);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.45;
    -webkit-font-smoothing: antialiased;
    padding-bottom: 40px;
  }
  a { color: var(--accent-2); }

  header.site {
    position: sticky; top: 0; z-index: 20;
    background: rgba(15,23,34,.92);
    backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--line);
    padding: 18px 20px 0;
  }
  .head-row { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; max-width: 1200px; margin: 0 auto; }
  h1 { font-size: 1.5rem; margin: 0; letter-spacing: .2px; }
  .head-row .sub { color: var(--muted); font-size: .9rem; }
  .print-btn {
    margin-left: auto; align-self: center;
    display: inline-flex; align-items: center; gap: 7px;
    border: 1px solid var(--line); background: var(--panel); color: var(--text);
    padding: 7px 14px; border-radius: 10px; font-size: .85rem; cursor: pointer;
    transition: background .15s, border-color .15s;
  }
  .print-btn:hover { background: var(--panel-2); border-color: var(--accent); }
  .print-btn svg { width: 16px; height: 16px; display: block; }

  .tabs { display: flex; gap: 6px; max-width: 1200px; margin: 14px auto 0; flex-wrap: wrap; }
  .tab {
    appearance: none; border: 1px solid var(--line); background: var(--panel);
    color: var(--text); padding: 9px 16px; border-radius: 10px 10px 0 0;
    font-size: .92rem; cursor: pointer; border-bottom: none; transition: background .15s;
  }
  .tab:hover { background: var(--panel-2); }
  .tab.active { background: var(--accent); border-color: var(--accent); font-weight: 600; }

  .toolbar {
    max-width: 1200px; margin: 0 auto; padding: 12px 0 16px;
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
  }
  .tod-group { display: flex; gap: 6px; align-items: center; }
  .tod-label { color: var(--muted); font-size: .82rem; margin-right: 2px; }
  .chip {
    appearance: none; border: 1px solid var(--line); background: var(--panel);
    color: var(--text); padding: 6px 13px; border-radius: 999px; font-size: .85rem;
    cursor: pointer; transition: all .15s;
  }
  .chip:hover { border-color: var(--accent); }
  .chip.active { background: var(--accent); border-color: var(--accent); font-weight: 600; }
  .chip[data-tod="morning"].active { background: var(--morning); color: #2a2200; border-color: var(--morning); }
  .chip[data-tod="midday"].active  { background: var(--midday);  color: #08240f; border-color: var(--midday); }
  .chip[data-tod="evening"].active { background: var(--evening); color: #2a0830; border-color: var(--evening); }
  .legend { color: var(--muted); font-size: .78rem; margin-left: auto; }
  .legend b { color: var(--text); font-weight: 600; }
  .resv-note {
    display: inline-flex; align-items: center; gap: 6px; font-size: .82rem;
    color: var(--text); background: var(--panel); border: 1px solid var(--accent);
    border-radius: 999px; padding: 4px 12px;
  }
  .resv-note b { color: var(--accent-2); font-size: 1rem; line-height: 1; }

  main { max-width: 1200px; margin: 0 auto; padding: 0 20px; }
  .view { display: none; }
  .view.active { display: block; }

  /* ---- By Day: aligned time grid (desktop) ---- */
  .timegrid-wrap {
    overflow-x: auto; -webkit-overflow-scrolling: touch;
    border: 1px solid var(--line); border-radius: var(--radius);
  }
  .timegrid {
    border-collapse: collapse; table-layout: fixed; width: 100%; min-width: 720px;
    background: var(--panel);
  }
  .timegrid th, .timegrid td { border: 1px solid var(--line); vertical-align: top; }
  .tg-corner, .tg-dayhead { background: var(--panel-2); }
  .tg-corner { width: 72px; position: sticky; left: 0; z-index: 3; }
  .tg-dayhead { text-align: center; font-weight: 600; font-size: .85rem; padding: 9px 4px; }
  .tg-time {
    width: 72px; position: sticky; left: 0; z-index: 1; background: var(--panel-2);
    font-weight: 700; font-size: .76rem; color: var(--accent-2);
    padding: 6px 8px; text-align: right; vertical-align: middle; white-space: nowrap;
  }
  .tg-cell { padding: 5px; }
  .tg-cell > * + * { margin-top: 5px; }   /* space between stacked cards in a cell */
  /* Alternate time-slot banding so each time block reads as one unit. */
  .tg-time.band, .tg-cell.band { background: var(--band); }
  /* Heavier divider line at each time-slot boundary. */
  .tg-time.slot-top, .tg-cell.slot-top { border-top: 2px solid var(--divider); }
  .card-compact { padding: 6px 8px; }
  .card-compact .cname { font-size: .82rem; font-weight: 600; }
  .card-compact .meta { color: var(--muted); font-size: .72rem; margin-top: 2px; }

  /* ---- By Day: stacked sections (mobile) ---- */
  .day-stack { display: none; }
  .day-block { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; margin-bottom: 12px; }
  .day-block > h3 { margin: 0; padding: 10px 12px; font-size: 1rem; background: var(--panel-2); border-bottom: 1px solid var(--line); }
  .db-body { padding: 8px; display: flex; flex-direction: column; gap: 8px; }
  .db-body .empty { color: var(--muted); font-size: .85rem; text-align: center; padding: 12px; }

  /* ---- Cards ---- */
  .card {
    background: var(--panel-2); border: 1px solid var(--line); border-left: 4px solid var(--accent);
    border-radius: 10px; padding: 8px 10px; transition: opacity .18s, filter .18s, box-shadow .18s;
  }
  .card .time { font-weight: 700; font-size: .9rem; }
  .card .cname { font-size: .9rem; margin-top: 1px; }
  .card .meta { color: var(--muted); font-size: .78rem; margin-top: 3px; }
  .card .badge {
    display: inline-block; font-size: .66rem; font-weight: 700; letter-spacing: .3px;
    text-transform: uppercase; color: var(--accent-2); border: 1px solid var(--accent);
    border-radius: 6px; padding: 0 5px; margin-top: 4px;
  }
  /* Card left-border color = activity type (set inline per family). */

  /* highlight mode (time-of-day filter OR color-key family selection) */
  body.hl-filtering .card { opacity: .2; filter: grayscale(.65); }
  body.hl-filtering .card.hl-match { opacity: 1; filter: none; box-shadow: var(--shadow); }
  body.hl-filtering .group.hl-hidden { display: none; }
  .tg-empty { background: var(--panel); }

  /* ---- Grouped views (class / instructor) ---- */
  .groups { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; }
  .group { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; }
  .group > h3 {
    margin: 0; padding: 11px 14px; font-size: 1rem; cursor: pointer;
    background: var(--panel-2); border-bottom: 1px solid var(--line);
    display: flex; justify-content: space-between; align-items: center; gap: 8px;
  }
  .group > h3 .count { color: var(--muted); font-size: .8rem; font-weight: 400; }
  .group .rows { display: flex; flex-direction: column; }
  .group.collapsed .rows { display: none; }
  .group > h3 .caret { color: var(--muted); font-size: .75rem; margin-right: 6px; }
  .daysep {
    padding: 6px 14px; background: var(--panel-2); color: var(--accent-2);
    font-weight: 700; font-size: .74rem; letter-spacing: .5px; text-transform: uppercase;
    border-bottom: 1px solid var(--line);
  }
  .row {
    display: grid; grid-template-columns: 52px 1fr; gap: 10px;
    padding: 8px 14px 8px 11px; border-bottom: 1px solid var(--line);
    border-left: 4px solid var(--line); transition: opacity .18s, filter .18s;
  }
  .row:last-child { border-bottom: none; }
  .row .rday { font-weight: 700; font-size: .82rem; color: var(--accent-2); }
  .row .rmain .rtime { font-weight: 600; font-size: .86rem; }
  .row .rmain .rsub { color: var(--muted); font-size: .78rem; }
  body.hl-filtering .row { opacity: .2; filter: grayscale(.65); }
  body.hl-filtering .row.hl-match { opacity: 1; filter: none; }

  /* ---- Color key (activity-type legend) ---- */
  .colorkey {
    max-width: 1200px; margin: 0 auto 12px; padding: 0 14px;
    background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
  }
  .colorkey > summary {
    list-style: none; cursor: pointer; padding: 11px 0; font-weight: 600; font-size: .92rem;
    display: flex; align-items: center; gap: 8px;
  }
  .colorkey > summary::-webkit-details-marker { display: none; }
  .colorkey > summary::before { content: "▾"; color: var(--muted); font-size: .8rem; }
  .colorkey:not([open]) > summary::before { content: "▸"; }
  .colorkey > summary .hint { color: var(--muted); font-weight: 400; font-size: .8rem; }
  .key-items {
    display: flex; flex-wrap: wrap; gap: 6px; padding: 2px 0 12px;
    max-height: 32vh; overflow-y: auto;
  }
  .key-item {
    display: inline-flex; align-items: center; gap: 7px; cursor: pointer;
    border: 1px solid var(--line); background: var(--panel-2); color: var(--text);
    border-radius: 999px; padding: 4px 11px 4px 8px; font-size: .8rem; transition: border-color .15s;
  }
  .key-item:hover { border-color: var(--muted); }
  .key-item.active { border-color: var(--text); box-shadow: 0 0 0 1px var(--text) inset; }
  .key-item .swatch { width: 12px; height: 12px; border-radius: 3px; flex: none; }
  .key-item .kcount { color: var(--muted); font-size: .72rem; }

  footer.site {
    max-width: 1200px; margin: 30px auto 0; padding: 18px 20px;
    border-top: 1px solid var(--line); color: var(--muted); font-size: .82rem;
  }
  footer.site b { color: var(--text); }
  footer .note { margin-top: 6px; }

  @media (max-width: 760px) {
    .timegrid-wrap { display: none; }
    .day-stack { display: block; }
    .legend { width: 100%; margin: 4px 0 0; order: 5; }
    h1 { font-size: 1.25rem; }
    .groups { grid-template-columns: 1fr; }
    header.site { padding: 14px 14px 0; }
    main { padding: 0 14px; }
  }

  /* ---- Print (landscape, fits the page at any orientation) ---- */
  @media print {
    @page { size: landscape; margin: .35in; }
    * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    html, body { background: #fff; color: #000; padding: 0; }
    body { font-size: 11px; }
    header.site { position: static; background: #fff; border: none; padding: 0 0 6px; }
    h1 { font-size: 16px; }
    .head-row { margin-bottom: 2px; }
    .head-row .sub { font-size: 10px; }
    .tabs, .toolbar, .print-btn { display: none !important; }
    main { max-width: none; padding: 0; }
    a { color: #000; text-decoration: none; }

    /* Print a compact color key so the colors stay meaningful on paper. */
    .colorkey { display: block !important; max-width: none; margin: 0 0 6px; padding: 0; border: none; }
    .colorkey > summary { padding: 1px 0; font-size: 10px; font-weight: 700; list-style: none; pointer-events: none; }
    .colorkey > summary::before, .colorkey > summary .hint { display: none; }
    .colorkey > summary::after { content: ":"; }
    .key-items { display: flex !important; max-height: none !important; overflow: visible !important; padding: 0; gap: 2px 9px; }
    .key-item { border: none !important; background: none !important; box-shadow: none !important; padding: 0; font-size: 9px; }
    .key-item .swatch { width: 9px; height: 9px; }
    .key-item .kcount { display: none; }

    /* Always print the aligned grid for By Day (not the mobile stack). */
    .timegrid-wrap { display: block !important; overflow: visible !important; border: none; }
    .day-stack { display: none !important; }
    .timegrid { min-width: 0 !important; width: 100% !important; background: #fff; }
    .timegrid th, .timegrid td { border-color: #bbb !important; }
    /* Repeat the day-name header on every page; keep each time block intact. */
    .timegrid thead { display: table-header-group; }
    .timegrid tbody.slot { break-inside: avoid; }
    /* Safari chunking: each chunk table starts a new page (header at its top). */
    .timegrid.page-break { break-before: page; }
    .tg-corner, .tg-time { width: 44px !important; }
    .tg-corner, .tg-dayhead, .tg-time, .tg-cell, .group, .day-block, .card, .row { background: #fff !important; color: #000 !important; }
    .tg-dayhead, .tg-time, .group > h3, .day-block > h3, .daysep { background: #eee !important; color: #000 !important; }
    .tg-cell.band { background: #f1f1f1 !important; }
    .tg-time.band { background: #e3e3e3 !important; }
    .tg-time.slot-top, .tg-cell.slot-top { border-top: 1.5px solid #888 !important; }
    .tg-dayhead { font-size: 10px; padding: 3px 2px; }
    .tg-time { font-size: 8.5px; padding: 3px; }
    .tg-cell { padding: 2px; }
    .tg-cell > * + * { margin-top: 2px; }
    .card-compact { padding: 2px 3px; }
    .card-compact .cname { font-size: 9px; }
    .card-compact .meta { font-size: 8px; }
    /* keep the family-color left border (set inline); neutral elsewhere */
    .card { border: 1px solid #bbb; border-left-width: 4px; box-shadow: none !important; }
    .card .badge { font-size: 7px; }
    .row { border-bottom: 1px solid #ddd; padding: 3px 8px; }
    .card .meta, .row .rsub, .group > h3 .count, .group > h3 .caret { color: #333 !important; }
    .badge { color: #000 !important; border-color: #888 !important; }
    .timegrid tr, .tg-cell, .card, .row, .daysep, .group, .day-block { break-inside: avoid; }
    .groups { gap: 8px; }
    footer.site { color: #333; border-color: #bbb; margin-top: 10px; padding: 8px 0 0; font-size: 9px; }
  }
</style>
</head>
<body>
<header class="site">
  <div class="head-row">
    <h1>FAC Group Exercise Schedule</h1>
    <span class="sub">Fayetteville Athletic Club · weekly classes</span>
    <button class="print-btn" id="printBtn" title="Print the current view">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <polyline points="6 9 6 2 18 2 18 9"></polyline>
        <path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"></path>
        <rect x="6" y="14" width="12" height="8"></rect>
      </svg>
      <span>Print</span>
    </button>
  </div>
  <nav class="tabs" id="tabs">
    <button class="tab active" data-view="day">By Day</button>
    <button class="tab" data-view="class">By Class</button>
    <button class="tab" data-view="instructor">By Instructor</button>
    <button class="tab" data-view="location">By Location</button>
  </nav>
  <div class="toolbar">
    <div class="tod-group">
      <span class="tod-label">Highlight:</span>
      <button class="chip active" data-tod="all">All</button>
      <button class="chip" data-tod="morning">Morning</button>
      <button class="chip" data-tod="midday">Midday</button>
      <button class="chip" data-tod="evening">Evening</button>
    </div>
    <span class="resv-note"><b>*</b> Reservation required</span>
    <span class="legend">
      <b>Morning</b> before 11:00 AM · <b>Midday</b> 11:00 AM–4:00 PM · <b>Evening</b> 4:00 PM+
    </span>
  </div>
  <details class="colorkey" id="colorkey" open>
    <summary>Class colors <span class="hint">— click a type to spotlight it everywhere</span></summary>
    <div class="key-items" id="keyItems"></div>
  </details>
</header>

<main>
  <section id="view-day" class="view active"></section>
  <section id="view-class" class="view"></section>
  <section id="view-instructor" class="view"></section>
  <section id="view-location" class="view"></section>
</main>

<footer class="site">
  <div>Last updated: <b>__UPDATED__</b></div>
  <div class="note">
    Source: <a href="https://www.fayac.com/strength-and-cardio/" target="_blank" rel="noopener">Fayetteville Athletic Club</a>
    — schedule data scraped from the official FAC page. Unofficial reformatting for easier browsing.
  </div>
  <div class="note"><b>*</b> Reservation required.</div>
</footer>

<script id="schedule-data" type="application/json">
/*__DATA__*/
</script>
<script>
(function () {
  "use strict";
  const DATA = JSON.parse(document.getElementById("schedule-data").textContent);
  const classes = DATA.classes;
  // Global lane priority: busiest activities first, so each class keeps the same
  // relative lane position in every time slot it appears in (consistent rows).
  const famRank = {};
  (DATA.families || []).slice()
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label))
    .forEach((f, i) => { famRank[f.family] = i; });
  const DAY_ORDER = ["MON","TUE","WED","THU","FRI","SAT","SUN"];
  const DAY_FULL = {MON:"Mon",TUE:"Tue",WED:"Wed",THU:"Thu",FRI:"Fri",SAT:"Sat",SUN:"Sun"};
  // Distinct weekly start times (sorted) with a display label, shared by the
  // By Day table builder and the Safari print-chunking workaround.
  const slotLabel = new Map();
  classes.forEach(c => { if (!slotLabel.has(c.timeMin)) slotLabel.set(c.timeMin, c.time); });
  const slots = Array.from(slotLabel.keys()).sort((a, b) => a - b);
  let daySlots = [], dayWrap = null;

  function el(tag, cls, html) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => (
      {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  }
  function badge(c) {
    return c.reservationRequired ? ' <span class="badge">Reserve *</span>' : "";
  }
  // Tag a card/row with its time-of-day + family, and color its left border by type.
  function paint(node, c) {
    node.dataset.tod = c.bucket;
    node.dataset.family = c.family;
    node.style.borderLeftColor = c.color;
    return node;
  }

  // ---- By Day ----
  const DAY_NAME = {MON:"Monday",TUE:"Tuesday",WED:"Wednesday",THU:"Thursday",FRI:"Friday",SAT:"Saturday",SUN:"Sunday"};
  function fullDayName(day) { return DAY_NAME[day] || day; }

  // Distinct activity families present at a given time slot (lane count).
  function slotLaneCount(min) {
    const fams = new Set();
    classes.forEach(c => { if (c.timeMin === min) fams.add(c.family); });
    return Math.max(fams.size, 1);
  }
  // One <tbody> per time slot: each family is a lane (<tr>), the time label spans
  // the slot's lanes via rowspan, and days with nothing in a lane stay blank.
  function slotTbody(min, si) {
    const band = si % 2 === 1 ? " band" : "";   // alternate slot banding
    const byDay = {};
    DAY_ORDER.forEach(d => { byDay[d] = classes.filter(c => c.day === d && c.timeMin === min); });
    // Lanes ordered by a single global priority so each class keeps the same
    // relative position in every slot.
    const laneDays = new Map();
    DAY_ORDER.forEach(d => byDay[d].forEach(c => {
      if (!laneDays.has(c.family)) laneDays.set(c.family, new Set());
      laneDays.get(c.family).add(d);
    }));
    const lanes = Array.from(laneDays.keys()).sort((a, b) =>
      (famRank[a] ?? 999) - (famRank[b] ?? 999));
    const L = Math.max(lanes.length, 1);
    const tbody = el("tbody", "slot");
    lanes.forEach((fam, li) => {
      const top = li === 0 ? " slot-top" : "";
      const tr = el("tr", li === 0 ? "slot-top" : null);
      if (li === 0) {
        const th = el("th", "tg-time" + band + " slot-top", esc(slotLabel.get(min)));
        th.rowSpan = L;
        tr.appendChild(th);
      }
      DAY_ORDER.forEach(d => {
        const here = byDay[d].filter(c => c.family === fam);
        if (here.length) {
          const cell = el("td", "tg-cell" + band + top);
          here.forEach(c => cell.appendChild(gridCard(c)));
          tr.appendChild(cell);
        } else {
          tr.appendChild(el("td", "tg-cell tg-empty" + band + top));
        }
      });
      tbody.appendChild(tr);
    });
    return tbody;
  }
  // Build one <table> for the given slots, each with a day-name <thead>.
  function dayTableFor(slotList) {
    const table = el("table", "timegrid");
    const thead = el("thead");
    const hr = el("tr");
    hr.appendChild(el("th", "tg-corner", "&nbsp;"));
    DAY_ORDER.forEach(day => hr.appendChild(el("th", "tg-dayhead", esc(fullDayName(day)))));
    thead.appendChild(hr);
    table.appendChild(thead);
    slotList.forEach(s => table.appendChild(slotTbody(s.min, s.si)));
    return table;
  }

  function buildDay() {
    const root = document.getElementById("view-day");

    // Desktop: a single aligned <table>. The <thead> repeats across printed
    // pages natively (Chrome/Firefox/Edge); Safari uses the chunking below.
    daySlots = slots.map((min, si) => ({ min, si }));
    dayWrap = el("div", "timegrid-wrap");
    dayWrap.appendChild(dayTableFor(daySlots));
    root.appendChild(dayWrap);

    // Mobile: stacked day sections (time shown on each card).
    const stack = el("div", "day-stack");
    DAY_ORDER.forEach(day => {
      const sec = el("section", "day-block");
      sec.appendChild(el("h3", null, esc(fullDayName(day))));
      const body = el("div", "db-body");
      const items = classes.filter(c => c.day === day);
      if (!items.length) body.appendChild(el("div", "empty", "—"));
      else items.forEach(c => body.appendChild(dayCard(c)));
      sec.appendChild(body);
      stack.appendChild(sec);
    });
    root.appendChild(stack);
  }

  // ---- Safari print workaround ----
  // WebKit doesn't repeat a <thead> on each printed page. For WebKit only, split
  // the day table into page-sized chunks (each its own table with its own header)
  // and force a page break before each, so every printed page gets a header.
  const IS_WEBKIT = /^((?!chrome|android|crios|fxios|edg).)*safari/i.test(navigator.userAgent);
  const ROWS_PER_PAGE = 18;   // conservative lane-rows that fit one landscape page
  function buildPrintChunks() {
    if (!dayWrap) return;
    const chunks = [];
    let cur = [], rows = 0;
    daySlots.forEach(s => {
      const L = slotLaneCount(s.min);
      if (cur.length && rows + L > ROWS_PER_PAGE) { chunks.push(cur); cur = []; rows = 0; }
      cur.push(s); rows += L;
    });
    if (cur.length) chunks.push(cur);
    dayWrap.textContent = "";
    chunks.forEach((ch, i) => {
      const t = dayTableFor(ch);
      if (i > 0) t.classList.add("page-break");
      dayWrap.appendChild(t);
    });
    applyHighlight();   // re-apply any active spotlight to the rebuilt nodes
  }
  function restoreDayTable() {
    if (!dayWrap) return;
    dayWrap.textContent = "";
    dayWrap.appendChild(dayTableFor(daySlots));
    applyHighlight();
  }
  function enterPrint() { if (IS_WEBKIT) buildPrintChunks(); }
  function exitPrint() { if (IS_WEBKIT) restoreDayTable(); }

  // Compact card for the grid — time is carried by the row label.
  function gridCard(c) {
    const card = el("div", "card card-compact");
    paint(card, c);
    card.innerHTML =
      '<div class="cname">' + esc(c.displayName) + badge(c) + '</div>' +
      '<div class="meta">' + esc(c.instructor) + ' · ' + esc(c.studio) + '</div>';
    return card;
  }
  // Full card for the mobile stack — includes the time.
  function dayCard(c) {
    const card = el("div", "card");
    paint(card, c);
    card.innerHTML =
      '<div class="time">' + esc(c.time) + '</div>' +
      '<div class="cname">' + esc(c.displayName) + badge(c) + '</div>' +
      '<div class="meta">' + esc(c.instructor) + ' · ' + esc(c.studio) + '</div>';
    return card;
  }

  // ---- Grouped views (class / instructor) ----
  // keyFn may return a string or an array of keys (a class can belong to
  // several instructor groups when it is co-taught).
  function buildGroups(rootId, keyFn, rowFn) {
    const root = document.getElementById(rootId);
    const groups = new Map();
    classes.forEach(c => {
      let keys = keyFn(c);
      if (!Array.isArray(keys)) keys = [keys];
      keys.forEach(k => {
        if (!groups.has(k)) groups.set(k, []);
        groups.get(k).push(c);
      });
    });
    const keys = Array.from(groups.keys()).sort((a, b) =>
      a.localeCompare(b, undefined, { sensitivity: "base" }));
    const wrap = el("div", "groups");
    keys.forEach(k => {
      const items = groups.get(k).slice().sort((a, b) =>
        a.dayIdx - b.dayIdx || a.timeMin - b.timeMin);
      const g = el("div", "group");
      const h = el("h3");
      h.innerHTML = '<span><span class="caret">▾</span>' + esc(k) + '</span>' +
        '<span class="count">' + items.length + ' / wk</span>';
      h.addEventListener("click", () => {
        const collapsed = g.classList.toggle("collapsed");
        h.querySelector(".caret").textContent = collapsed ? "▸" : "▾";
      });
      g.appendChild(h);
      const rows = el("div", "rows");
      items.forEach(c => rows.appendChild(rowFn(c, k)));
      g.appendChild(rows);
      wrap.appendChild(g);
    });
    root.appendChild(wrap);
  }

  function classRow(c) {
    const row = el("div", "row");
    paint(row, c);
    row.innerHTML =
      '<div class="rday">' + esc(DAY_FULL[c.day] || c.day) + '</div>' +
      '<div class="rmain">' +
        '<div class="rtime">' + esc(c.time) + ' — ' + esc(c.displayName) + badge(c) + '</div>' +
        '<div class="rsub">' + esc(c.instructor) + ' · ' + esc(c.studio) + '</div>' +
      '</div>';
    return row;
  }

  function instructorRow(c, who) {
    const row = el("div", "row");
    paint(row, c);
    const others = (c.instructors || [c.instructor]).filter(n => n !== who);
    const withNote = others.length ? ' · with ' + esc(others.join(", ")) : '';
    row.innerHTML =
      '<div class="rday">' + esc(DAY_FULL[c.day] || c.day) + '</div>' +
      '<div class="rmain">' +
        '<div class="rtime">' + esc(c.time) + ' — ' + esc(c.displayName) + badge(c) + '</div>' +
        '<div class="rsub">' + esc(c.studio) + withNote + '</div>' +
      '</div>';
    return row;
  }

  // ---- By Location (grouped by studio, then broken down by day) ----
  const LOC_ORDER = { Large: 1, Small: 2, Cycle: 3, WAYMO: 4, Pool: 5, Tennis: 6, Court: 7 };
  function buildLocation() {
    const root = document.getElementById("view-location");
    const groups = new Map();
    classes.forEach(c => {
      const k = c.studio || "—";
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(c);
    });
    const keys = Array.from(groups.keys()).sort((a, b) =>
      (LOC_ORDER[a] || 99) - (LOC_ORDER[b] || 99) || a.localeCompare(b));
    const wrap = el("div", "groups");
    keys.forEach(k => {
      const items = groups.get(k).slice().sort((a, b) =>
        a.dayIdx - b.dayIdx || a.timeMin - b.timeMin);
      const g = el("div", "group");
      const h = el("h3");
      h.innerHTML = '<span><span class="caret">▾</span>' + esc(k) + '</span>' +
        '<span class="count">' + items.length + ' / wk</span>';
      h.addEventListener("click", () => {
        const collapsed = g.classList.toggle("collapsed");
        h.querySelector(".caret").textContent = collapsed ? "▸" : "▾";
      });
      g.appendChild(h);
      const rows = el("div", "rows");
      let curDay = null;
      items.forEach(c => {
        if (c.day !== curDay) {
          curDay = c.day;
          rows.appendChild(el("div", "daysep", esc(fullDayName(c.day))));
        }
        rows.appendChild(locationRow(c));
      });
      g.appendChild(rows);
      wrap.appendChild(g);
    });
    root.appendChild(wrap);
  }
  function locationRow(c) {
    const row = el("div", "row");
    paint(row, c);
    row.innerHTML =
      '<div class="rday">' + esc(c.time) + '</div>' +
      '<div class="rmain">' +
        '<div class="rtime">' + esc(c.displayName) + badge(c) + '</div>' +
        '<div class="rsub">' + esc(c.instructor) + '</div>' +
      '</div>';
    return row;
  }

  function buildClass() {
    buildGroups("view-class", c => c.familyLabel, classRow);
  }
  function buildInstructor() {
    buildGroups("view-instructor", c => (c.instructors && c.instructors.length ? c.instructors : [c.instructor || "—"]), instructorRow);
  }

  // ---- Tabs ----
  document.getElementById("tabs").addEventListener("click", e => {
    const btn = e.target.closest(".tab");
    if (!btn) return;
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("view-" + btn.dataset.view).classList.add("active");
  });

  // ---- Highlight (time-of-day filter + color-key family spotlight) ----
  let curTod = "all", curFam = null;

  function applyHighlight() {
    const filtering = (curTod && curTod !== "all") || !!curFam;
    document.body.classList.toggle("hl-filtering", filtering);
    document.querySelectorAll(".card, .row").forEach(node => {
      const match =
        (curTod === "all" || node.dataset.tod === curTod) &&
        (!curFam || node.dataset.family === curFam);
      node.classList.toggle("hl-match", filtering && match);
    });
    // Hide groups with no matching session so grouped views stay tidy.
    document.querySelectorAll(".group").forEach(g => {
      const any = !filtering || g.querySelector(".row.hl-match");
      g.classList.toggle("hl-hidden", filtering && !any);
    });
  }

  // Time-of-day chips
  document.querySelector(".tod-group").addEventListener("click", e => {
    const btn = e.target.closest(".chip");
    if (!btn) return;
    document.querySelectorAll(".chip").forEach(c => c.classList.remove("active"));
    btn.classList.add("active");
    curTod = btn.dataset.tod;
    applyHighlight();
  });

  // Color key — each family is a clickable swatch that spotlights that activity.
  function buildLegend() {
    const box = document.getElementById("keyItems");
    (DATA.families || []).forEach(f => {
      const item = el("div", "key-item");
      item.dataset.family = f.family;
      item.innerHTML =
        '<span class="swatch" style="background:' + esc(f.color) + '"></span>' +
        esc(f.label) + ' <span class="kcount">' + f.count + '</span>';
      item.addEventListener("click", () => {
        const wasActive = curFam === f.family;
        document.querySelectorAll(".key-item").forEach(k => k.classList.remove("active"));
        curFam = wasActive ? null : f.family;
        if (curFam) item.classList.add("active");
        applyHighlight();
      });
      box.appendChild(item);
    });
  }

  // ---- Print ----
  document.getElementById("printBtn").addEventListener("click", () => window.print());
  // Safari: swap to page-chunked tables for printing, then restore.
  const mqlPrint = window.matchMedia && window.matchMedia("print");
  if (mqlPrint) {
    const onChange = e => (e.matches ? enterPrint() : exitPrint());
    if (mqlPrint.addEventListener) mqlPrint.addEventListener("change", onChange);
    else if (mqlPrint.addListener) mqlPrint.addListener(onChange);  // older Safari
  }
  window.addEventListener("beforeprint", enterPrint);
  window.addEventListener("afterprint", exitPrint);

  // ---- Init ----
  buildLegend();
  buildDay();
  buildClass();
  buildInstructor();
  buildLocation();
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
