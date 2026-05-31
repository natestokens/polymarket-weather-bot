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
# NYC  +1.4°F: 29-day LGA analysis (Apr 17 – May 15), warm 76% of days.  [13 results ✓]
# MIA  -2.2°F: calibrated May 17–28, ensemble consistently under-shot.    [ 5 results ✓]
# CHI   0.0°F: 3 results only — correction withheld until 5 verified.
# LAX   0.0°F: 0 results — correction withheld until 5 verified.
CITY_BIAS = {
    "NYC": 1.4,
    "MIA": -2.2,
    "CHI": 0.0,
    "LAX": 0.0,
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

def fetch_polymarket(target: date) -> Optional[Tuple[dict, dict]]:
    """
    Return (prices, slugs) where both are {outcome_label: value}, or None on failure.
    prices: {label: probability 0-1}
    slugs:  {label: market_slug}
    """
    day   = target.strftime("%-d")
    month = target.strftime("%B").lower()
    year  = target.strftime("%Y")
    city_slug  = CITY_COORDS.get(CITY, CITY_COORDS["NYC"])[3]
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
    size    = DEFAULT_TRADE_SIZE
    do_html    = "--html"    in args
    do_log     = "--log"     in args
    do_history = "--history" in args
    args = [a for a in args if a not in ("--html", "--log", "--history")]

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

    if do_history:
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
