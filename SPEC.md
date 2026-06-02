# Weather Bot — Project Spec & Website Handoff

A Polymarket weather-temperature trading bot (terminal, Python stdlib only) plus
a public-dashboard goal. This doc is the source of truth: any new chat building
the website should read this first, not rely on prior conversation context.

---

## 1. What this is

For each city + day, Polymarket runs a "highest temperature in {city} on {date}"
market split into temperature brackets. The bot:

1. Pulls a **173-member weather ensemble** (Open-Meteo) for the city/day.
2. Converts the ensemble spread into a **probability per bracket**.
3. Compares model probability vs the **Polymarket price** to find edges.
4. Logs every forecast and its **resolution** so accuracy/bias can be measured.

No trades are placed automatically — signals are confirmed by hand. The data
side (forecast → resolution tracking) is the part the website will visualise.

**Owner context:** based in Sydney (UTC+10, AEST in June). Python beginner,
building this partly as YouTube content. Wallet `0xD13CC6b93B79555B5eEe81A5B932aFA0Bf675992`,
handle `@rainmoneymaker`. Prefers "remove emotion, trade off the numbers."

---

## 2. Cities

| Code | City        | Unit | Brackets        | NWS station | CITY_BIAS (°) |
|------|-------------|------|-----------------|-------------|---------------|
| NYC  | New York    | °F   | 2°F windows     | KLGA        | +1.4          |
| MIA  | Miami       | °F   | 2°F windows     | KMIA        | −2.2          |
| CHI  | Chicago     | °F   | 2°F windows     | KORD        |  0.0          |
| LAX  | Los Angeles | °F   | 2°F windows     | KLAX        |  0.0          |
| TYO  | Tokyo       | °C   | single-degree   | none (intl) |  0.0          |
| SIN  | Singapore   | °C   | single-degree   | none (intl) |  0.0          |

US markets use **2°F** brackets (e.g. `68-69°F`, plus open-ended `70°F or higher`
/ `59°F or below`). Asian markets use **single-degree °C** brackets (e.g. `25°C`,
`27°C or higher`) — exact-hit, structurally harder, behave like YES bets.

---

## 3. Bias convention (important)

`adj_mean = ensemble_mean − CITY_BIAS`

- **Positive** CITY_BIAS = ensemble runs **warm** → subtract to get the real
  expectation.
- **Negative** CITY_BIAS = ensemble runs **cool** → subtracting a negative adds.

`error = actual_high − adj_mean`. **Positive error = ensemble ran cool**
(under-forecast). The per-city mean error suggests a bias correction:
`suggested CITY_BIAS = current CITY_BIAS − mean(recorded error)`.

Rule: only apply a CITY_BIAS correction once there are **5+ verified results**
for that city. Current open question: CHI recorded error ≈ +3.4 (suggests
−3.4) but only 2 recorded rows so far — left at 0.0 until more accumulate.
NYC at +1.4 may have the wrong sign (every NYC miss ran hotter than model).

---

## 4. Data files (schemas)

All CSVs live in the repo root next to `bot.py`.

### `forecasts.csv` — the headline dataset (one row per city/day)
Every evaluated city-day, trade or not. This is what the website renders.

| Column          | Meaning |
|-----------------|---------|
| `date`          | Market date (YYYY-MM-DD) |
| `city`          | City code (NYC/MIA/CHI/LAX/TYO/SIN) |
| `unit`          | `°F` or `°C` |
| `ensemble_mean` | Raw mean of the 173 member highs |
| `adj_mean`      | Bias-corrected mean (`ensemble_mean − CITY_BIAS`) |
| `n_members`     | Ensemble members used |
| `range_lo`/`range_hi` | Coolest/warmest member high |
| `nws_high`      | NWS daily-high forecast (US only, cross-check) |
| `wttr_high`     | wttr.in forecast high (US only, ~Weather Underground proxy) |
| `market_fav`    | Highest-priced Polymarket bracket (the crowd's pick) |
| `fav_prob`      | That bracket's price (0–1) |
| `actual_high`   | Resolved daily high (blank until settled) |
| `error`         | `actual_high − adj_mean` (blank until settled) |
| `source`        | `recorded` (live trade-time mean) or `reconstructed` (refetched after the fact) |
| `logged_at`     | Timestamp the row was written |

**`source` caveat:** `reconstructed` rows were refetched via Open-Meteo
`past_days` after the fact — they approximate the analysis, NOT the lead-time
forecast we traded on, so their `error` understates true bias. **For
calibration, trust `recorded` rows only.** Reconstructed rows anchor actuals +
favourites. The `past_days` archive only reaches ~a week back, so older days
are actual-only (mean blank).

### `signals.csv` — the trade log (one row per traded bracket)
16 columns (newer rows): `date, bracket, bracket_lo, bracket_hi, market_price,
model_prob, edge, direction, trade_size, ensemble_mean, adj_mean, nws_forecast,
logged_at, actual_high, pnl, result`.
**Legacy 13-column rows exist** (pre-dating ensemble_mean/adj_mean/nws_forecast)
— any reader MUST branch on `len(cells)` (16 vs 13); a naive DictReader
misaligns the old rows. `result` ∈ {WIN, LOSS, CUT}. `bracket` may carry a city
tag like `(CHI)`; untagged = NYC.

### `favorites.csv` — market-favourite tracker (one row per city/day)
`date, city, fav_bracket, fav_lo, fav_hi, fav_prob, actual_high, fav_hit`.
Measures how often the crowd's single favourite bracket actually resolves
(answers "is betting NO against the favourite sound?"). actual/hit are joined
from signals.csv at report time.

---

## 5. Pipeline (how a row is produced)

```
Open-Meteo ensemble API ──► 173 member daily highs (6am–11pm window)
   models: ecmwf_ifs025, ecmwf_aifs025, ncep_gefs025, icon_seamless
        │
        ├─ mean ──► adj_mean (− CITY_BIAS)
        ├─ votes per bracket ──► model probability
        │
Polymarket Gamma API ──► market price per bracket + the favourite
   event slug: highest-temperature-in-{slug}-on-{month}-{day}-{year}
   slugs: nyc, miami, chicago, los-angeles, tokyo, singapore
        │
        ▼
   edge = model_prob − market_price  ──► NO/YES signals (thresholds below)
        │
        ▼
   forecasts.csv row (actual/error blank)
        │
   ... market settles ...
        │
   Resolution:
     US  ─► NWS station observations (api.weather.gov), exact, auto
     TYO/SIN ─► MANUAL (`--set-actual`); Open-Meteo grid runs ~2°C off the
                JMA station Polymarket settles on, so it is hint-only
```

**Signal thresholds:** NO needs edge ≥ 5pp (many brackets to miss → easier);
YES needs edge ≥ 15pp (must hit one exact bracket → harder). NO bets within
`MEAN_BUFFER = 3°F` of the adjusted mean are flagged higher-risk.

---

## 6. Commands (the daily routine)

```bash
python3 bot.py [DATE] [CITY]          # interactive analysis for one city/day
python3 bot.py --track [DATE]         # sweep all 6 markets → forecasts.csv (no trades)
python3 bot.py --resolve              # auto-fill US actuals (NWS); hint TYO/SIN
python3 bot.py --set-actual DATE CITY VALUE   # manual resolution for TYO/SIN
python3 bot.py --backfill             # rebuild forecasts.csv history from signals/favorites
python3 bot.py --forecastreport       # per-city accuracy + bias signal
python3 bot.py --favreport            # favourite-vs-resolved hit rate
python3 bot.py --log                  # log signals while analysing (interactive path)
python3 bot.py --html | --history     # local HTML dashboards (existing)
```

**Typical day (Sydney evening):** `--track`, then next day `--resolve` and
`--set-actual` for Tokyo/Singapore, then `--forecastreport`.

---

## 7. Resolution sources

- **US (NYC/MIA/CHI/LAX):** NWS station observations
  `api.weather.gov/stations/{KLGA|KMIA|KORD|KLAX}/observations` — max temperature
  over the local calendar day, °C→°F. Validated exact (KORD = 72°F matched our
  hand-logged actual). This is automatic via `--resolve`.
- **International (TYO/SIN):** No NWS station. Open-Meteo's gridded daily max
  was ~2°C off the value Polymarket settled on (Tokyo Jun 2: grid 24°C vs market
  26°C) — enough to flip a single-degree market. So actuals are entered by hand
  with `--set-actual` after checking the resolution on Polymarket. Open question:
  identify the exact JMA/station feed to automate this later.

---

## 8. Website goals (Phase 3 — the new build)

A **public, static** site (no backend) that reads the CSVs and renders:

1. **Live forecasts** — today's markets across all 6 cities: ensemble mean,
   adjusted mean, distribution/range, market favourite, our edge.
2. **Accuracy / calibration** — per-city forecast error over time; how the
   ensemble's adjusted mean tracks the actual high; the bias signal.
3. **Track record** — the trade log: win rate, cumulative P&L, per-signal detail
   (already prototyped in `--history` → `history.html`).
4. **Favourite vs resolved** — how often the crowd's favourite bracket hits.

**Constraints / preferences:**
- Free hosting suited to a content creator — **GitHub Pages** is the leading
  candidate (static, free, custom domain support).
- The bot is **stdlib-only**; keep the data-production side dependency-free.
  The website can use whatever frontend stack makes sense (it is a separate
  concern), but the simplest path is static HTML/CSS/JS reading the CSVs (or a
  small JSON the bot emits).
- Existing dashboards (`generate_html`, `generate_history_html` in `bot.py`)
  show the visual language already in use: dark theme, mono/sans mix, accent
  `#5b8cf5`, win `#3d9e6e` / loss `#c0392b`. Reuse for consistency.
- Honesty matters: distinguish `recorded` vs `reconstructed` data; label
  pending/unsettled days; don't present reconstructed error as calibration.

**Suggested first step for the website chat:** decide whether the site reads the
CSVs directly (fetch + parse client-side) or whether `bot.py` gains an
`--export-json` command that emits a single `site_data.json` the page consumes.
The latter keeps parsing logic (13 vs 16 column handling, joins) in Python where
it already exists, and is recommended.

---

## 9. Repo orientation

- `bot.py` — everything (fetch, probabilities, signals, logging, reports, HTML).
  ~1900 lines, stdlib only.
- `signals.csv`, `forecasts.csv`, `favorites.csv` — the datasets above.
- `dashboard.html`, `history.html` — generated local dashboards.
- Other files in the dir (aviation_*, intraday*, smart_money.py, backtest.py)
  are unrelated experiments — **not part of this project**, leave untracked.
