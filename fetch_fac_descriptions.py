#!/usr/bin/env python3
"""
Fetch FAC class bullet-points for the non-Les-Mills schedule tooltips.

The Fayetteville Athletic Club "Strength & Cardio" page describes several of its
own (non-Les-Mills) classes as short bullet lists. This script scrapes those
bullets and stores them, verbatim, in `fac_descriptions.json` so a human/agent
can write a published `summary` from them — exactly mirroring how
`fetch_descriptions.py` handles the Les Mills programs, but sourced from FAC.

IMPORTANT — what this does and does NOT touch (same contract as the Les Mills
fetcher, so the two behave identically):
  * It writes the scraped VERBATIM bullets into each class's `source_bullets`
    field. That text is REFERENCE ONLY: build.py never publishes it.
  * It NEVER overwrites `summary` — the published, our-own-words description.
    When the bullets change (or a brand-new class appears) it is reported so a
    fresh `summary` can be hand/agent-written from them.
  * `summary_source` records the bullets the current `summary` was last
    reconciled against. A summary is flagged STALE whenever the live
    `source_bullets` differ from its `summary_source` — a level-triggered check
    that persists every run until resolved. WHEN YOU (RE)WRITE a `summary`, also
    reconcile it (see --reconcile) so the stale flag clears.

Unlike the Les Mills programs (each on its own marketing page), every FAC class
description lives on the single Strength & Cardio page, so the published tooltip
credit links back to that one page for all of them.

Usage:
    python3 fetch_fac_descriptions.py             # scrape + update source_bullets
    python3 fetch_fac_descriptions.py --check      # report changes, exit 1 if any
    python3 fetch_fac_descriptions.py --dry-run    # report changes, never write/fail
    python3 fetch_fac_descriptions.py --reconcile [KEY ...]
                                                   # mark summaries current (no network)

Notes:
  * Stdlib only — no `pip install`.
  * CLASSES below maps each build.py *family* key (so build.py can attach a
    description straight to a family) to the heading text used on the FAC page.
    Only classes that appear on the schedule belong here; classes on the page
    that FAC doesn't currently schedule (e.g. Cardio Pickleball, Progressive
    Strength) are intentionally omitted and reported as unmapped.
"""

import argparse
import html as html_mod
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path


def gh_set_output(key: str, value: str) -> None:
    """Expose a value to later GitHub Actions steps via $GITHUB_OUTPUT."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{key}={value}\n")
    except OSError:
        pass


def gh_annotate(level: str, msg: str) -> None:
    """Emit a GitHub Actions annotation (warning/error), or plain text locally."""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{level}::{msg}")
    else:
        print(f"[{level.upper()}] {msg}")


HERE = Path(__file__).resolve().parent
FAC_DESCRIPTIONS_JSON = HERE / "fac_descriptions.json"
SOURCE_URL = "https://www.fayac.com/strength-and-cardio/"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# --------------------------------------------------------------------------- #
# Class registry: build.py *family* key -> how it shows up on fayac.com.
# `name` is the display name we publish; `heading` is the exact heading text on
# the Strength & Cardio page whose following bullet list we scrape. Keys must
# match build.py's `family` values so build.py can attach by family.
# --------------------------------------------------------------------------- #
CLASSES = {
    "DANCEFIIT":         {"name": "DanceFIIT",         "heading": "DanceFIIT"},
    "HOT_CARDIO_SCULPT": {"name": "Hot Cardio Sculpt", "heading": "Hot Cardio Sculpt"},
    "FAST_FEED":         {"name": "Fast Feed Tennis",  "heading": "Fast Feed Tennis"},
    "BARRE":             {"name": "Barre Intensity",   "heading": "Barre Intensity"},
    "FULL_BODY_TONE":    {"name": "Full Body Tone",    "heading": "Full Body Tone"},
}


# --------------------------------------------------------------------------- #
# Fetching + parsing
# --------------------------------------------------------------------------- #
def fetch(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _clean(fragment: str) -> str:
    """Strip inner tags, unescape entities, collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def extract_bullets(page: str, heading: str) -> list[str]:
    """Return the bullet list immediately following a class heading on the FAC
    page. The page is Elementor markup: a heading widget (<h2>/<h3 class=
    "elementor-heading-title ...">Name</h…>) followed shortly by a text-editor
    widget holding a <ul><li>…</li></ul>. We locate the heading by its exact
    text, then take the first <ul> within the next stretch of HTML (bounded so a
    section with no bullets can't borrow the next section's list)."""
    hm = re.search(
        r"<h[1-4][^>]*elementor-heading-title[^>]*>\s*"
        + re.escape(heading) + r"\s*</h[1-4]>",
        page, re.IGNORECASE,
    )
    if not hm:
        return []
    window = page[hm.end(): hm.end() + 2000]
    # Don't cross into the next class's heading.
    nxt = re.search(r"elementor-heading-title", window)
    if nxt:
        window = window[: nxt.start()]
    um = re.search(r"<ul\b[^>]*>(.*?)</ul>", window, re.IGNORECASE | re.DOTALL)
    if not um:
        return []
    bullets = []
    for li in re.finditer(r"<li\b[^>]*>(.*?)</li>", um.group(1),
                          re.IGNORECASE | re.DOTALL):
        text = _clean(li.group(1))
        if text:
            bullets.append(text)
    return bullets


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def load_existing() -> dict:
    if FAC_DESCRIPTIONS_JSON.exists():
        try:
            return json.loads(FAC_DESCRIPTIONS_JSON.read_text(encoding="utf-8"))
        except ValueError as exc:
            print(f"WARN: existing {FAC_DESCRIPTIONS_JSON.name} is invalid JSON: {exc}")
    return {}


def reconcile(keys: list[str]) -> None:
    """Mark summaries as current: set each class's `summary_source` to its stored
    `source_bullets` and bump `summary_updated`. Run this right AFTER you
    (re)write a `summary`, so the level-triggered stale check clears. Offline —
    reads/writes fac_descriptions.json only, no fetching."""
    data = load_existing()
    classes = data.get("classes", {})
    if not classes:
        print(f"No classes in {FAC_DESCRIPTIONS_JSON.name}; nothing to reconcile.")
        return
    targets = keys or list(classes)
    today = date.today().isoformat()
    done, skipped = [], []
    for k in targets:
        r = classes.get(k)
        if r is None:
            print(f"  {k}: not found"); skipped.append(k); continue
        if not (r.get("summary") or "").strip():
            print(f"  {k}: no summary to reconcile"); skipped.append(k); continue
        if (r.get("summary_source") or []) == (r.get("source_bullets") or []):
            skipped.append(f"{k} (already current)"); continue
        r["summary_source"] = list(r.get("source_bullets") or [])
        r["summary_updated"] = today
        if not (r.get("summary_origin") or "").strip():
            r["summary_origin"] = "agent"
        done.append(k)
    FAC_DESCRIPTIONS_JSON.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Reconciled summary_source for: {', '.join(done) or '—'}")
    if skipped:
        print(f"Skipped: {', '.join(skipped)}")
    print("Now run `python3 build.py` to rebuild the site.")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Refresh FAC class bullet descriptions.")
    ap.add_argument("--check", action="store_true",
                    help="report changes and exit 1 if any; do not write")
    ap.add_argument("--dry-run", action="store_true",
                    help="report changes but never write or fail")
    ap.add_argument("--reconcile", nargs="*", metavar="KEY",
                    help="set summary_source = source_bullets (and bump "
                         "summary_updated) for the given classes (or all if none "
                         "given), then exit. Run after rewriting a summary so the "
                         "stale flag clears. No network.")
    args = ap.parse_args()

    if args.reconcile is not None:
        reconcile([k.upper() for k in args.reconcile])
        return

    existing = load_existing()
    old_classes = existing.get("classes", {})

    page = ""
    try:
        page = fetch(SOURCE_URL)
        print(f"Fetched {len(page):,} bytes from {SOURCE_URL}")
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: could not fetch FAC page ({exc}); keeping existing bullets.")

    today = date.today().isoformat()
    new_classes: dict[str, dict] = {}
    misses: list[str] = []
    bullets_added, bullets_changed = [], []

    for key, meta in CLASSES.items():
        # Start from the existing record so summary/origin/etc. are preserved.
        rec = dict(old_classes.get(key, {}))
        rec.setdefault("name", meta["name"])
        rec.setdefault("summary", "")
        rec.setdefault("summary_origin", "")
        rec.setdefault("summary_updated", None)
        rec["url"] = SOURCE_URL
        rec.setdefault("source_bullets", [])
        rec.setdefault("source_bullets_fetched", None)
        rec.setdefault("summary_source", [])

        fetched = extract_bullets(page, meta["heading"]) if page else []
        if fetched:
            old = old_classes.get(key, {}).get("source_bullets", [])
            if not old:
                bullets_added.append(key)
            elif old != fetched:
                bullets_changed.append(key)
            rec["source_bullets"] = fetched
            rec["source_bullets_fetched"] = today
            print(f"  {key}: {len(fetched)} bullets from \"{meta['heading']}\"")
        else:
            # Keep the existing record untouched (no blanking on a flaky fetch).
            if old_classes.get(key, {}).get("source_bullets"):
                misses.append(f"{key} (kept existing bullets)")
            else:
                misses.append(f"{key} (no bullets yet)")
        new_classes[key] = rec

    # One-time migration / new-class init: when a summary exists but has no
    # recorded basis, adopt the current bullets as that basis.
    for r in new_classes.values():
        if (r.get("summary") or "").strip() and not (r.get("summary_source") or []):
            r["summary_source"] = list(r.get("source_bullets") or [])

    needs_summary = [
        k for k, r in new_classes.items()
        if r.get("source_bullets") and not (r.get("summary") or "").strip()
    ]
    # Level-triggered staleness: published summary vs the bullets it was
    # reconciled against (persists every run until someone rewrites + reconciles).
    stale = [
        k for k, r in new_classes.items()
        if (r.get("summary") or "").strip()
        and (r.get("source_bullets") or [])
        and (r.get("source_bullets") or []) != (r.get("summary_source") or [])
    ]
    removed = [k for k in old_classes if k not in new_classes]

    print("\n=== FAC description refresh report ===")
    print(f"  bullets new:          {', '.join(bullets_added)   or '—'}")
    print(f"  bullets changed:      {', '.join(bullets_changed) or '—'}")
    print(f"  needs a summary:      {', '.join(needs_summary)   or '—'}")
    print(f"  summary may be stale: {', '.join(stale)           or '—'}")
    print(f"  removed:              {', '.join(removed)         or '—'}")
    print(f"  scrape misses:        {', '.join(misses)          or '—'}")
    if needs_summary or stale:
        affected = " ".join(sorted(set(needs_summary) | set(stale)))
        print("\n  → (Re)write `summary` for the above from `source_bullets`, then run:")
        print(f"        python3 fetch_fac_descriptions.py --reconcile {affected}")
        print("        python3 build.py")

    hard_miss = [m for m in misses if "no bullets yet" in m]
    drift_reasons = []
    if needs_summary:
        drift_reasons.append("needs a summary: " + ", ".join(needs_summary))
    if stale:
        drift_reasons.append("summary may be stale: " + ", ".join(stale))
    if removed:
        drift_reasons.append("removed: " + ", ".join(removed))
    if hard_miss:
        drift_reasons.append("could not scrape (no bullets yet): " + ", ".join(hard_miss))
    if drift_reasons:
        gh_set_output("drift", "true")
        gh_set_output("drift_summary", " | ".join(drift_reasons))
        for r in drift_reasons:
            gh_annotate("warning", f"FAC descriptions: {r}")

    has_changes = bool(bullets_added or bullets_changed or removed)

    if args.check or args.dry_run:
        prefix = "(dry run) " if args.dry_run else ""
        print(f"\n{prefix}{'Bullet changes detected.' if has_changes else 'No changes.'}")
        sys.exit(1 if (args.check and has_changes) else 0)

    payload = {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "source": SOURCE_URL,
        "note": existing.get("note", ""),
        "classes": new_classes,
    }
    FAC_DESCRIPTIONS_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\nWrote {FAC_DESCRIPTIONS_JSON.name} ({len(new_classes)} classes).")
    if needs_summary or stale:
        print("Some summaries need (re)writing before the site reflects the latest copy.")


if __name__ == "__main__":
    main()
