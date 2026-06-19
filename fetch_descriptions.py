#!/usr/bin/env python3
"""
Fetch Les Mills class descriptions for the FAC schedule tooltips.

Pulls program descriptions from the official Les Mills "all workouts" listing
(https://www.lesmills.com/us/workouts/all) and each program's own page, matches
them to the Les Mills programs that appear on the FAC schedule, and updates
`descriptions.json`. Run it periodically to pick up new programs or copy that
has changed since last time.

IMPORTANT — what this does and does NOT touch:
  * It writes the fetched VERBATIM Les Mills copy into each program's
    `source_text` field. That text is REFERENCE ONLY: build.py never publishes
    it, so it does not appear on the site.
  * It NEVER overwrites `summary` — the published, our-own-words description.
    When the verbatim `source_text` changes (or a brand-new program appears),
    it is reported so a fresh `summary` can be hand/agent-written from it.
  * `summary_source` records the source copy the current `summary` was last
    reconciled against. A summary is flagged STALE whenever the live
    `source_text` differs from its `summary_source` — a level-triggered check
    that persists every run until resolved (it does not self-clear when a
    changed source is committed). WHEN YOU (RE)WRITE A `summary`, also set that
    program's `summary_source` to the `source_text` you wrote it from, so the
    stale flag clears. (Set it to the current fetched `source_text` even if you
    drew the prose from richer on-page copy, so the automated comparison stays
    stable.)

Usage:
    python3 fetch_descriptions.py            # fetch + update source_text in descriptions.json
    python3 fetch_descriptions.py --check     # report changes, exit 1 if any, no write
    python3 fetch_descriptions.py --dry-run   # same as --check but always exit 0

Notes:
  * Stdlib only — no `pip install`.
  * Matching the marketing site to our schedule needs a little glue: the
    PROGRAMS registry below maps each Les Mills program (keyed the same way as
    build.py's `program_for`) to the names/aliases used on lesmills.com.
  * If a program can't be fetched (network down, page moved, parser missed it),
    its existing record is kept untouched — nothing is blanked by a flaky fetch.
    Such misses are reported so you can fix the matching or the parser.
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime
from html.parser import HTMLParser
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
DESCRIPTIONS_JSON = HERE / "descriptions.json"
LISTING_URL = "https://www.lesmills.com/us/workouts/all"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# --------------------------------------------------------------------------- #
# Program registry: our program key -> how it shows up on lesmills.com.
# `key` must match build.py's program_for() output. `aliases` are normalized
# (see norm()) and matched against link text on the listing page. `url` is the
# known program page, used as a fallback when the listing match fails.
# --------------------------------------------------------------------------- #
PROGRAMS = {
    "BODYPUMP": {
        "name": "LES MILLS BODYPUMP",
        "aliases": ["BODYPUMP", "BODY PUMP"],
        "url": "https://www.lesmills.com/us/workouts/group-fitness/bodypump/",
    },
    "BODYATTACK": {
        "name": "LES MILLS BODYATTACK",
        "aliases": ["BODYATTACK", "BODY ATTACK"],
        "url": "https://www.lesmills.com/us/workouts/group-fitness/bodyattack/",
    },
    "GRIT": {
        "name": "LES MILLS GRIT",
        "aliases": ["GRIT", "LES MILLS GRIT"],
        "url": "https://www.lesmills.com/us/workouts/group-fitness/les-mills-grit/",
    },
    "CORE": {
        "name": "LES MILLS CORE",
        "aliases": ["CORE", "LES MILLS CORE", "CXWORX"],
        "url": "https://www.lesmills.com/us/workouts/group-fitness/core/",
    },
    "SHAPES": {
        "name": "LES MILLS SHAPES",
        "aliases": ["SHAPES", "LES MILLS SHAPES"],
        "url": "https://www.lesmills.com/us/workouts/group-fitness/les-mills-shapes/",
    },
    "STRENGTH_DEVELOPMENT": {
        "name": "LES MILLS STRENGTH DEVELOPMENT",
        "aliases": ["STRENGTH DEVELOPMENT", "LES MILLS STRENGTH DEVELOPMENT"],
        "url": "https://www.lesmills.com/us/workouts/group-fitness/les-mills-strength-development/",
    },
    "TONE": {
        "name": "LES MILLS TONE",
        "aliases": ["TONE", "LES MILLS TONE"],
        "url": "https://www.lesmills.com/us/workouts/group-fitness/tone/",
    },
    "CEREMONY": {
        # Les Mills folded CEREMONY into the LES MILLS x HYROX partnership; the
        # old group-fitness/ceremony page 404s. Resolve to the HYROX page first.
        "name": "LES MILLS CEREMONY",
        "aliases": ["CEREMONY HYROX", "LES MILLS CEREMONY", "CEREMONY"],
        "url": "https://www.lesmills.com/us/workouts/ceremony-hyrox",
    },
    "RPM": {
        "name": "LES MILLS RPM",
        "aliases": ["RPM", "LES MILLS RPM"],
        "url": "https://www.lesmills.com/us/workouts/group-fitness/rpm/",
    },
    "SPRINT": {
        "name": "LES MILLS SPRINT",
        "aliases": ["SPRINT", "LES MILLS SPRINT"],
        "url": "https://www.lesmills.com/us/workouts/group-fitness/les-mills-sprint/",
    },
}


def norm(s: str) -> str:
    """Normalize a name for matching: upper-case, alphanumerics + spaces only."""
    s = re.sub(r"[^A-Za-z0-9 ]+", " ", s or "")
    return re.sub(r"\s+", " ", s).strip().upper()


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def fetch(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def listing_links(page: str) -> dict[str, str]:
    """Map normalized link text -> absolute URL for every workout link found."""
    links: dict[str, str] = {}
    for m in re.finditer(
        r'<a\b[^>]*\bhref="([^"]*workouts/[^"]+)"[^>]*>(.*?)</a>',
        page,
        re.IGNORECASE | re.DOTALL,
    ):
        href, inner = m.group(1), m.group(2)
        text = norm(re.sub(r"<[^>]+>", " ", inner))
        if not text:
            continue
        if href.startswith("/"):
            href = "https://www.lesmills.com" + href
        links.setdefault(text, href)
    return links


class _MetaScraper(HTMLParser):
    """Pull og:description / meta description and the first paragraph of body."""

    def __init__(self) -> None:
        super().__init__()
        self.meta_desc = ""
        self.og_desc = ""
        self._in_p = False
        self._p_chunks: list[str] = []
        self.first_p = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            content = (a.get("content") or "").strip()
            if a.get("property") == "og:description" and content:
                self.og_desc = self.og_desc or content
            if (a.get("name") or "").lower() == "description" and content:
                self.meta_desc = self.meta_desc or content
        elif tag == "p" and not self.first_p:
            self._in_p = True
            self._p_chunks = []

    def handle_endtag(self, tag):
        if tag == "p" and self._in_p:
            self._in_p = False
            text = re.sub(r"\s+", " ", "".join(self._p_chunks)).strip()
            # Skip tiny/boilerplate paragraphs (cookie notices, nav, etc.).
            if len(text) >= 60 and not self.first_p:
                self.first_p = text

    def handle_data(self, data):
        if self._in_p:
            self._p_chunks.append(data)


def extract_description(page: str) -> str:
    """Best-effort description from a program page: og:desc > meta > first <p>."""
    # Prefer JSON-LD "description" when present (cleanest, editor-authored).
    for m in re.finditer(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        page,
        re.IGNORECASE | re.DOTALL,
    ):
        try:
            blob = json.loads(m.group(1).strip())
        except ValueError:
            continue
        for node in blob if isinstance(blob, list) else [blob]:
            if isinstance(node, dict):
                d = (node.get("description") or "").strip()
                if len(d) >= 40:
                    return re.sub(r"\s+", " ", d)
    s = _MetaScraper()
    try:
        s.feed(page)
    except Exception:  # noqa: BLE001 — malformed HTML shouldn't crash a refresh
        pass
    for candidate in (s.og_desc, s.meta_desc, s.first_p):
        if candidate and len(candidate) >= 40:
            return re.sub(r"\s+", " ", candidate).strip()
    return ""


def resolve_url(key: str, links: dict[str, str]) -> str:
    """Find a program's page URL from the listing, falling back to its known URL."""
    for alias in PROGRAMS[key]["aliases"]:
        na = norm(alias)
        if na in links:
            return links[na]
    # Loose contains-match (e.g. "LES MILLS BODYPUMP" link text vs "BODYPUMP").
    for alias in PROGRAMS[key]["aliases"]:
        na = norm(alias)
        for text, href in links.items():
            if na and na in text:
                return href
    return PROGRAMS[key]["url"]


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def load_existing() -> dict:
    if DESCRIPTIONS_JSON.exists():
        try:
            return json.loads(DESCRIPTIONS_JSON.read_text(encoding="utf-8"))
        except ValueError as exc:
            print(f"WARN: existing {DESCRIPTIONS_JSON.name} is invalid JSON: {exc}")
    return {}


def reconcile(keys: list[str]) -> None:
    """Mark summaries as current: set each program's `summary_source` to its
    stored `source_text` and bump `summary_updated`. Run this right AFTER you
    (re)write a `summary`, so the level-triggered stale check clears. Offline —
    reads/writes descriptions.json only, no fetching."""
    data = load_existing()
    programs = data.get("programs", {})
    if not programs:
        print(f"No programs in {DESCRIPTIONS_JSON.name}; nothing to reconcile.")
        return
    targets = keys or list(programs)
    today = date.today().isoformat()
    done, skipped = [], []
    for k in targets:
        r = programs.get(k)
        if r is None:
            print(f"  {k}: not found"); skipped.append(k); continue
        if not (r.get("summary") or "").strip():
            print(f"  {k}: no summary to reconcile"); skipped.append(k); continue
        if (r.get("summary_source") or "") == (r.get("source_text") or ""):
            skipped.append(f"{k} (already current)"); continue
        r["summary_source"] = r.get("source_text") or ""
        r["summary_updated"] = today
        if not (r.get("summary_origin") or "").strip():
            r["summary_origin"] = "agent"
        done.append(k)
    DESCRIPTIONS_JSON.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Reconciled summary_source for: {', '.join(done) or '—'}")
    if skipped:
        print(f"Skipped: {', '.join(skipped)}")
    print("Now run `python3 build.py` to rebuild the site.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Refresh Les Mills descriptions.")
    ap.add_argument("--check", action="store_true",
                    help="report changes and exit 1 if any; do not write")
    ap.add_argument("--dry-run", action="store_true",
                    help="report changes but never write or fail")
    ap.add_argument("--reconcile", nargs="*", metavar="KEY",
                    help="set summary_source = source_text (and bump summary_updated) "
                         "for the given programs (or all if none given), then exit. "
                         "Run after rewriting a summary so the stale flag clears. "
                         "No network.")
    args = ap.parse_args()

    if args.reconcile is not None:
        reconcile([k.upper() for k in args.reconcile])
        return

    existing = load_existing()
    old_programs = existing.get("programs", {})

    # 1. Listing page (for URL discovery + spotting brand-new programs).
    links: dict[str, str] = {}
    try:
        links = listing_links(fetch(LISTING_URL))
        print(f"Listing: found {len(links)} workout links on {LISTING_URL}")
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: could not fetch listing ({exc}); using known program URLs.")

    # 2. Each program page. We refresh `source_text` (verbatim, reference-only)
    #    and preserve everything else — crucially the published `summary`.
    today = date.today().isoformat()
    new_programs: dict[str, dict] = {}
    misses: list[str] = []
    # Track which programs' verbatim copy is new or changed (→ re-summarize).
    src_added, src_changed = [], []
    for key, meta in PROGRAMS.items():
        url = resolve_url(key, links)
        # Start from the existing record so summary/origin/etc. are preserved.
        rec = dict(old_programs.get(key, {}))
        rec.setdefault("name", meta["name"])
        rec.setdefault("summary", "")
        rec.setdefault("summary_origin", "")
        rec.setdefault("summary_updated", None)
        rec["url"] = url
        rec.setdefault("source_text", "")
        rec.setdefault("source_text_fetched", None)
        # The source copy the current `summary` was last reconciled against.
        # Drives level-triggered staleness (see below); set when a summary is
        # (re)written. Empty means "needs backfill / not yet reconciled".
        rec.setdefault("summary_source", "")

        fetched_text = ""
        try:
            fetched_text = extract_description(fetch(url))
        except Exception as exc:  # noqa: BLE001
            print(f"  {key}: fetch failed ({exc})")

        if fetched_text:
            old_src = old_programs.get(key, {}).get("source_text", "")
            if not old_src:
                src_added.append(key)
            elif old_src != fetched_text:
                src_changed.append(key)
            rec["source_text"] = fetched_text
            rec["source_text_fetched"] = today
            print(f"  {key}: fetched {len(fetched_text)} chars from {url}")
        else:
            # Keep the existing record untouched (no blanking on a flaky fetch).
            if old_programs.get(key, {}).get("source_text"):
                misses.append(f"{key} (kept existing source_text)")
            else:
                misses.append(f"{key} (no source_text yet)")
        new_programs[key] = rec

    # One-time migration / new-program init: when a summary exists but has no
    # recorded basis, adopt the current source copy as that basis (assumes the
    # existing summary already matches the current source). Mismatches that
    # appear AFTER this is set are what `stale` reports.
    for r in new_programs.values():
        if (r.get("summary") or "").strip() and not (r.get("summary_source") or "").strip():
            r["summary_source"] = r.get("source_text") or ""

    # Programs with verbatim copy but no published summary yet → need one written.
    needs_summary = [
        k for k, r in new_programs.items()
        if r.get("source_text") and not (r.get("summary") or "").strip()
    ]
    # Level-triggered staleness: a published summary is stale whenever the live
    # source copy differs from the `summary_source` it was reconciled against.
    # Unlike change-detection, this PERSISTS every run until someone rewrites the
    # summary and updates `summary_source` — so it can't silently self-clear when
    # a changed source is committed (the bug that hid the CEREMONY rebrand).
    stale = [
        k for k, r in new_programs.items()
        if (r.get("summary") or "").strip()
        and (r.get("source_text") or "")
        and (r.get("source_text") or "") != (r.get("summary_source") or "")
    ]
    removed = [k for k in old_programs if k not in new_programs]

    # Les Mills links on the listing we don't yet map (possible new classes).
    mapped_aliases = {norm(a) for m in PROGRAMS.values() for a in m["aliases"]}
    unmapped = sorted(
        t for t in links
        if "LES MILLS" in t and not any(a in t or t in a for a in mapped_aliases)
    )

    print("\n=== Description refresh report ===")
    print(f"  source copy new:      {', '.join(src_added)   or '—'}")
    print(f"  source copy changed:  {', '.join(src_changed) or '—'}")
    print(f"  needs a summary:      {', '.join(needs_summary) or '—'}")
    print(f"  summary may be stale: {', '.join(stale)       or '—'}")
    print(f"  removed:              {', '.join(removed)     or '—'}")
    print(f"  fetch misses:         {', '.join(misses)      or '—'}")
    if unmapped:
        print("  unmapped Les Mills links on the site (consider adding to PROGRAMS):")
        for t in unmapped:
            print(f"    - {t} -> {links[t]}")
    if needs_summary or stale:
        affected = " ".join(sorted(set(needs_summary) | set(stale)))
        print("\n  → (Re)write `summary` for the above from `source_text`, then run:")
        print(f"        python3 fetch_descriptions.py --reconcile {affected}")
        print("        python3 build.py")

    # Surface description "drift" to CI: anything that means the published copy
    # may no longer reflect Les Mills, or that a page could not be parsed. These
    # are all actionable and self-clearing once addressed. `unmapped` is omitted
    # on purpose — those are programs FAC doesn't offer, a persistent condition
    # that would re-alert every run; it stays in the report above for reference.
    hard_miss = [m for m in misses if "no source_text yet" in m]
    drift_reasons = []
    if needs_summary:
        drift_reasons.append("needs a summary: " + ", ".join(needs_summary))
    if stale:
        drift_reasons.append("summary may be stale: " + ", ".join(stale))
    if removed:
        drift_reasons.append("removed: " + ", ".join(removed))
    if hard_miss:
        drift_reasons.append("could not fetch (no copy yet): " + ", ".join(hard_miss))
    if drift_reasons:
        gh_set_output("drift", "true")
        gh_set_output("drift_summary", " | ".join(drift_reasons))
        for r in drift_reasons:
            gh_annotate("warning", f"Les Mills descriptions: {r}")

    has_changes = bool(src_added or src_changed or removed)

    if args.check or args.dry_run:
        prefix = "(dry run) " if args.dry_run else ""
        print(f"\n{prefix}{'Source-copy changes detected.' if has_changes else 'No changes.'}")
        sys.exit(1 if (args.check and has_changes) else 0)

    # 3. Write (source_text + bookkeeping only; summaries are left as-is).
    payload = {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "source": LISTING_URL,
        "note": existing.get("note", ""),
        "programs": new_programs,
    }
    DESCRIPTIONS_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\nWrote {DESCRIPTIONS_JSON.name} ({len(new_programs)} programs).")
    if needs_summary or stale:
        print("Some summaries need (re)writing before the site reflects the latest copy.")


if __name__ == "__main__":
    main()
