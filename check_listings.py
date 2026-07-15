#!/usr/bin/env python3
"""
Van Watch -- Toyota Sienna AWD hybrid listing monitor.

Queries the auto.dev Listings API for 2021+ Siennas (hybrid-only generation)
nationwide, filters to AWD under a price cap, pushes new finds to ntfy,
and rebuilds a static digest page for GitHub Pages.

State (seen VINs) is committed back to the repo by the workflow.
"""

import html
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- config
API_KEY = os.environ["AUTODEV_API_KEY"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]

PRICE_CAP = 44000
MIN_YEAR = 2021            # first hybrid-only Sienna generation
MAX_PAGES = 15             # deep sweep page budget (once daily)
PAGE_LIMIT = 20

# Value gate: a listing must be priced at or below its mileage-adjusted
# ceiling. Calibrated to observed market: ~19c/mi real depreciation on
# hybrid AWD Siennas; 15c is deliberately lenient so deals surface.
#   ceiling = VALUE_BASE - VALUE_PER_MILE * miles
#   50k mi -> $36,500 | 100k mi -> $29,000 | 130k mi -> $24,500
VALUE_BASE = 44000
VALUE_PER_MILE = 0.15

# Hourly runs do a 1-page "newest first" poll for fast alerts; the digest is
# rebuilt only by the daily deep sweep (price.asc). Budget: ~960 calls/mo,
# inside auto.dev's 1,000/mo free tier.
DEEP_INTERVAL_HOURS = 20   # any run finding the last deep sweep older than this promotes itself
MAX_PAGES_SHALLOW = 1

# Flag-don't-hide thresholds (listing still shows, with a caution tag)
SUSPICIOUS_PRICE = 24000   # nationwide cheapest often = flood/rebuilt; scrutinize
HIGH_MILES = 120000

# Hard-exclude trims (fabric interior). Word-boundary match so XLE/Limited survive.
import re
EXCLUDE_TRIM_RE = re.compile(r"^\s*LE\b", re.IGNORECASE)

# Hard-exclude branded titles wherever the listing discloses one.
SALVAGE_RE = re.compile(
    r"\b(salvage|rebuilt|rebuildable|reconstructed|branded\s+title|"
    r"flood(?:ed)?|water\s+damage|hail\s+damage|lemon(?:\s+law)?\s*(?:buyback)?|"
    r"theft\s+recovery|r[\s-]?title|prior\s+total\s+loss|total(?:ed)?\s+loss|"
    r"insurance\s+loss|junk\s+title|bonded\s+title|export\s+only)\b",
    re.IGNORECASE,
)
# Phrases like "salvage-free" or "no flood damage" are selling points, not
# disclosures: strip them before scanning.
NEGATION_RE = re.compile(
    r"\b(?:no|not\s+a|never|free\s+of|zero|without)\s+(?:prior\s+)?"
    r"(?:salvage|rebuilt|rebuildable|reconstructed|flood(?:ed)?|water\s+damage|"
    r"hail\s+damage|accidents?|total\s+loss|branded\s+title)[\w-]*"
    r"|\b(?:salvage|flood|accident|damage)[\s-]?free\b",
    re.IGNORECASE,
)
TITLE_FIELDS = (
    "retailListing.titleStatus",
    "vehicle.titleStatus",
    "retailListing.title",
    "retailListing.condition",
    "retailListing.description",
    "history.titleStatus",
)


def is_branded(row: dict) -> bool:
    for field in TITLE_FIELDS:
        val = get_path(row, field)
        if val and SALVAGE_RE.search(NEGATION_RE.sub(" ", str(val))):
            return True
    return False

STATE_FILE = Path("seen_vins.json")
DIGEST_FILE = Path("docs/index.html")
WATCH_STATE_FILE = Path("watch_state.json")

API_BASE = "https://api.auto.dev/listings"

AWD_TOKENS = ("awd", "all-wheel", "all wheel", "4wd", "four-wheel", "four wheel")


# ---------------------------------------------------------------- helpers
def api_get(params: dict) -> dict:
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "van-watch/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_path(obj: dict, dotted: str, default=None):
    """Read 'a.b.c' from nested dicts; tolerate flat dot-keyed rows too."""
    if dotted in obj:
        return obj[dotted]
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def is_awd(row: dict) -> bool:
    haystacks = []
    for field in (
        "vehicle.driveType",
        "vehicle.drivetrain",
        "vehicle.trim",
        "vehicle.style",
        "retailListing.trim",
        "retailListing.description",
    ):
        val = get_path(row, field)
        if val:
            haystacks.append(str(val).lower())
    blob = " | ".join(haystacks)
    return any(tok in blob for tok in AWD_TOKENS)


def listing_fields(row: dict) -> dict:
    vin = (get_path(row, "vehicle.vin") or row.get("vin") or "").upper()
    return {
        "vin": vin,
        "year": get_path(row, "vehicle.year"),
        "trim": get_path(row, "vehicle.trim") or "",
        "price": get_path(row, "retailListing.price"),
        "miles": get_path(row, "retailListing.miles")
        or get_path(row, "retailListing.mileage"),
        "city": get_path(row, "retailListing.city") or "",
        "state": get_path(row, "retailListing.state") or "",
        "dealer": get_path(row, "retailListing.dealerName")
        or get_path(row, "retailListing.dealer") or "",
        "url": get_path(row, "retailListing.vdpUrl")
        or get_path(row, "retailListing.url") or "",
        "ext": get_path(row, "vehicle.exteriorColor")
        or get_path(row, "retailListing.exteriorColor")
        or get_path(row, "vehicle.color") or "",
        "int": get_path(row, "vehicle.interiorColor")
        or get_path(row, "retailListing.interiorColor") or "",
    }


def fmt_money(n) -> str:
    try:
        return f"${int(n):,}"
    except (TypeError, ValueError):
        return "price n/a"


def fmt_miles(n) -> str:
    try:
        return f"{int(n):,} mi"
    except (TypeError, ValueError):
        return "miles n/a"


def ntfy_push(title: str, body: str, url: str | None):
    """ntfy reads headers as latin-1: keep Title strictly ASCII (SurfBud lesson)."""
    headers = {
        "Title": title.encode("ascii", "ignore").decode("ascii"),
        "Priority": "high",
        "Tags": "minibus",
    }
    if url:
        headers["Click"] = url
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as exc:  # alerting must never kill the run
        print(f"ntfy push failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------- digest
def build_digest(matches: list[dict], checked_at: str):
    rows = []
    for m in sorted(matches, key=lambda x: (x["price"] is None, x["price"] or 0)):
        flags = []
        if m["price"] is not None and m["price"] < SUSPICIOUS_PRICE:
            flags.append("verify title/history")
        if m["miles"] is not None and m["miles"] > HIGH_MILES:
            flags.append("high miles")
        flag_html = (
            f'<span class="flag">{html.escape(" - ".join(flags))}</span>' if flags else ""
        )
        link_open = f'<a href="{html.escape(m["url"])}" target="_blank" rel="noopener">' if m["url"] else "<span>"
        link_close = "</a>" if m["url"] else "</span>"
        loc = ", ".join(p for p in (m["city"], m["state"]) if p)
        colors = " / ".join(p for p in (m.get("ext", ""), m.get("int", "")) if p)
        sub_bits = [fmt_miles(m["miles"])]
        if colors:
            sub_bits.append(colors)
        sub_bits.extend([loc, str(m["dealer"])])
        sub = " &middot; ".join(html.escape(str(b)) for b in sub_bits if b)
        rows.append(
            f"""<li class="card">
  <div class="price">{fmt_money(m["price"])}</div>
  <div class="meta">
    {link_open}{m["year"]} Sienna {html.escape(str(m["trim"]))} AWD{link_close}
    <span class="sub">{sub}</span>
    {flag_html}
    <span class="vin">{html.escape(m["vin"])}</span>
  </div>
</li>"""
        )

    cards = "\n".join(rows) if rows else '<li class="empty">No live matches under the cap right now. The watcher runs twice daily.</li>'

    DIGEST_FILE.write_text(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Van Watch</title>
<style>
  :root {{
    --road: #191d24;      /* asphalt */
    --paper: #f2efe8;     /* map paper */
    --paint: #e8c547;     /* lane-line yellow */
    --pine: #3f6b4f;      /* roadside pine */
    --ink: #23272f;
    --muted: #7c8492;
  }}
  * {{ box-sizing: border-box; margin: 0; }}
  body {{
    background: var(--paper);
    color: var(--ink);
    font: 16px/1.55 "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
    padding: 0 1.25rem 4rem;
  }}
  header {{
    max-width: 720px; margin: 0 auto; padding: 3rem 0 1.5rem;
    border-bottom: 4px double var(--ink);
  }}
  h1 {{
    font-family: "Avenir Next Condensed", "Arial Narrow", "Helvetica Neue", sans-serif;
    font-weight: 700; letter-spacing: .14em; text-transform: uppercase;
    font-size: clamp(1.6rem, 5vw, 2.4rem);
  }}
  h1 .lane {{ color: transparent; -webkit-text-stroke: 1.5px var(--ink); }}
  .tagline {{ color: var(--muted); font-style: italic; margin-top: .35rem; }}
  .stamp {{
    display: inline-block; margin-top: .8rem; padding: .15rem .6rem;
    border: 2px solid var(--pine); color: var(--pine); border-radius: 3px;
    font-family: "Avenir Next Condensed", "Arial Narrow", sans-serif;
    font-size: .8rem; letter-spacing: .12em; text-transform: uppercase;
    transform: rotate(-1.2deg);
  }}
  ul {{ max-width: 720px; margin: 1.5rem auto 0; padding: 0; list-style: none; }}
  .card {{
    display: flex; gap: 1.1rem; align-items: baseline;
    padding: 1rem 0; border-bottom: 1px solid #d8d3c6;
  }}
  .price {{
    font-family: "Avenir Next Condensed", "Arial Narrow", sans-serif;
    font-weight: 700; font-size: 1.35rem; min-width: 5.2rem;
    color: var(--road);
  }}
  .meta a {{ color: var(--ink); text-decoration-color: var(--paint); text-decoration-thickness: 3px; }}
  .sub, .vin {{ display: block; color: var(--muted); font-size: .85rem; }}
  .vin {{ letter-spacing: .06em; }}
  .flag {{
    display: inline-block; margin-top: .2rem; padding: .05rem .45rem;
    background: var(--paint); color: var(--ink); border-radius: 2px;
    font-family: "Avenir Next Condensed", "Arial Narrow", sans-serif;
    font-size: .75rem; letter-spacing: .08em; text-transform: uppercase;
  }}
  .empty {{ padding: 2rem 0; color: var(--muted); font-style: italic; }}
  footer {{
    max-width: 720px; margin: 2.5rem auto 0; color: var(--muted); font-size: .85rem;
  }}
</style>
</head>
<body>
<header>
  <h1>Van <span class="lane">Watch</span></h1>
  <p class="tagline">2021+ Toyota Sienna &middot; AWD hybrid &middot; value-priced under {fmt_money(PRICE_CAP)} &middot; nationwide, cheapest first</p>
  <span class="stamp">checked {checked_at}</span>
</header>
<ul>
{cards}
</ul>
<footer>Runs twice daily via GitHub Actions. Prices below {fmt_money(SUSPICIOUS_PRICE)} get a caution tag: nationwide cheapest-first surfaces flood-region and rebuilt-title inventory; always pull the Carfax and decode the VIN before travel.</footer>
</body>
</html>
""", encoding="utf-8")


# ---------------------------------------------------------------- main
def main():
    seen = set()
    if STATE_FILE.exists():
        try:
            seen = set(json.loads(STATE_FILE.read_text()))
        except json.JSONDecodeError:
            pass

    now = datetime.now(timezone.utc)
    last_deep = None
    if WATCH_STATE_FILE.exists():
        try:
            raw = json.loads(WATCH_STATE_FILE.read_text()).get("last_deep")
            if raw:
                last_deep = datetime.fromisoformat(raw)
        except (json.JSONDecodeError, ValueError):
            pass
    deep_due = last_deep is None or (now - last_deep).total_seconds() > DEEP_INTERVAL_HOURS * 3600
    deep = deep_due or os.environ.get("FORCE_DEEP") == "1"

    params = {
        "vehicle.make": "Toyota",
        "vehicle.model": "Sienna",
        "vehicle.year": f"{MIN_YEAR}-2035",
        "retailListing.price": f"1-{PRICE_CAP}",
        "limit": PAGE_LIMIT,
    }
    if deep:
        params["sort"] = "retailListing.price.asc"
    # shallow: omit sort -> API default updatedAt.desc (newest activity first)

    page_budget = MAX_PAGES if deep else MAX_PAGES_SHALLOW

    matches: list[dict] = []
    cursor = None
    for page in range(page_budget):
        q = dict(params)
        if cursor:
            q["cursor"] = cursor
        else:
            q["page"] = page + 1  # harmless if API ignores it
        try:
            payload = api_get(q)
        except Exception as exc:
            print(f"API error on page {page + 1}: {exc}", file=sys.stderr)
            break

        rows = payload.get("data", payload if isinstance(payload, list) else [])
        if not rows:
            break

        for row in rows:
            if not is_awd(row):
                continue
            if is_branded(row):
                continue  # disclosed salvage/rebuilt/flood/lemon title
            f = listing_fields(row)
            if not f["vin"]:
                continue
            if f["price"] is not None and f["price"] > PRICE_CAP:
                continue
            if EXCLUDE_TRIM_RE.search(str(f["trim"])):
                continue  # LE = fabric interior; XLE/Woodland/Limited/Platinum pass
            if (
                f["price"] is not None
                and f["miles"] is not None
                and f["price"] > VALUE_BASE - VALUE_PER_MILE * f["miles"]
            ):
                continue  # overpriced for the miles
            matches.append(f)

        cursor = (
            get_path(payload, "pagination.nextCursor")
            or get_path(payload, "apiPagination.nextCursor")
            or payload.get("nextCursor")
        )
        if not cursor and len(rows) < PAGE_LIMIT:
            break

    new = [m for m in matches if m["vin"] not in seen]
    for m in new:
        loc = ", ".join(p for p in (m["city"], m["state"]) if p)
        colors = " / ".join(p for p in (m["ext"], m["int"]) if p)
        title = f"Van Watch: {m['year']} Sienna {m['trim']} AWD {fmt_money(m['price'])}"
        body_lines = [f"{fmt_miles(m['miles'])} - {loc}"]
        if colors:
            body_lines.append(colors)
        body_lines.append(str(m["dealer"]))
        body_lines.append(f"VIN {m['vin']}")
        ntfy_push(title, "\n".join(body_lines), m["url"] or None)

    checked_at = now.strftime("%Y-%m-%d %H:%M UTC")
    if deep:
        build_digest(matches, checked_at)
        WATCH_STATE_FILE.write_text(json.dumps({"last_deep": now.isoformat()}))

    STATE_FILE.write_text(
        json.dumps(sorted(seen | {m["vin"] for m in matches}), indent=0)
    )
    mode = "deep" if deep else "shallow"
    print(
        f"[{mode}] {len(matches)} AWD matches under {fmt_money(PRICE_CAP)}; "
        f"{len(new)} new alerted; checked {checked_at}"
    )


if __name__ == "__main__":
    main()
