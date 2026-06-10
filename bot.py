#!/usr/bin/env python3
"""
Weather prediction bot.
Usage:
  python3 bot.py [YYYY-MM-DD]          — terminal output
  python3 bot.py [YYYY-MM-DD] --html   — write dashboard.html and open it
  python3 bot.py [--size N]            — trade size in USD (default 20)

No trades are placed automatically — each must be manually confirmed.
"""

import csv
import json
import math
import os
import subprocess
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Tuple

LOG_FILE = Path(__file__).parent / "signals.csv"
LOG_FIELDS = [
    "date", "bracket", "bracket_lo", "bracket_hi",
    "market_price", "model_prob", "edge", "direction", "trade_size",
    "ensemble_mean", "adj_mean", "nws_forecast",
    "logged_at", "actual_high", "pnl", "result",
]

# Market-favourite tracking: one row per date+city recording the single
# highest-priced bracket (the market's pick) so we can measure how often the
# crowd's favourite actually resolves. Answers "is betting against the
# favourite sound, or does the market see something we don't?"
FAV_FILE = Path(__file__).parent / "favorites.csv"
FAV_FIELDS = [
    "date", "city", "fav_bracket", "fav_lo", "fav_hi",
    "fav_prob", "actual_high", "fav_hit",
]

# Ensemble forecast dataset: one row per (date, city) — EVERY day the bot
# evaluates a city, not just days we traded. Captures what the ensemble
# predicted vs what actually resolved, so per-city bias can be calibrated off
# the full record. `error` = actual − adj_mean (positive = we ran cool /
# under-forecast). `source` = "recorded" (the live trade-time mean) vs
# "reconstructed" (refetched after the fact via past_days — approximates the
# analysis, NOT the lead-time forecast, so its error understates true bias).
FORECAST_FILE = Path(__file__).parent / "forecasts.csv"
FORECAST_FIELDS = [
    "date", "city", "unit", "ensemble_mean", "adj_mean", "n_members",
    "range_lo", "range_hi", "nws_high", "wttr_high",
    "market_fav", "fav_prob", "actual_high", "error", "source", "logged_at",
]

# Coordinates for every city we log — superset of CITY_COORDS, adding the Asian
# single-degree °C markets (Tokyo, Singapore) that resolve outside the main US
# pipeline. Tuple: (lat, lon, timezone, open-meteo temperature_unit).
FORECAST_COORDS = {
    "NYC": (40.7128, -74.0060, "America/New_York",    "fahrenheit"),
    "MIA": (25.7617, -80.1918, "America/New_York",    "fahrenheit"),
    "CHI": (41.8827, -87.6233, "America/Chicago",     "fahrenheit"),
    "LAX": (33.9425, -118.408, "America/Los_Angeles", "fahrenheit"),
    "TYO": (35.6762, 139.6503, "Asia/Tokyo",          "celsius"),
    "SIN": (1.3521,  103.8198, "Asia/Singapore",      "celsius"),
}

# ── Config ────────────────────────────────────────────────────────────────────

CITY        = "NYC"
LATITUDE    = 40.7128
LONGITUDE   = -74.0060
TIMEZONE    = "America/New_York"

# Bias correction (°F): how much each model runs warm (+) or cold (-).
# Key: (city, model_tag, season_index)  season: 0=DJF 1=MAM 2=JJA 3=SON
# Positive = model runs warm → we subtract from the forecast.
BIAS: dict = {
    # ("NYC", "ncep_gefs025", 1): 2.0,  # example: GFS +2°F warm in NYC spring
}

# Edge thresholds (percentage points).
# NO bets just need the temp to miss a single 2°F window — many paths to win.
# YES bets need the temp to hit one specific bracket — only one path to win.
# Higher bar for YES to compensate for that structural disadvantage.
NO_EDGE_THRESHOLD  = 5.0
YES_EDGE_THRESHOLD = 15.0

# NO bets within this many °F of the ensemble mean are flagged as higher risk.
# The ensemble mean is the model's best guess — if the bracket sits right next
# to it, a small forecast error is enough to lose the NO.
MEAN_BUFFER = 3.0

# Per-city bias correction (°F): how much the ensemble mean over/under-shoots reality.
# Positive  = ensemble runs WARM  → we subtract to get the "real" expectation.
# Negative  = ensemble runs COOL  → we subtract a negative (i.e. we add).
#
# RULE: only apply a bias correction once we have 5+ verified results for that city.
#       Assumed corrections are risky — ensemble bias varies by city and season.
#
# NYC  -1.0°F: updated 2026-06-04. Ensemble under-shoots in late May/June.    [ 2 recorded ⚠ low-n]
#              Prior +1.4 was from Apr–May spring data (13 pts); regime shifted for summer.
# MIA  -4.8°F: updated 2026-06-04. Ensemble still short after old -2.2 correction. [ 2 recorded ⚠ low-n]
# CHI  -3.7°F: updated 2026-06-04. Actual consistently ~3.7°F hotter than ensemble. [ 4 recorded ⚠ low-n]
# LAX  +4.3°F: updated 2026-06-04. Ensemble runs ~4.3°F hot; actual cooler.         [ 2 recorded ⚠ low-n]
#              NOTE: Polymarket resolves LAX vs a different station than NWS KLAX — actual
#              spread may be wider. Watch for further correction after more data.
CITY_BIAS = {
    "NYC": -1.0,   # updated 2026-06-04: 2 recorded pts, actual +2.4°F hotter → was +1.4
    "MIA": -3.6,   # updated 2026-06-10: 5 recorded pts, over-correcting by 1.2°F → was -4.8
    "CHI": -3.7,   # updated 2026-06-04: 4 recorded pts, actual +3.7°F hotter → was 0.0
    "LAX":  6.5,   # updated 2026-06-10: 5 recorded pts, still under-correcting by 1.3°F → was +5.5
}
MODEL_WARM_BIAS = CITY_BIAS.get(CITY, 0.0)

# Per-city geographic config — coords drive the Open-Meteo ensemble fetch.
# Slug is the city token in Polymarket event URLs.
CITY_COORDS = {
    #  city  : (latitude, longitude, timezone,         polymarket_slug)
    "NYC": (40.7128, -74.0060, "America/New_York",  "nyc"),
    "MIA": (25.7617, -80.1918, "America/New_York",  "miami"),
    "CHI": (41.8827, -87.6233, "America/Chicago",   "chicago"),
    "LAX": (33.9425, -118.408, "America/Los_Angeles", "los-angeles"),
}

# Default trade size in USD per signal (user can override with --size flag)
DEFAULT_TRADE_SIZE = 20

# ── Helpers ───────────────────────────────────────────────────────────────────

def _season(d: date) -> int:
    m = d.month
    if m in (12, 1, 2): return 0
    if m in (3, 4, 5):  return 1
    if m in (6, 7, 8):  return 2
    return 3

def _bias_for(key: str, d: date) -> float:
    for tag in ("ecmwf_ifs025", "ecmwf_aifs025", "ncep_gefs025", "icon_seamless"):
        if tag in key:
            return BIAS.get((CITY, tag, _season(d)), 0.0)
    return 0.0

# ── Step 1: Fetch ensemble ────────────────────────────────────────────────────

def fetch_ensemble(target: date) -> list:
    """Return bias-corrected daily highs (6am–11pm) for every ensemble member."""
    tz = TIMEZONE.replace("/", "%2F")
    url = (
        "https://ensemble-api.open-meteo.com/v1/ensemble"
        f"?latitude={LATITUDE}&longitude={LONGITUDE}"
        "&hourly=temperature_2m"
        "&models=ecmwf_ifs025,ecmwf_aifs025,ncep_gefs025,icon_seamless"
        "&temperature_unit=fahrenheit"
        "&forecast_days=7"
        f"&timezone={tz}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "weather-bot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())

    hourly = data["hourly"]
    times  = hourly["time"]
    target_str = target.isoformat()

    # Indices for 6am–11pm on the target date
    day_idx = [
        i for i, t in enumerate(times)
        if t.startswith(target_str) and 6 <= int(t[11:13]) <= 23
    ]
    if not day_idx:
        raise ValueError(f"No hourly data for {target_str} — outside the 7-day window?")

    highs = []
    for key, values in hourly.items():
        if not key.startswith("temperature_2m"):
            continue
        temps = [values[i] for i in day_idx if values[i] is not None]
        if not temps:
            continue
        bias = _bias_for(key, target)
        highs.append(max(temps) - bias)

    return sorted(highs)

# ── Step 2: Bracket probabilities ─────────────────────────────────────────────

def bracket_probs(highs: list, brackets: list) -> dict:
    """Count ensemble votes per bracket → probability."""
    n = len(highs)
    counts = {label: 0 for label, _, _ in brackets}
    for h in highs:
        for label, lo, hi in brackets:
            if lo <= h <= hi:
                counts[label] += 1
                break
    return {label: counts[label] / n for label, _, _ in brackets}

# ── Step 3: Fetch Polymarket data ─────────────────────────────────────────────

def fetch_polymarket(target: date, city_slug: Optional[str] = None) -> Optional[Tuple[dict, dict]]:
    """
    Return (prices, slugs) where both are {outcome_label: value}, or None on failure.
    prices: {label: probability 0-1}
    slugs:  {label: market_slug}
    city_slug overrides the Polymarket city token (defaults to the active CITY) —
    used by the --track sweep to fetch tokyo/singapore without touching globals.
    """
    day   = target.strftime("%-d")
    month = target.strftime("%B").lower()
    year  = target.strftime("%Y")
    if city_slug is None:
        city_slug = CITY_COORDS.get(CITY, CITY_COORDS["NYC"])[3]
    event_slug = f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    url = f"https://gamma-api.polymarket.com/events?slug={event_slug}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "weather-bot/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            events = json.loads(r.read())
    except Exception as e:
        print(f"  [WARN] Polymarket fetch failed: {e}")
        return None

    if not events:
        print(f"  [WARN] No event found for slug: {event_slug}")
        return None

    prices: dict = {}
    slugs:  dict = {}
    for market in events[0].get("markets", []):
        label = market.get("groupItemTitle", "").strip()
        if not label:
            continue
        raw_price = (
            market.get("lastTradePrice")
            or market.get("bestBid")
            or market.get("bestAsk")
            or 0
        )
        try:
            prices[label] = float(raw_price)
        except (TypeError, ValueError):
            pass

        mslug = market.get("slug", "").strip()
        if mslug:
            slugs[label] = mslug

    return (prices, slugs) if prices else None

# ── Step 4: Parse bracket labels ──────────────────────────────────────────────

def parse_brackets(labels: list) -> list:
    """Convert Polymarket outcome strings to (label, lo, hi) tuples.

    Handles all observed label formats:
      "68-69°F"          → (68, 69)
      "70°F or higher"   → (70, +inf)
      "51°F or below"    → (-inf, 51)
      "≥72°F"            → (72, +inf)
      "≤53°F"            → (-inf, 53)
      "88-89°F (MIA)"    → (88, 89)   [city tag stripped]
    """
    import re
    brackets = []
    for raw in labels:
        label = raw.strip()
        # Strip city tag suffix like " (MIA)" or " (CHI)" for parsing only
        clean = re.sub(r'\s*\([A-Z]{2,4}\)', '', label)
        clean = clean.replace("°F", "").replace("°", "").strip()

        # "70 or higher" / "70 or above"
        m = re.fullmatch(r'([\d.]+)\s+or\s+(?:higher|above)', clean, re.IGNORECASE)
        if m:
            brackets.append((label, float(m.group(1)), math.inf))
            continue

        # "51 or below" / "51 or under"
        m = re.fullmatch(r'([\d.]+)\s+or\s+(?:below|under)', clean, re.IGNORECASE)
        if m:
            brackets.append((label, -math.inf, float(m.group(1))))
            continue

        # Symbol forms: ≥72 / >=72
        if "≥" in clean or ">=" in clean:
            lo = float(clean.replace("≥", "").replace(">=", "").strip())
            brackets.append((label, lo, math.inf))
            continue

        # Symbol forms: ≤53 / <=53
        if "≤" in clean or "<=" in clean:
            hi = float(clean.replace("≤", "").replace("<=", "").strip())
            brackets.append((label, -math.inf, hi))
            continue

        # Range: "68-69" (split on first hyphen, ignoring leading minus for negatives)
        m = re.fullmatch(r'(-?[\d.]+)-(-?[\d.]+)', clean)
        if m:
            brackets.append((label, float(m.group(1)), float(m.group(2))))
            continue

        # Single value: "66"
        try:
            v = float(clean)
            brackets.append((label, v, v))
        except ValueError:
            pass

    return brackets

def _fallback_brackets() -> list:
    bkts = [("≤53", -math.inf, 53.0)]
    for lo in range(54, 72, 2):
        bkts.append((f"{lo}-{lo+1}", float(lo), float(lo + 1)))
    bkts.append(("≥72", 72.0, math.inf))
    return bkts

# ── Trade execution (requires explicit confirmation) ──────────────────────────

def execute_trade(slug: str, outcome: str, amount: int) -> None:
    """Run bullpen buy command after printing it for the user to see."""
    cmd = ["bullpen", "polymarket", "buy", slug, outcome, str(amount), "--yes"]
    print(f"\n  Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"  [ERROR] Trade command exited with code {result.returncode}")

# ── Forecast cross-check (NWS + wttr.in) ─────────────────────────────────────

def fetch_forecast_crosscheck(target: date) -> dict:
    """
    Pull two secondary forecast sources and return their predicted highs (°F).
    Resolution source for Polymarket temp markets is Weather Underground (KORD/etc),
    which often diverges from NWS. wttr.in uses commercial models closer to that source.

    Returns dict with keys:
      nws_high   : int or None  — NWS daily high forecast (°F)
      wttr_high  : int or None  — wttr.in commercial forecast high (°F)
      errors     : list[str]
    """
    result: dict = {"nws_high": None, "wttr_high": None, "errors": []}

    # 1. NWS daily forecast ---------------------------------------------------
    try:
        lat, lon = LATITUDE, LONGITUDE
        points_url = f"https://api.weather.gov/points/{lat},{lon}"
        req = urllib.request.Request(points_url, headers={"User-Agent": "weather-bot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            pts = json.loads(r.read())
        forecast_url = pts["properties"]["forecast"]
        req2 = urllib.request.Request(forecast_url, headers={"User-Agent": "weather-bot/1.0"})
        with urllib.request.urlopen(req2, timeout=10) as r:
            fc = json.loads(r.read())
        target_str = target.strftime("%Y-%m-%d")
        for period in fc["properties"]["periods"]:
            # Daytime period on the target date
            if period["startTime"][:10] == target_str and period.get("isDaytime", True):
                result["nws_high"] = period["temperature"]
                break
        # Fallback: first period that starts on target date
        if result["nws_high"] is None:
            for period in fc["properties"]["periods"]:
                if period["startTime"][:10] == target_str:
                    result["nws_high"] = period["temperature"]
                    break
    except Exception as e:
        result["errors"].append(f"NWS: {e}")

    # 2. wttr.in commercial forecast ------------------------------------------
    # Uses lat/lon for precision; returns The Weather Company data (same family as
    # Weather Underground which is Polymarket's resolution source).
    try:
        url = f"http://wttr.in/{LATITUDE},{LONGITUDE}?format=j1"
        req = urllib.request.Request(url, headers={"User-Agent": "weather-bot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            wdata = json.loads(r.read())
        today_offset = (target - date.today()).days
        weather_days = wdata.get("weather", [])
        if 0 <= today_offset < len(weather_days):
            result["wttr_high"] = int(weather_days[today_offset]["maxtempF"])
    except Exception as e:
        result["errors"].append(f"wttr.in: {e}")

    return result


def _print_crosscheck(real: float, cc: dict) -> None:
    """Print a formatted cross-check summary and flag source disagreement."""
    nws  = cc.get("nws_high")
    wttr = cc.get("wttr_high")

    sources = {"Ensemble (adj)": real}
    if nws  is not None: sources["NWS daily"]     = float(nws)
    if wttr is not None: sources["wttr.in (WU~)"] = float(wttr)

    print("  Forecast cross-check:")
    for label, val in sources.items():
        delta = val - real
        arrow = f"  ↑ {delta:+.1f}°F vs ensemble" if abs(delta) > 1 else "  ✓ near ensemble"
        print(f"    {label:<18} {val:.1f}°F{arrow}")

    vals = list(sources.values())
    span = max(vals) - min(vals)
    if span >= 8:
        print(f"  ⚠️  WIDE SPREAD ({span:.0f}°F across sources) — high uncertainty, consider half-size")
    elif span >= 4:
        print(f"  ⚠️  Moderate spread ({span:.0f}°F) — sources disagree, trade conservatively")
    else:
        print(f"  ✅  Sources aligned (span {span:.1f}°F)")

    # Note that wttr.in is the closest proxy to WU (Polymarket's resolution source)
    if wttr is not None and nws is not None:
        if abs(wttr - nws) >= 5:
            print(f"  📌  wttr.in vs NWS gap: {abs(wttr-nws):.0f}°F — "
                  f"wttr.in tracks closer to Wunderground (resolution source)")
    print()


# ── Market window check ───────────────────────────────────────────────────────

# Approximate UTC offsets per timezone (DST-aware for common periods).
# Update if cities shift DST — these cover Northern Hemisphere summer / Southern Hemisphere autumn.
_TZ_UTC_OFFSET = {
    "America/New_York":    -4,   # EDT (Mar–Nov)
    "America/Chicago":     -5,   # CDT (Mar–Nov)
    "America/Los_Angeles": -7,   # PDT (Mar–Nov)
    "Asia/Tokyo":           9,
    "Asia/Singapore":       8,
    "Asia/Hong_Kong":       8,
    "Asia/Seoul":           9,
    "Asia/Shanghai":        8,
}

SYDNEY_UTC_OFFSET = 11   # AEDT (Oct–Apr) / change to 10 for AEST (Apr–Oct)

def check_market_window(target: date) -> bool:
    """
    Print when this market resolves in Sydney time and how many hours away that is.
    Returns True if within the 24-hour trading window, False (with a warning) if not.
    Markets are analysed only on the day they close — this check enforces that rule.
    """
    from datetime import timezone as _tz, timedelta as _td
    city_offset   = _TZ_UTC_OFFSET.get(TIMEZONE, 0)
    # Resolution ≈ midnight at end of target date in the city's local time
    resolution_utc = (
        datetime(target.year, target.month, target.day, tzinfo=_tz.utc)
        + _td(days=1)
        - _td(hours=city_offset)
    )
    now_utc       = datetime.now(_tz.utc)
    hours_until   = (resolution_utc - now_utc).total_seconds() / 3600
    res_sydney    = resolution_utc + _td(hours=SYDNEY_UTC_OFFSET)

    if hours_until < 0:
        label = f"⚠️  ALREADY RESOLVED ({abs(hours_until):.0f}h ago)"
        ok = False
    elif hours_until <= 24:
        label = f"✅  within 24h window"
        ok = True
    else:
        label = f"⛔  {hours_until:.0f}h away — OUTSIDE 24h window"
        ok = False

    print(f"  Market closes: {res_sydney.strftime('%a %b %-d %-I:%M %p')} Sydney time  "
          f"({hours_until:.0f}h from now)  {label}")
    if not ok and hours_until > 0:
        print(f"  !! Wait until this market is within 24h before analysing.")
        print(f"  !! In Sydney that means running the bot after: "
              f"{(res_sydney - _td(hours=24)).strftime('%a %b %-d %-I:%M %p')}")
    print()
    return ok


# ── Main ──────────────────────────────────────────────────────────────────────

def run(target: date, trade_size: int = DEFAULT_TRADE_SIZE, do_log: bool = False) -> None:
    W = 68
    print(f"\n{'═' * W}")
    print(f"  Weather Bot — {CITY} — {target.strftime('%A %B %-d, %Y')}")
    print(f"{'═' * W}\n")

    within_window = check_market_window(target)
    if not within_window:
        ans = input("  Continue anyway? (y/N): ").strip().lower()
        if ans != "y":
            print("  Skipping — run again when the market is within 24h.\n")
            return
        print()

    print("Fetching ensemble forecasts from Open-Meteo...")
    highs = fetch_ensemble(target)
    mean  = sum(highs) / len(highs)
    real  = mean - MODEL_WARM_BIAS
    print(f"  {len(highs)} members  |  mean = {mean:.1f}°F  |  range = {highs[0]:.0f}–{highs[-1]:.0f}°F")
    bias_dir = "warm" if MODEL_WARM_BIAS > 0 else "cool"
    bias_abs = abs(MODEL_WARM_BIAS)
    print(f"  REAL ≈ {real:.1f}°F  (bias-corrected: {MODEL_WARM_BIAS:+.1f}°F {bias_dir} bias [{CITY}])\n")

    print("Fetching secondary forecasts (NWS + wttr.in)...")
    cc = fetch_forecast_crosscheck(target)
    if cc["errors"]:
        for e in cc["errors"]:
            print(f"  [WARN] {e}")
    _print_crosscheck(real, cc)

    print("Fetching Polymarket prices...")
    poly_data = fetch_polymarket(target)

    if poly_data:
        market_prices, market_slugs = poly_data
        print(f"  Found {len(market_prices)} outcome brackets on Polymarket\n")
        brackets = parse_brackets(list(market_prices.keys()))
        if not brackets:
            print("  [!] Could not parse bracket labels — check API response")
            return
    else:
        market_prices = {}
        market_slugs  = {}
        print("  [!] Using fallback bracket structure (no live prices)\n")
        brackets = _fallback_brackets()

    model_probs_map = bracket_probs(highs, brackets)

    # ── Market favourite ──────────────────────────────────────────────────────
    if market_prices:
        fav_label = max(market_prices, key=lambda k: market_prices[k])
        fav_prob  = market_prices[fav_label] * 100
        fav_model = model_probs_map.get(fav_label, 0.0) * 100
        print(f"  Market favourite: {fav_label} @ {fav_prob:.0f}%  "
              f"(our model: {fav_model:.0f}%)  — see track record with `--favreport`\n")

    # ── Log the ensemble forecast (every evaluated day, trade or not) ─────────
    if do_log:
        log_forecast(target, CITY, mean, real, highs, cc, market_prices)

    # ── Print table ───────────────────────────────────────────────────────────
    print(f"{'─' * W}")
    print(f"  {'Bracket':<12} {'Market':>8} {'Model':>8} {'Edge':>9}  Signal")
    print(f"{'─' * W}")

    signals = []
    for label, lo, hi in brackets:
        model_p  = model_probs_map.get(label, 0.0) * 100
        market_p = (market_prices.get(label, 0.0) * 100) if market_prices else 0.0
        edge     = model_p - market_p
        abs_edge = abs(edge)

        direction = "YES" if edge > 0 else "NO"
        threshold = YES_EDGE_THRESHOLD if direction == "YES" else NO_EDGE_THRESHOLD

        # Distance from bias-corrected mean to nearest bracket edge (0 if real is inside bracket)
        if lo <= real <= hi:
            mean_dist = 0.0
        else:
            mean_dist = min(abs(real - lo), abs(real - hi))

        near_mean = (direction == "NO") and (mean_dist < MEAN_BUFFER)

        if abs_edge >= threshold:
            if direction == "YES":
                risk_tag = "  ⚠ needs exact bracket"
            elif near_mean:
                risk_tag = f"  ⚠ {mean_dist:.1f}°F from REAL mean — higher risk"
            else:
                risk_tag = ""
            signal_str = f"BUY {direction}  +{abs_edge:.1f}pp{risk_tag}"
            signals.append((label, edge, direction, mean_dist))
        elif abs_edge >= NO_EDGE_THRESHOLD and direction == "YES":
            signal_str = f"(YES {abs_edge:.1f}pp — below {YES_EDGE_THRESHOLD:.0f}pp YES threshold)"
        else:
            signal_str = "—"

        mkt_str = f"{market_p:.1f}%" if market_prices else "n/a"
        print(f"  {label:<12} {mkt_str:>8} {model_p:>7.1f}%  {edge:>+8.1f}pp  {signal_str}")

    print(f"{'─' * W}\n")
    print(f"  Ensemble mean: {mean:.1f}°F  →  REAL ≈ {real:.1f}°F  ·  NO brackets within {MEAN_BUFFER:.0f}°F of REAL mean flagged ⚠\n")

    if not signals:
        print(f"  No NO signals above {NO_EDGE_THRESHOLD}pp or YES signals above {YES_EDGE_THRESHOLD}pp.")
        print()
        return

    # Sort: clean NOs first (by edge), then risky NOs, then YES
    signals.sort(key=lambda x: (
        0 if (x[2] == "NO" and x[3] >= MEAN_BUFFER) else
        1 if (x[2] == "NO" and x[3] <  MEAN_BUFFER) else 2,
        -abs(x[1])
    ))

    print(f"  ACTIONABLE SIGNALS  (NO ≥ {NO_EDGE_THRESHOLD}pp · YES ≥ {YES_EDGE_THRESHOLD}pp):\n")
    for label, edge, direction, mean_dist in signals:
        slug = market_slugs.get(label)
        cmd  = (
            f"bullpen polymarket buy {slug} {direction} {trade_size} --yes"
            if slug else
            f"# no slug found for '{label}' — look up on Polymarket"
        )
        if direction == "YES":
            note = "  ← needs exact bracket to win"
        elif mean_dist < MEAN_BUFFER:
            note = f"  ← ⚠ only {mean_dist:.1f}°F from REAL mean ({real:.1f}°F), forecast error could beat you"
        else:
            note = f"  ← {mean_dist:.1f}°F buffer from REAL mean ({real:.1f}°F)"
        print(f"  [{'+' if edge > 0 else '-'}{abs(edge):.1f}pp]  BUY {direction}  {label}{note}")
        print(f"    $ {cmd}\n")

    # ── Interactive confirm ────────────────────────────────────────────────────
    print("  Enter a bracket label to execute that trade (or press Enter to skip all):")
    while True:
        try:
            answer = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Skipping all trades.")
            break

        if not answer:
            print("  No trades placed.")
            break

        match = next(((l, e, d, md) for l, e, d, md in signals if l.lower() == answer.lower()), None)
        if not match:
            print(f"  Unknown bracket '{answer}'. Valid: {[l for l, _, _, _ in signals]}")
            continue

        label, edge, direction, mean_dist = match
        slug = market_slugs.get(label)
        if not slug:
            print(f"  No market slug for '{label}' — cannot execute automatically.")
            continue

        execute_trade(slug, direction, trade_size)
        print(f"\n  Enter another bracket to trade, or press Enter to exit:")

    # ── Log signals ───────────────────────────────────────────────────────────
    if do_log and signals:
        print(f"\n  NWS forecast for {CITY} on {target} (°F, press Enter to skip): ", end="")
        try:
            nws_input = input().strip()
            nws_val = nws_input if nws_input else ""
        except (EOFError, KeyboardInterrupt):
            nws_val = ""
        n = log_signals(target, signals, brackets, market_prices, model_probs_map, trade_size,
                        ensemble_mean=mean, adj_mean=real, nws_forecast=nws_val)
        print(f"  Logged {n} signal(s) to {LOG_FILE.name}")
        fav = log_market_favorite(target, market_prices, brackets)
        if fav:
            print(f"  Logged market favourite ({fav[0]} @ {fav[1]*100:.0f}%) to {FAV_FILE.name}")


# ── Signal logger ─────────────────────────────────────────────────────────────

def log_signals(target: date, signals: list, brackets: list, market_prices: dict,
                model_probs_map: dict, trade_size: int,
                ensemble_mean: float = 0.0, adj_mean: float = 0.0,
                nws_forecast: str = "") -> int:
    """Append signals to signals.csv. Returns number of rows written."""
    bracket_bounds = {label: (lo, hi) for label, lo, hi in brackets}
    write_header = not LOG_FILE.exists()

    rows_written = 0
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        for label, edge, direction, *_ in signals:
            lo, hi = bracket_bounds.get(label, (math.nan, math.nan))
            writer.writerow({
                "date":          target.isoformat(),
                "bracket":       label,
                "bracket_lo":    "-inf" if lo == -math.inf else str(lo),
                "bracket_hi":    "inf"  if hi ==  math.inf else str(hi),
                "market_price":  f"{market_prices.get(label, 0):.4f}",
                "model_prob":    f"{model_probs_map.get(label, 0):.4f}",
                "edge":          f"{edge:.2f}",
                "direction":     direction,
                "trade_size":    trade_size,
                "ensemble_mean": f"{ensemble_mean:.1f}",
                "adj_mean":      f"{adj_mean:.1f}",
                "nws_forecast":  nws_forecast,
                "logged_at":     datetime.now().strftime("%Y-%m-%d %H:%M"),
                "actual_high":   "",
                "pnl":           "",
                "result":        "",
            })
            rows_written += 1
    return rows_written

# ── Market-favourite tracker ──────────────────────────────────────────────────

def _city_tag(label: str) -> str:
    """Extract a city tag like '(CHI)' from a bracket label; default 'NYC'."""
    import re
    m = re.search(r'\(([A-Z]{2,4})\)', label or "")
    return m.group(1) if m else "NYC"

def log_market_favorite(target: date, market_prices: dict, brackets: list) -> Optional[tuple]:
    """Record the single highest-priced bracket (the market favourite) for this
    date+city. actual_high / fav_hit are left blank — filled at resolution by
    favorite_report() via a join against signals.csv. Returns (label, prob)."""
    if not market_prices:
        return None
    bounds = {label: (lo, hi) for label, lo, hi in brackets}
    fav_label = max(market_prices, key=lambda k: market_prices[k])
    fav_prob  = market_prices[fav_label]
    lo, hi    = bounds.get(fav_label, (math.nan, math.nan))

    write_header = not FAV_FILE.exists()
    with open(FAV_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FAV_FIELDS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow({
            "date":        target.isoformat(),
            "city":        CITY,
            "fav_bracket": fav_label,
            "fav_lo":      "-inf" if lo == -math.inf else str(lo),
            "fav_hi":      "inf"  if hi ==  math.inf else str(hi),
            "fav_prob":    f"{fav_prob:.4f}",
            "actual_high": "",
            "fav_hit":     "",
        })
    return fav_label, fav_prob

def _signals_actuals() -> dict:
    """Map (date, city) → actual_high from signals.csv, tolerating the older
    13-column rows that predate the ensemble/adj/nws fields."""
    actuals: dict = {}
    if not LOG_FILE.exists():
        return actuals
    raw = list(csv.reader(open(LOG_FILE)))
    for cells in raw[1:]:
        n = len(cells)
        if   n == 16: d, label, actual = cells[0], cells[1], cells[13]
        elif n == 13: d, label, actual = cells[0], cells[1], cells[10]
        else:         continue
        try:
            actuals[(d, _city_tag(label))] = float(actual)
        except (ValueError, TypeError):
            continue
    return actuals

def favorite_report() -> None:
    """Print how often the market favourite resolved correctly, overall and per
    city. Pulls actuals from signals.csv so resolved rows update automatically."""
    if not FAV_FILE.exists():
        print("No favorites.csv yet — run `python3 bot.py <date> <city> --log` to start tracking.")
        return
    favs    = list(csv.DictReader(open(FAV_FILE)))
    actuals = _signals_actuals()

    print(f"\n  {'Date':<12}{'City':<6}{'Favourite':<22}{'Price':>6} {'Actual':>8}  Result")
    print(f"  {'-'*62}")
    by_city: dict = {}
    overall = [0, 0]  # hits, total
    for r in sorted(favs, key=lambda x: (x["date"], x["city"])):
        a = r.get("actual_high") or ""
        actual = None
        try:    actual = float(a)
        except (ValueError, TypeError):
            actual = actuals.get((r["date"], r["city"]))
        if actual is None:
            res = "pending"
            line_actual = "—"
        else:
            lo = -math.inf if r["fav_lo"] in ("-inf", "") else float(r["fav_lo"])
            hi =  math.inf if r["fav_hi"] in ("inf",  "") else float(r["fav_hi"])
            hit = lo <= actual <= hi
            res = "HIT ✓" if hit else "MISS ✗"
            line_actual = f"{actual:.0f}"
            overall[1] += 1; overall[0] += 1 if hit else 0
            c = by_city.setdefault(r["city"], [0, 0])
            c[1] += 1; c[0] += 1 if hit else 0
        prob = float(r["fav_prob"]) * 100
        import re as _re
        fav_clean = _re.sub(r'\s*\([A-Z]{2,4}\)', '', r["fav_bracket"])
        print(f"  {r['date']:<12}{r['city']:<6}{fav_clean:<22}{prob:>5.0f}% {line_actual:>8}  {res}")
    print(f"  {'-'*62}")
    if overall[1]:
        print(f"  Overall favourite hit rate: {overall[0]}/{overall[1]} = {overall[0]/overall[1]*100:.0f}%")
        for city, (h, t) in sorted(by_city.items()):
            flag = "  ← favourite misses more often than it hits" if t and h/t < 0.5 else ""
            print(f"    {city:<5} {h}/{t} = {h/t*100:.0f}%{flag}")
    print(f"\n  Read: a low hit rate means betting NO against the favourite is structurally sound —")
    print(f"  the crowd's single 2°F pick is wrong more often than right.\n")

# ── Ensemble forecast dataset (prediction → resolution) ───────────────────────

def fetch_highs(lat: float, lon: float, tz_name: str, target: date,
                unit: str = "fahrenheit") -> list:
    """Raw (no bias) ensemble daily highs (6am–11pm) for any date.
    Future dates use forecast_days; past dates use past_days — but a past-date
    refetch returns ≈analysis values, not the lead-time forecast we traded on."""
    days_back = (date.today() - target).days
    tz = tz_name.replace("/", "%2F")
    window = (f"past_days={min(days_back + 2, 92)}&forecast_days=2"
              if days_back > 0 else "forecast_days=7")
    url = (
        "https://ensemble-api.open-meteo.com/v1/ensemble"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=temperature_2m"
        "&models=ecmwf_ifs025,ecmwf_aifs025,ncep_gefs025,icon_seamless"
        f"&temperature_unit={unit}"
        f"&{window}"
        f"&timezone={tz}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "weather-bot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    hourly, ts = data["hourly"], target.isoformat()
    times = hourly["time"]
    idx = [i for i, t in enumerate(times)
           if t.startswith(ts) and 6 <= int(t[11:13]) <= 23]
    if not idx:
        return []
    highs = []
    for key, vals in hourly.items():
        if not key.startswith("temperature_2m"):
            continue
        temps = [vals[i] for i in idx if vals[i] is not None]
        if temps:
            highs.append(max(temps))
    return sorted(highs)


def _load_forecasts() -> dict:
    """(date, city) → row dict from forecasts.csv."""
    rows: dict = {}
    if FORECAST_FILE.exists():
        for r in csv.DictReader(open(FORECAST_FILE)):
            rows[(r["date"], r["city"])] = r
    return rows


def _save_forecasts(rows: dict) -> None:
    with open(FORECAST_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FORECAST_FIELDS, extrasaction="ignore")
        w.writeheader()
        for k in sorted(rows, key=lambda x: (x[0], x[1])):
            w.writerow(rows[k])


def log_forecast(target: date, city: str, ensemble_mean: float, adj_mean: float,
                 highs: list, cc: dict, market_prices: dict, force: bool = False) -> None:
    """Append/update this run's ensemble forecast. By default skips if a mean is
    already logged for this day; force=True overwrites the forecast fields (used
    by the daily --track sweep) while preserving any resolution already filled in."""
    rows = _load_forecasts()
    key  = (target.isoformat(), city)
    existing = rows.get(key, {})
    if existing.get("ensemble_mean") and not force:
        return
    fav_label, fav_prob = "", ""
    if market_prices:
        fav_label = max(market_prices, key=lambda k: market_prices[k])
        fav_prob  = f"{market_prices[fav_label]:.4f}"
    unit = "°C" if FORECAST_COORDS.get(city, (0, 0, 0, "fahrenheit"))[3] == "celsius" else "°F"
    rows[key] = {
        "date": target.isoformat(), "city": city, "unit": unit,
        "ensemble_mean": f"{ensemble_mean:.1f}", "adj_mean": f"{adj_mean:.1f}",
        "n_members": str(len(highs)),
        "range_lo": f"{highs[0]:.0f}" if highs else "",
        "range_hi": f"{highs[-1]:.0f}" if highs else "",
        "nws_high":  "" if cc.get("nws_high")  is None else str(cc["nws_high"]),
        "wttr_high": "" if cc.get("wttr_high") is None else str(cc["wttr_high"]),
        "market_fav": fav_label, "fav_prob": fav_prob,
        # Preserve a resolution that was already filled in by --backfill/--resolve.
        "actual_high": existing.get("actual_high", ""),
        "error":       existing.get("error", ""),
        "source": "recorded",
        "logged_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    _save_forecasts(rows)


def _signals_index() -> dict:
    """(date, city) → {actual, mean, adj, nws} pulled from signals.csv,
    tolerating both the 13-column (old) and 16-column (new) row widths."""
    idx: dict = {}
    if not LOG_FILE.exists():
        return idx
    for cells in list(csv.reader(open(LOG_FILE)))[1:]:
        n = len(cells)
        if   n == 16:
            d, label, mean, adj, nws, actual = (
                cells[0], cells[1], cells[9], cells[10], cells[11], cells[13])
        elif n == 13:
            d, label, mean, adj, nws, actual = (
                cells[0], cells[1], "", "", "", cells[10])
        else:
            continue
        e = idx.setdefault((d, _city_tag(label)),
                           {"actual": "", "mean": "", "adj": "", "nws": ""})
        try:
            float(actual); e["actual"] = actual
        except (ValueError, TypeError):
            pass
        if mean: e["mean"] = mean
        if adj:  e["adj"]  = adj
        if nws:  e["nws"]  = nws
    return idx


def _favorites_index() -> dict:
    """(date, city) → (fav_bracket, fav_prob) from favorites.csv."""
    idx: dict = {}
    if FAV_FILE.exists():
        for r in csv.DictReader(open(FAV_FILE)):
            idx[(r["date"], r["city"])] = (r.get("fav_bracket", ""),
                                           r.get("fav_prob", ""))
    return idx


def rebuild_forecasts(refetch: bool = True) -> None:
    """Rebuild forecasts.csv from signals.csv + favorites.csv. Uses the recorded
    trade-time ensemble mean where we have it; otherwise refetches via past_days
    (flagged 'reconstructed'). Fills actuals + error for every resolved day."""
    sig  = _signals_index()
    favs = _favorites_index()
    rows = _load_forecasts()

    print(f"\n  Rebuilding {FORECAST_FILE.name} from {len(sig)} city-days...\n")
    for (d, city), info in sorted(sig.items()):
        tgt      = date.fromisoformat(d)
        coords   = FORECAST_COORDS.get(city)
        unit_sym = "°C" if (coords and coords[3] == "celsius") else "°F"
        bias     = CITY_BIAS.get(city, 0.0)
        existing = rows.get((d, city), {})

        mean, adj, source = info["mean"], info["adj"], "recorded"
        n_members = existing.get("n_members", "")
        rlo, rhi  = existing.get("range_lo", ""), existing.get("range_hi", "")

        if not mean and refetch and coords:
            lat, lon, tz, unit = coords
            try:
                highs = fetch_highs(lat, lon, tz, tgt, unit)
            except Exception as ex:
                highs = []
                print(f"    [WARN] refetch failed {d} {city}: {ex}")
            if highs:
                rawm      = sum(highs) / len(highs)
                mean      = f"{rawm:.1f}"
                adj       = f"{rawm - bias:.1f}"
                n_members = str(len(highs))
                rlo, rhi  = f"{highs[0]:.0f}", f"{highs[-1]:.0f}"
                source    = "reconstructed"
                print(f"    {d} {city}: reconstructed mean {mean}{unit_sym}")
        if mean and not adj:
            adj = f"{float(mean) - bias:.1f}"

        actual, error = info["actual"], ""
        if actual and adj:
            try:
                error = f"{float(actual) - float(adj):+.1f}"
            except (ValueError, TypeError):
                pass

        fav, favp = favs.get((d, city),
                             (existing.get("market_fav", ""), existing.get("fav_prob", "")))
        rows[(d, city)] = {
            "date": d, "city": city, "unit": unit_sym,
            "ensemble_mean": mean, "adj_mean": adj,
            "n_members": n_members, "range_lo": rlo, "range_hi": rhi,
            "nws_high":  info["nws"] or existing.get("nws_high", ""),
            "wttr_high": existing.get("wttr_high", ""),
            "market_fav": fav, "fav_prob": favp,
            "actual_high": actual, "error": error, "source": source,
            "logged_at": existing.get("logged_at") or datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

    _save_forecasts(rows)
    print(f"\n  Wrote {len(rows)} rows → {FORECAST_FILE.name}\n")
    forecast_report()


def forecast_report() -> None:
    """Print the ensemble prediction-vs-resolution record and the per-city mean
    forecast error. The bias suggestion is computed from RECORDED rows only —
    reconstructed rows have near-zero error by construction and would mask it."""
    if not FORECAST_FILE.exists():
        print("No forecasts.csv yet — run `python3 bot.py --backfill`.")
        return
    rows = list(csv.DictReader(open(FORECAST_FILE)))

    print(f"\n  {'Date':<12}{'City':<6}{'Mean':>7}{'Adj':>7}{'Actual':>8}{'Err':>7}  Source")
    print(f"  {'-'*60}")
    rec_err: dict = {}   # recorded-only errors, per city → [floats]
    all_err: dict = {}   # every resolved row, per city
    for r in sorted(rows, key=lambda x: (x["city"], x["date"])):
        err = r.get("error", "")
        mean   = f"{r['ensemble_mean']}{r['unit']}" if r["ensemble_mean"] else "—"
        adj    = f"{r['adj_mean']}{r['unit']}"      if r["adj_mean"]      else "—"
        actual = f"{r['actual_high']}{r['unit']}"   if r["actual_high"]   else "—"
        tag    = (r.get("source") or "")[:13]
        print(f"  {r['date']:<12}{r['city']:<6}{mean:>7}{adj:>7}{actual:>8}{err:>7}  {tag}")
        if err:
            try:
                e = float(err)
                all_err.setdefault(r["city"], []).append(e)
                if r.get("source") == "recorded":
                    rec_err.setdefault(r["city"], []).append(e)
            except ValueError:
                pass
    print(f"  {'-'*60}")

    print(f"\n  Per-city forecast error  (actual − adj_mean; + = ensemble ran COOL):")
    for city in sorted(all_err):
        ae = all_err[city]
        re = rec_err.get(city, [])
        amean = sum(ae) / len(ae)
        line = f"    {city:<5} all: {amean:+.1f} (n={len(ae)})"
        if re:
            rmean = sum(re) / len(re)
            suggest = CITY_BIAS.get(city, 0.0) - rmean
            line += (f"   recorded: {rmean:+.1f} (n={len(re)})"
                     f"  → suggested CITY_BIAS {suggest:+.1f}"
                     f" (now {CITY_BIAS.get(city, 0.0):+.1f})")
        print(line)
    print(f"\n  Note: 'reconstructed' rows are refetched after the fact and ≈ the analysis,")
    print(f"  so their error understates real lead-time bias. Trust 'recorded' rows for")
    print(f"  calibration; reconstructed rows anchor actuals + market favourites only.\n")

# ── Daily tracking sweep ──────────────────────────────────────────────────────

# Cities swept by --track. US markets are 2°F (°F); the Asian markets (TYO/SIN)
# are single-degree °C — both handled via the unit-aware fetch_highs pipeline.
TRACK_CITIES = ["NYC", "MIA", "CHI", "LAX", "TYO", "SIN"]

# Polymarket city token per city (the {city} slot in the event slug).
POLY_SLUG = {
    "NYC": "nyc",   "MIA": "miami",     "CHI": "chicago", "LAX": "los-angeles",
    "TYO": "tokyo", "SIN": "singapore",
}

def track_all(target: date) -> None:
    """Non-interactive sweep: log the current ensemble forecast for every tracked
    city to forecasts.csv, in each market's native unit (°F US / °C Asia). No
    trades, no prompts — safe to schedule. Resolutions filled later by --resolve."""
    global LATITUDE, LONGITUDE
    print(f"\n  Tracking {len(TRACK_CITIES)} markets for {target.strftime('%A %b %-d, %Y')}...\n")
    logged = 0
    for city in TRACK_CITIES:
        lat, lon, tz, unit = FORECAST_COORDS[city]
        bias = CITY_BIAS.get(city, 0.0)
        try:
            highs = fetch_highs(lat, lon, tz, target, unit)
        except Exception as ex:
            print(f"    {city:<4} ensemble fetch failed: {ex}")
            continue
        if not highs:
            print(f"    {city:<4} no ensemble data for {target}")
            continue
        mean = sum(highs) / len(highs)
        real = mean - bias
        poly = fetch_polymarket(target, city_slug=POLY_SLUG.get(city))
        market_prices = poly[0] if poly else {}
        # NWS + wttr cross-check is US-only (NWS has no Asian stations).
        if unit == "fahrenheit":
            LATITUDE, LONGITUDE = lat, lon
            cc = fetch_forecast_crosscheck(target)
        else:
            cc = {"nws_high": None, "wttr_high": None}
        log_forecast(target, city, mean, real, highs, cc, market_prices, force=True)
        logged += 1
        u   = "°C" if unit == "celsius" else "°F"
        fav = (max(market_prices, key=lambda k: market_prices[k])
               if market_prices else "no live market")
        print(f"    {city:<4} mean {mean:.1f}{u} → real {real:.1f}{u}   fav: {fav}")
    print(f"\n  Logged {logged} city-day(s) → {FORECAST_FILE.name}\n")

# ── Resolution (auto-fill actuals) ────────────────────────────────────────────

# NWS station each US market resolves against (observed daily high). The Asian
# °C markets have no NWS station — they stay manual until a source is wired in.
RESOLVE_STATION = {
    "NYC": "KLGA",   # LaGuardia
    "MIA": "KMIA",   # Miami Intl
    "CHI": "KORD",   # O'Hare
    "LAX": "KLAX",   # LA Intl
}

def fetch_observed_high(station: str, target: date, tz_name: str) -> Optional[float]:
    """Observed daily high (°F) for a US station over the target's LOCAL calendar
    day, from NWS station observations (METAR). Returns None if no obs found.

    IMPORTANT: We filter to hourly METAR observations only (timestamp minute 45–59).
    Wunderground (Polymarket's resolution source) uses hourly METARs, NOT the
    5-minute ASOS auto-reports. The 5-min reports round Celsius to whole degrees,
    which can produce readings 1°F higher than the precise hourly METAR T-group
    value. Using all observations caused a systematic +1°F overcount vs Wunderground.
    """
    from datetime import timezone as _tz, timedelta as _td
    offset    = _TZ_UTC_OFFSET.get(tz_name, 0)
    start_utc = datetime(target.year, target.month, target.day, tzinfo=_tz.utc) - _td(hours=offset)
    end_utc   = start_utc + _td(hours=24)
    url = (
        f"https://api.weather.gov/stations/{station}/observations"
        f"?start={start_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&end={end_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "weather-bot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())

    # Only use the standard hourly METAR observations (issued at :53 past the hour).
    # Wunderground (Polymarket's resolution source) records hourly METARs only —
    # NOT the 5-minute ASOS auto-reports that run at :00/:05/…/:50/:55.
    #
    # Why this matters: 5-min ASOS reports use rounded whole-degree Celsius from
    # the standard METAR temperature field. The :53 METAR uses the T-group (tenths
    # of °C), which is more precise and converts to a slightly different °F value.
    # Example: ASOS reports 22.0°C → 71.6°F → rounds to 72°F.
    #          METAR T-group: 21.7°C → 71.1°F → rounds to 71°F.
    # Using all observations inflated the daily max by ~1°F vs Wunderground.
    temps_c = []
    all_temps_c = []  # kept as fallback
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        temp  = props.get("temperature", {}).get("value")
        ts    = props.get("timestamp", "")   # e.g. "2026-06-04T20:53:00+00:00"
        if temp is None:
            continue
        all_temps_c.append(temp)
        try:
            minute = int(ts[14:16])
        except (ValueError, IndexError):
            continue
        if minute == 53:   # standard hourly METAR only
            temps_c.append(temp)

    # Fallback: if no :53 METARs found (unusual), use all observations
    if not temps_c:
        temps_c = all_temps_c
    if not temps_c:
        return None
    return float(round(max(temps_c) * 9 / 5 + 32))


def fetch_observed_high_intl(lat: float, lon: float, tz_name: str, target: date,
                             unit: str = "celsius") -> Optional[float]:
    """Observed daily high for a non-US location via Open-Meteo's daily max
    (gridded, ~station proxy). Used for Tokyo/Singapore — no NWS station exists.
    Less exact than a station METAR; flagged 'open-meteo' so it's distinguishable."""
    days_back = (date.today() - target).days
    tz = tz_name.replace("/", "%2F")
    window = (f"past_days={min(days_back + 2, 92)}&forecast_days=1"
              if days_back > 0 else "forecast_days=1")
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&daily=temperature_2m_max"
        f"&temperature_unit={unit}"
        f"&{window}"
        f"&timezone={tz}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "weather-bot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())
    daily = data.get("daily", {})
    ts    = target.isoformat()
    for t, v in zip(daily.get("time", []), daily.get("temperature_2m_max", [])):
        if t == ts and v is not None:
            return float(round(v))
    return None


def resolve_forecasts() -> None:
    """Fill actual_high + error for every settled (past-dated) forecasts.csv row
    that's still missing a resolution, using NWS station observations."""
    rows = _load_forecasts()
    pending = [
        (k, r) for k, r in rows.items()
        if not r.get("actual_high") and date.fromisoformat(r["date"]) < date.today()
    ]
    if not pending:
        print("\n  No pending resolutions — every settled day already has an actual.\n")
        return

    print(f"\n  Resolving {len(pending)} settled city-day(s)...\n")
    filled, skipped = 0, 0
    for (d, city), r in sorted(pending):
        coords = FORECAST_COORDS.get(city)
        if not coords:
            print(f"    {d} {city}: unknown city — fill manually")
            skipped += 1
            continue
        lat, lon, tz, unit = coords
        u = "°C" if unit == "celsius" else "°F"
        station = RESOLVE_STATION.get(city)

        # International markets have no NWS station, and the Open-Meteo grid
        # disagrees with the JMA/station source Polymarket settles on (~2°C off on
        # Tokyo — enough to flip a single-degree market). So we only HINT and leave
        # the value for manual --set-actual; never auto-write an unreliable actual.
        if not station:
            try:
                est = fetch_observed_high_intl(lat, lon, tz, date.fromisoformat(d), unit)
            except Exception:
                est = None
            hint = f"  (Open-Meteo est ~{est:.0f}{u}, verify on Polymarket)" if est is not None else ""
            print(f"    {d} {city}: manual →  python3 bot.py --set-actual {d} {city} <value>{hint}")
            skipped += 1
            continue

        try:
            hi = fetch_observed_high(station, date.fromisoformat(d), tz)
        except Exception as ex:
            print(f"    {d} {city}: obs fetch failed: {ex}")
            skipped += 1
            continue
        if hi is None:
            print(f"    {d} {city}: no observations returned ({station})")
            skipped += 1
            continue
        r["actual_high"] = f"{hi:.1f}"
        adj = r.get("adj_mean", "")
        if adj:
            try:
                r["error"] = f"{hi - float(adj):+.1f}"
            except (ValueError, TypeError):
                pass
        filled += 1
        print(f"    {d} {city}: actual {hi:.0f}{u}   err {r.get('error', '—') or '—'}   ({station})")

    _save_forecasts(rows)
    print(f"\n  Filled {filled} resolution(s)"
          f"{f', {skipped} manual/skipped' if skipped else ''} → {FORECAST_FILE.name}\n")


def set_actual(target_str: str, city: str, value: str) -> None:
    """Manually set a resolution on a forecasts.csv row (for Tokyo/Singapore,
    which can't be auto-resolved). Recomputes error from adj_mean."""
    city = city.upper()
    rows = _load_forecasts()
    key  = (target_str, city)
    if key not in rows:
        print(f"  No forecasts.csv row for {target_str} {city} — run --track for that day first.")
        return
    try:
        v = float(value)
    except ValueError:
        print(f"  '{value}' is not a number.")
        return
    r = rows[key]
    r["actual_high"] = f"{v:.1f}"
    adj = r.get("adj_mean", "")
    if adj:
        try:
            r["error"] = f"{v - float(adj):+.1f}"
        except (ValueError, TypeError):
            pass
    _save_forecasts(rows)
    print(f"  Set {city} {target_str} actual = {v:.0f}{r['unit']}   "
          f"err {r.get('error', '—') or '—'} → {FORECAST_FILE.name}")

# ── HTML dashboard ────────────────────────────────────────────────────────────

def generate_html(
    target: date,
    highs: list,
    brackets: list,
    model_probs: dict,
    market_prices: dict,
    out_path: str = "dashboard.html",
) -> str:
    mean_high = sum(highs) / len(highs)
    real_high = mean_high - MODEL_WARM_BIAS

    # Histogram: count members per 1°F bin
    lo_bin = int(math.floor(min(highs)))
    hi_bin = int(math.ceil(max(highs)))
    bins = list(range(lo_bin, hi_bin + 1))
    counts = {b: 0 for b in bins}
    for h in highs:
        counts[int(round(h))] = counts.get(int(round(h)), 0) + 1
    max_count = max(counts.values()) or 1

    # Build histogram bars (inline SVG-free, pure CSS)
    hist_rows = ""
    for b in bins:
        n   = counts.get(b, 0)
        pct = n / max_count * 100
        # Hue shifts from blue (215) at lowest count to blue-green (185) at highest
        frac = n / max_count
        hue  = 215 - frac * (215 - 185)
        sat  = 55 + frac * 10
        lgt  = 48 + frac * 8
        color = f"hsl({hue:.0f},{sat:.0f}%,{lgt:.0f}%)"
        hist_rows += (
            f'<div class="bar-row">'
            f'<span class="bar-label">{b}°</span>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{pct:.1f}%;background:{color}"></div></div>'
            f'<span class="bar-count">{n}</span>'
            f'</div>\n'
        )

    # Build bracket table rows
    table_rows = ""
    for label, lo, hi in brackets:
        model_p  = model_probs.get(label, 0.0) * 100
        market_p = (market_prices.get(label, 0.0) * 100) if market_prices else None
        edge     = (model_p - market_p) if market_p is not None else None

        mkt_str  = f"{market_p:.1f}%" if market_p is not None else "—"
        edge_str = f"{edge:+.1f}pp"   if edge is not None else "—"
        table_rows += (
            f'<tr>'
            f'<td class="bracket-cell">{label}</td>'
            f'<td class="num-cell">{mkt_str}</td>'
            f'<td class="num-cell">{model_p:.1f}%</td>'
            f'<td class="num-cell edge-cell">{edge_str}</td>'
            f'</tr>\n'
        )

    generated_at = datetime.now().strftime("%b %-d, %Y  %I:%M %p")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Weather Bot — {CITY} {target.strftime('%b %-d, %Y')}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  :root {{
    --bg:       #0d0f14;
    --surface:  #161a22;
    --border:   #252933;
    --muted:    #4a5165;
    --text:     #e2e6f0;
    --subtext:  #8b92a8;
    --accent:   #5b8cf5;
    --mono:     'SF Mono', 'Fira Code', 'Consolas', monospace;
    --sans:     'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  }}

  body {{
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    min-height: 100vh;
    padding: 48px 32px;
  }}

  .page {{
    max-width: 900px;
    margin: 0 auto;
  }}

  /* ── Header ── */
  .header {{ margin-bottom: 36px; }}
  .header-top {{
    display: flex;
    align-items: baseline;
    gap: 16px;
    margin-bottom: 6px;
  }}
  .city-title {{
    font-size: 32px;
    font-weight: 700;
    letter-spacing: -0.5px;
    color: var(--text);
  }}
  .date-label {{
    font-size: 18px;
    color: var(--subtext);
    font-weight: 400;
  }}
  .meta {{
    font-size: 13px;
    color: var(--muted);
    font-family: var(--mono);
  }}

  /* ── Stat chips ── */
  .stats {{
    display: flex;
    gap: 12px;
    margin-bottom: 40px;
    flex-wrap: wrap;
  }}
  .stat-chip {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 20px;
  }}
  .stat-label {{
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    margin-bottom: 4px;
  }}
  .stat-value {{
    font-size: 22px;
    font-weight: 600;
    font-family: var(--mono);
    color: var(--text);
  }}

  /* ── Section titles ── */
  .section-title {{
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--muted);
    margin-bottom: 14px;
  }}

  /* ── Table ── */
  .table-wrap {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
    margin-bottom: 40px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-family: var(--mono);
    font-size: 14px;
  }}
  thead th {{
    padding: 12px 20px;
    text-align: right;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    font-weight: 500;
    border-bottom: 1px solid var(--border);
    background: var(--bg);
  }}
  thead th:first-child {{ text-align: left; }}
  tbody tr {{ border-bottom: 1px solid var(--border); }}
  tbody tr:last-child {{ border-bottom: none; }}
  tbody tr:hover {{ background: rgba(255,255,255,0.02); }}
  td {{ padding: 13px 20px; }}
  .bracket-cell {{ color: var(--text); font-weight: 500; text-align: left; }}
  .num-cell {{ text-align: right; color: var(--subtext); }}
  .edge-cell {{ color: var(--text); }}

  /* ── Histogram ── */
  .histogram {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 20px 24px;
    margin-bottom: 40px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
  }}
  .bar-row {{
    display: flex;
    align-items: center;
    gap: 10px;
  }}
  .bar-label {{
    font-family: var(--mono);
    font-size: 12px;
    color: var(--subtext);
    width: 36px;
    text-align: right;
    flex-shrink: 0;
  }}
  .bar-track {{
    flex: 1;
    height: 10px;
    background: var(--border);
    border-radius: 3px;
    overflow: hidden;
  }}
  .bar-fill {{
    height: 100%;
    border-radius: 3px;
  }}
  .bar-count {{
    font-family: var(--mono);
    font-size: 11px;
    color: var(--muted);
    width: 28px;
    text-align: right;
    flex-shrink: 0;
  }}

  /* ── Two-column layout ── */
  .two-col {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 24px;
    align-items: stretch;
    margin-bottom: 40px;
  }}
  .two-col > div {{
    display: flex;
    flex-direction: column;
  }}
  .two-col .histogram,
  .two-col .table-wrap {{
    flex: 1;
  }}

  /* ── Footer ── */
  .footer {{
    font-size: 12px;
    color: var(--muted);
    font-family: var(--mono);
    border-top: 1px solid var(--border);
    padding-top: 16px;
  }}
</style>
</head>
<body>
<div class="page">

  <div class="header">
    <div class="header-top">
      <span class="city-title">{CITY}</span>
      <span class="date-label">{target.strftime('%A, %B %-d %Y')}</span>
    </div>
    <div class="meta">173 ensemble members · ECMWF IFS · ECMWF AIFS · NCEP GEFS · ICON</div>
  </div>

  <div class="stats">
    <div class="stat-chip">
      <div class="stat-label">Ensemble Mean</div>
      <div class="stat-value">{mean_high:.1f}°F</div>
    </div>
    <div class="stat-chip" style="border-color:#5b8cf5;">
      <div class="stat-label" style="color:#5b8cf5;">Real (Bias-Corrected)</div>
      <div class="stat-value" style="color:#5b8cf5;">{real_high:.1f}°F</div>
    </div>
    <div class="stat-chip">
      <div class="stat-label">Members</div>
      <div class="stat-value">{len(highs)}</div>
    </div>
    <div class="stat-chip">
      <div class="stat-label">Range</div>
      <div class="stat-value">{highs[0]:.0f}–{highs[-1]:.0f}°F</div>
    </div>
    <div class="stat-chip">
      <div class="stat-label">Resolution Source</div>
      <div class="stat-value" style="font-size:15px;padding-top:3px">LaGuardia / NWS</div>
    </div>
  </div>

  <div class="two-col">
    <div>
      <div class="section-title">Ensemble Distribution — Daily High (°F)</div>
      <div class="histogram">
{hist_rows}      </div>
    </div>

    <div>
      <div class="section-title">Market vs Model Probability</div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Bracket</th>
              <th>Polymarket</th>
              <th>Model</th>
              <th>Edge</th>
            </tr>
          </thead>
          <tbody>
{table_rows}          </tbody>
        </table>
      </div>
    </div>
  </div>

  <div class="footer">
    Generated {generated_at} · Open-Meteo ensemble API · Polymarket Gamma API
  </div>

</div>
</body>
</html>"""

    with open(out_path, "w") as f:
        f.write(html)
    return out_path


# ── Public dashboard JSON export ──────────────────────────────────────────────

def export_site_json(out_path: str = "site_data.json") -> str:
    """Build a structured JSON snapshot consumed by the public dashboard (index.html)."""
    import re as _re

    out = Path(__file__).parent / out_path

    def _f(v):
        try:
            return float(v) if (v is not None and str(v).strip() not in ("", "CUT")) else None
        except (ValueError, TypeError):
            return None

    # ── Forecasts ─────────────────────────────────────────────────────────────
    forecasts = []
    if FORECAST_FILE.exists():
        for r in csv.DictReader(open(FORECAST_FILE)):
            forecasts.append({
                "date":          r["date"],
                "city":          r["city"],
                "unit":          r["unit"],
                "ensemble_mean": _f(r.get("ensemble_mean")),
                "adj_mean":      _f(r.get("adj_mean")),
                "n_members":     _f(r.get("n_members")),
                "range_lo":      _f(r.get("range_lo")),
                "range_hi":      _f(r.get("range_hi")),
                "nws_high":      _f(r.get("nws_high")),
                "wttr_high":     _f(r.get("wttr_high")),
                "market_fav":    r.get("market_fav") or None,
                "fav_prob":      _f(r.get("fav_prob")),
                "actual_high":   _f(r.get("actual_high")),
                "error":         _f(r.get("error")),
                "source":        r.get("source") or "recorded",
                "logged_at":     r.get("logged_at") or "",
            })

    # ── Signals ───────────────────────────────────────────────────────────────
    def _bound(s):
        if s in ("-inf", "inf", "", None):
            return None
        try:
            return float(s)
        except (ValueError, TypeError):
            return None

    signals = []
    if LOG_FILE.exists():
        for cells in list(csv.reader(open(LOG_FILE)))[1:]:
            n = len(cells)
            if n == 16:
                sig = {
                    "date": cells[0], "bracket": cells[1],
                    "bracket_lo": _bound(cells[2]), "bracket_hi": _bound(cells[3]),
                    "market_price": _f(cells[4]), "model_prob": _f(cells[5]),
                    "edge": _f(cells[6]), "direction": cells[7],
                    "trade_size": _f(cells[8]),
                    "ensemble_mean": _f(cells[9]), "adj_mean": _f(cells[10]),
                    "nws_forecast": _f(cells[11]),
                    "logged_at": cells[12],
                    "actual_high": _f(cells[13]), "pnl": _f(cells[14]),
                    "result": cells[15] or None,
                }
            elif n == 13:
                sig = {
                    "date": cells[0], "bracket": cells[1],
                    "bracket_lo": _bound(cells[2]), "bracket_hi": _bound(cells[3]),
                    "market_price": _f(cells[4]), "model_prob": _f(cells[5]),
                    "edge": _f(cells[6]), "direction": cells[7],
                    "trade_size": _f(cells[8]),
                    "ensemble_mean": None, "adj_mean": None, "nws_forecast": None,
                    "logged_at": cells[9],
                    "actual_high": _f(cells[10]), "pnl": _f(cells[11]),
                    "result": cells[12] or None,
                }
            else:
                continue
            m = _re.search(r'\(([A-Z]{2,4})\)', sig["bracket"])
            sig["city"] = m.group(1) if m else "NYC"
            signals.append(sig)

    # ── Favorites ─────────────────────────────────────────────────────────────
    favorites = []
    if FAV_FILE.exists():
        actuals = _signals_actuals()
        for r in csv.DictReader(open(FAV_FILE)):
            actual_f = _f(r.get("actual_high")) or actuals.get((r["date"], r["city"]))
            fav_hit_raw = r.get("fav_hit") or ""
            fav_hit = None
            if fav_hit_raw in ("1", "true", "True"):
                fav_hit = True
            elif fav_hit_raw in ("0", "false", "False"):
                fav_hit = False
            elif actual_f is not None:
                lo_s, hi_s = r.get("fav_lo", ""), r.get("fav_hi", "")
                lo_f = -math.inf if lo_s == "-inf" else _f(lo_s)
                hi_f =  math.inf if hi_s ==  "inf" else _f(hi_s)
                if lo_f is not None and hi_f is not None:
                    fav_hit = bool(lo_f <= actual_f <= hi_f)
            favorites.append({
                "date": r["date"], "city": r["city"],
                "fav_bracket": r.get("fav_bracket") or None,
                "fav_lo": None if r.get("fav_lo") == "-inf" else _f(r.get("fav_lo")),
                "fav_hi": None if r.get("fav_hi") ==  "inf" else _f(r.get("fav_hi")),
                "fav_prob": _f(r.get("fav_prob")),
                "actual_high": actual_f,
                "fav_hit": fav_hit,
            })

    # ── Summary stats ─────────────────────────────────────────────────────────
    resolved = [s for s in signals if s.get("result") in ("WIN", "LOSS", "CUT")]
    wins   = sum(1 for s in resolved if s["result"] == "WIN")
    losses = sum(1 for s in resolved if s["result"] == "LOSS")
    cuts   = sum(1 for s in resolved if s["result"] == "CUT")
    total_pnl = sum(s["pnl"] for s in resolved if s.get("pnl") is not None)

    by_city: dict = {}
    for s in resolved:
        c = s["city"]
        d = by_city.setdefault(c, {"wins": 0, "losses": 0, "cuts": 0, "pnl": 0.0})
        if s["result"] == "WIN":    d["wins"]   += 1
        elif s["result"] == "LOSS": d["losses"] += 1
        else:                       d["cuts"]   += 1
        if s.get("pnl") is not None:
            d["pnl"] += s["pnl"]
    for c, d in by_city.items():
        t = d["wins"] + d["losses"] + d["cuts"]
        d["total"]    = t
        d["win_rate"] = round(d["wins"] / t, 3) if t else 0.0
        d["pnl"]      = round(d["pnl"], 2)

    summary = {
        "total_trades": len(resolved),
        "wins": wins, "losses": losses, "cuts": cuts,
        "win_rate": round(wins / len(resolved), 3) if resolved else 0.0,
        "total_pnl": round(total_pnl, 2),
        "by_city": by_city,
    }

    # Cumulative P&L series for the chart
    cumulative = []
    running = 0.0
    for s in sorted(resolved, key=lambda x: (x["date"], x["logged_at"])):
        if s.get("pnl") is not None:
            running += s["pnl"]
            cumulative.append({"date": s["date"], "city": s["city"],
                                "bracket": s["bracket"], "result": s["result"],
                                "pnl": round(s["pnl"], 2), "cum_pnl": round(running, 2)})
    summary["cumulative_pnl"] = cumulative

    # ── Calibration ───────────────────────────────────────────────────────────
    cal_by_city: dict = {}
    for f in forecasts:
        c = f["city"]
        if f.get("actual_high") is None or f.get("adj_mean") is None:
            continue
        err = round(f["actual_high"] - f["adj_mean"], 2)
        row = {"date": f["date"], "adj_mean": f["adj_mean"],
               "actual_high": f["actual_high"], "error": err,
               "source": f.get("source", "recorded")}
        cal_by_city.setdefault(c, {"rows": [], "current_bias": CITY_BIAS.get(c, 0.0)})
        cal_by_city[c]["rows"].append(row)
    for c, d in cal_by_city.items():
        rec = [r for r in d["rows"] if r["source"] == "recorded"]
        d["n_recorded"]          = len(rec)
        d["mean_error_recorded"] = round(sum(r["error"] for r in rec) / len(rec), 2) if rec else None
        d["n_total"]             = len(d["rows"])
        d["mean_error_all"]      = round(sum(r["error"] for r in d["rows"]) / len(d["rows"]), 2) if d["rows"] else None

    # ── Favourite stats ───────────────────────────────────────────────────────
    settled = [f for f in favorites if f.get("fav_hit") is not None]
    fav_hits = sum(1 for f in settled if f["fav_hit"])
    fav_by_city: dict = {}
    for f in settled:
        c = f["city"]
        d = fav_by_city.setdefault(c, {"hits": 0, "total": 0})
        d["total"] += 1
        if f["fav_hit"]:
            d["hits"] += 1

    fav_stats = {
        "overall_hits": fav_hits,
        "overall_total": len(settled),
        "overall_hit_rate": round(fav_hits / len(settled), 3) if settled else 0.0,
        "by_city": fav_by_city,
    }

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "forecasts": forecasts,
        "signals": signals,
        "favorites": favorites,
        "summary": summary,
        "calibration": {"by_city": cal_by_city},
        "fav_stats": fav_stats,
    }

    with open(out, "w") as fh:
        json.dump(payload, fh, indent=2)

    return str(out)


# ── History dashboard ─────────────────────────────────────────────────────────

def generate_history_html(out_path: str = "history.html") -> str:
    """Read signals.csv and generate a track-record dashboard."""
    if not LOG_FILE.exists():
        raise FileNotFoundError("signals.csv not found — run bot.py --log first.")

    with open(LOG_FILE, newline="") as f:
        rows = list(csv.DictReader(f))

    verified  = [r for r in rows if r.get("result")]
    pending   = [r for r in rows if not r.get("result")]
    n_total   = len(verified)
    n_wins    = sum(1 for r in verified if r["result"] == "WIN")
    total_pnl = sum(float(r["pnl"]) for r in verified)
    win_rate  = (n_wins / n_total * 100) if n_total else 0

    # Cumulative P&L for sparkline
    cumulative = []
    running = 0.0
    for r in sorted(verified, key=lambda x: (x["date"], x["logged_at"])):
        running += float(r["pnl"])
        cumulative.append(running)

    # Build sparkline SVG
    spark_svg = ""
    if len(cumulative) >= 2:
        w, h = 260, 60
        pad = 8
        mn, mx = min(0, min(cumulative)), max(cumulative)
        rng = mx - mn or 1
        pts = []
        for i, v in enumerate(cumulative):
            x = pad + (i / (len(cumulative) - 1)) * (w - pad * 2)
            y = h - pad - ((v - mn) / rng) * (h - pad * 2)
            pts.append(f"{x:.1f},{y:.1f}")
        zero_y = h - pad - ((0 - mn) / rng) * (h - pad * 2)
        spark_svg = (
            f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">'
            f'<line x1="{pad}" y1="{zero_y:.1f}" x2="{w-pad}" y2="{zero_y:.1f}" '
            f'stroke="#252933" stroke-width="1"/>'
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="#5b8cf5" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{pts[-1].split(",")[0]}" cy="{pts[-1].split(",")[1]}" r="3" fill="#5b8cf5"/>'
            f'</svg>'
        )

    # Build signal rows
    signal_rows = ""
    for r in sorted(verified, key=lambda x: (x["date"], x["logged_at"])):
        pnl       = float(r["pnl"])
        result    = r["result"]
        actual    = f"{float(r['actual_high']):.0f}°F" if r.get("actual_high") else "—"
        pnl_str   = f"${pnl:+.2f}"
        mkt_str   = f"{float(r['market_price'])*100:.1f}%"
        mod_str   = f"{float(r['model_prob'])*100:.1f}%"
        edge_str  = f"{float(r['edge']):+.1f}pp"
        result_cls = "win" if result == "WIN" else "loss"
        signal_rows += (
            f'<tr>'
            f'<td class="date-cell">{r["date"]}</td>'
            f'<td class="bracket-cell">{r["bracket"]}</td>'
            f'<td class="num-cell">{mkt_str}</td>'
            f'<td class="num-cell">{mod_str}</td>'
            f'<td class="num-cell">{edge_str}</td>'
            f'<td class="num-cell">{r["direction"]}</td>'
            f'<td class="num-cell">{actual}</td>'
            f'<td class="num-cell"><span class="badge {result_cls}">{result}</span></td>'
            f'<td class="num-cell pnl-cell">{pnl_str}</td>'
            f'</tr>\n'
        )

    pending_note = ""
    if pending:
        pending_note = f'<p class="pending-note">{len(pending)} signal(s) pending resolution.</p>'

    generated_at = datetime.now().strftime("%b %-d, %Y  %I:%M %p")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Weather Bot — Track Record</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  :root {{
    --bg:      #0d0f14;
    --surface: #161a22;
    --border:  #252933;
    --muted:   #4a5165;
    --text:    #e2e6f0;
    --subtext: #8b92a8;
    --accent:  #5b8cf5;
    --win:     #3d9e6e;
    --loss:    #c0392b;
    --mono:    'SF Mono', 'Fira Code', 'Consolas', monospace;
    --sans:    'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  }}

  body {{
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    min-height: 100vh;
    padding: 48px 32px;
  }}

  .page {{ max-width: 960px; margin: 0 auto; }}

  /* ── Header ── */
  .header {{ margin-bottom: 32px; }}
  .header-top {{ display: flex; align-items: baseline; gap: 16px; margin-bottom: 6px; }}
  .title {{ font-size: 28px; font-weight: 700; letter-spacing: -0.5px; }}
  .subtitle {{ font-size: 16px; color: var(--subtext); }}
  .meta {{ font-size: 13px; color: var(--muted); font-family: var(--mono); }}

  /* ── Stat chips ── */
  .stats {{ display: flex; gap: 12px; margin-bottom: 36px; flex-wrap: wrap; align-items: stretch; }}
  .stat-chip {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px 20px;
    min-width: 130px;
  }}
  .stat-label {{
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--muted); margin-bottom: 6px;
  }}
  .stat-value {{ font-size: 24px; font-weight: 600; font-family: var(--mono); }}
  .stat-value.positive {{ color: #3d9e6e; }}
  .stat-value.neutral  {{ color: var(--text); }}

  /* ── Sparkline chip ── */
  .spark-chip {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px 20px;
    flex: 1;
    min-width: 200px;
  }}

  /* ── Section title ── */
  .section-title {{
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em;
    color: var(--muted); margin-bottom: 14px;
  }}

  /* ── Table ── */
  .table-wrap {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
    margin-bottom: 24px;
  }}
  table {{ width: 100%; border-collapse: collapse; font-family: var(--mono); font-size: 13px; }}
  thead th {{
    padding: 12px 16px; text-align: right;
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.07em;
    color: var(--muted); font-weight: 500;
    border-bottom: 1px solid var(--border); background: var(--bg);
  }}
  thead th:first-child, thead th:nth-child(2) {{ text-align: left; }}
  tbody tr {{ border-bottom: 1px solid var(--border); }}
  tbody tr:last-child {{ border-bottom: none; }}
  tbody tr:hover {{ background: rgba(255,255,255,0.02); }}
  td {{ padding: 13px 16px; }}
  .date-cell    {{ color: var(--subtext); text-align: left; white-space: nowrap; }}
  .bracket-cell {{ color: var(--text); font-weight: 500; text-align: left; }}
  .num-cell     {{ text-align: right; color: var(--subtext); }}
  .pnl-cell     {{ color: var(--text); font-weight: 600; }}

  /* ── Badges ── */
  .badge {{
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 11px; font-weight: 600; letter-spacing: 0.05em;
    text-transform: uppercase;
  }}
  .badge.win  {{ background: rgba(61,158,110,0.15); color: #3d9e6e; }}
  .badge.loss {{ background: rgba(192,57,43,0.15);  color: #e74c3c; }}

  /* ── Pending note ── */
  .pending-note {{
    font-size: 12px; color: var(--muted); font-family: var(--mono);
    margin-bottom: 24px;
  }}

  /* ── Footer ── */
  .footer {{
    font-size: 12px; color: var(--muted); font-family: var(--mono);
    border-top: 1px solid var(--border); padding-top: 16px; margin-top: 8px;
  }}
</style>
</head>
<body>
<div class="page">

  <div class="header">
    <div class="header-top">
      <span class="title">Track Record</span>
      <span class="subtitle">NYC Temperature Markets — Dry Run</span>
    </div>
    <div class="meta">Ensemble model · 173 members · $20/signal · no bias correction applied</div>
  </div>

  <div class="stats">
    <div class="stat-chip">
      <div class="stat-label">Signals</div>
      <div class="stat-value neutral">{n_total}</div>
    </div>
    <div class="stat-chip">
      <div class="stat-label">Win Rate</div>
      <div class="stat-value {'positive' if win_rate >= 50 else 'neutral'}">{win_rate:.0f}%</div>
    </div>
    <div class="stat-chip">
      <div class="stat-label">Total P&amp;L</div>
      <div class="stat-value {'positive' if total_pnl >= 0 else 'neutral'}">${total_pnl:+.2f}</div>
    </div>
    <div class="stat-chip">
      <div class="stat-label">Avg / Signal</div>
      <div class="stat-value {'positive' if total_pnl >= 0 else 'neutral'}">${total_pnl/n_total:+.2f}</div>
    </div>
    <div class="spark-chip">
      <div class="stat-label">Cumulative P&amp;L</div>
      {spark_svg if spark_svg else '<div style="color:var(--muted);font-size:12px;padding-top:8px">Not enough data yet</div>'}
    </div>
  </div>

  <div class="section-title">Signal Log</div>
  {pending_note}
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Date</th>
          <th>Bracket</th>
          <th>Market</th>
          <th>Model</th>
          <th>Edge</th>
          <th>Dir</th>
          <th>Actual</th>
          <th>Result</th>
          <th>P&amp;L</th>
        </tr>
      </thead>
      <tbody>
{signal_rows}      </tbody>
    </table>
  </div>

  <div class="footer">
    Generated {generated_at} · P&amp;L assumes ${DEFAULT_TRADE_SIZE} per signal, Polymarket settlement
  </div>

</div>
</body>
</html>"""

    with open(out_path, "w") as f:
        f.write(html)
    return out_path


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]

    # --set-actual YYYY-MM-DD CITY VALUE — manual resolution for TYO/SIN
    if "--set-actual" in args:
        i = args.index("--set-actual")
        try:
            set_actual(args[i + 1], args[i + 2], args[i + 3])
        except IndexError:
            print("Usage: python3 bot.py --set-actual YYYY-MM-DD CITY VALUE")
        sys.exit(0)

    size    = DEFAULT_TRADE_SIZE
    do_html           = "--html"           in args
    do_log            = "--log"            in args
    do_history        = "--history"        in args
    do_favreport      = "--favreport"      in args
    do_backfill       = "--backfill"       in args
    do_forecastreport = "--forecastreport" in args
    do_track          = "--track"          in args
    do_resolve        = "--resolve"        in args
    do_export_json    = "--export-json"    in args
    args = [a for a in args if a not in (
        "--html", "--log", "--history", "--favreport",
        "--backfill", "--forecastreport", "--track", "--resolve",
        "--export-json")]

    if do_export_json:
        out = export_site_json()
        print(f"Site data written → {out}")
        sys.exit(0)

    if do_favreport:
        favorite_report()
        sys.exit(0)

    if do_backfill:
        rebuild_forecasts()
        sys.exit(0)

    if do_forecastreport:
        forecast_report()
        sys.exit(0)

    if do_resolve:
        resolve_forecasts()
        sys.exit(0)

    # Optional --size flag
    if "--size" in args:
        idx  = args.index("--size")
        size = int(args[idx + 1])
        args = [a for i, a in enumerate(args) if i != idx and i != idx + 1]

    # Positional args: [YYYY-MM-DD] [CITY]  (either order, city is uppercase 2-3 chars)
    positional_date = None
    positional_city = None
    for a in args:
        if a.upper() in CITY_COORDS:
            positional_city = a.upper()
        else:
            try:
                positional_date = date.fromisoformat(a)
            except ValueError:
                pass

    target = positional_date or date.today()

    # Override city globals if a city was given
    if positional_city:
        CITY = positional_city
        lat, lon, tz, _ = CITY_COORDS[CITY]
        LATITUDE        = lat
        LONGITUDE       = lon
        TIMEZONE        = tz
        MODEL_WARM_BIAS = CITY_BIAS.get(CITY, 0.0)

    if do_track:
        track_all(target)
    elif do_history:
        out = generate_history_html()
        print(f"History dashboard written → {os.path.abspath(out)}")
        subprocess.run(["open", out])
    elif do_html:
        # Run data pipeline silently, generate HTML, open in browser
        print("Fetching ensemble forecasts...")
        highs = fetch_ensemble(target)
        print("Fetching Polymarket prices...")
        poly_data = fetch_polymarket(target)
        if poly_data:
            market_prices, market_slugs = poly_data
        else:
            market_prices, market_slugs = {}, {}
        brackets    = parse_brackets(list(market_prices.keys())) if market_prices else _fallback_brackets()
        model_probs = bracket_probs(highs, brackets)
        out = generate_html(target, highs, brackets, model_probs, market_prices)
        print(f"Dashboard written → {os.path.abspath(out)}")
        subprocess.run(["open", out])
    else:
        run(target, trade_size=size, do_log=do_log)
