# Van Watch

Surf Bud's sibling. Watches the national market for **2021+ Toyota Sienna, AWD, under $41,500** (all 2021+ Siennas are hybrid) and pings your phone when a new one appears.

## How it works
- GitHub Actions runs `check_listings.py` twice daily (7am / 7pm ET)
- Queries the [auto.dev Listings API](https://auto.dev) nationwide, sorted cheapest-first
- Filters to AWD via drivetrain / trim / description fields
- New VINs push to [ntfy.sh](https://ntfy.sh); all live matches render to a GitHub Pages digest
- Seen VINs persist in `seen_vins.json`, committed back each run
- Listings priced under $24k or over 120k miles get a caution tag (flood / rebuilt / fleet risk)

## Setup (5 min)
1. Free API key from [auto.dev](https://auto.dev)
2. Repo secrets (Settings > Secrets and variables > Actions): `AUTODEV_API_KEY`, `NTFY_TOPIC`
3. Enable Pages: Settings > Pages > Deploy from branch > `main` / `docs`
4. Subscribe to your topic in the ntfy app
5. Actions tab > Van Watch > Run workflow to test

## Tuning (top of `check_listings.py`)
| Variable | Current |
|---|---|
| `PRICE_CAP` | 41500 |
| `MIN_YEAR` | 2021 |
| `MAX_PAGES` | 15 |
| `SUSPICIOUS_PRICE` | 24000 |
| `HIGH_MILES` | 120000 |
