#!/usr/bin/env python3
"""
Forward verification — checks signals.csv against actual outcomes.

Run this daily (or whenever you want to update results).
It fetches actual highs for any logged signal date that has passed
and hasn't been verified yet, then updates P&L in signals.csv.

Usage: python3 verify.py [--summary]
"""

import csv
import json
import math
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

LOG_FILE = Path(__file__).parent / "signals.csv"

FIELDS = [
    "date", "bracket", "bracket_lo", "bracket_hi",
    "market_price", "model_prob", "edge", "direction", "trade_size",
    "logged_at", "actual_high", "pnl", "result",
]

# ── Station map — Polymarket resolution source per city ──────────────────────
# Key: city tag used in bracket label suffix e.g. "88-89°F (MIA)"
# Value: (ASOS station code, timezone string for Iowa State API)
STATIONS = {
    "NYC": ("LGA", "America%2FNew_York"),   # LaGuardia — default
    "MIA": ("MIA", "America%2FNew_York"),   # Miami International
    "LAX": ("LAX", "America%2FLos_Angeles"),
    "CHI": ("ORD", "America%2FChicago"),
    "LON": ("EGLL", "Europe%2FLondon"),
    "PAR": ("LFPG", "Europe%2FParis"),
}
DEFAULT_STATION = "NYC"

def _station_for_bracket(bracket: str) -> tuple:
    """Detect city from bracket label suffix e.g. '88-89°F (MIA)' → MIA station."""
    import re
    m = re.search(r'\(([A-Z]{2,4})\)', bracket)
    if m:
        tag = m.group(1)
        if tag in STATIONS:
            return STATIONS[tag]
    return STATIONS[DEFAULT_STATION]

# ── Fetch actual high (ASOS — matches Polymarket resolution source) ───────────

def fetch_actual_high(d: date, station: str = "LGA", tz: str = "America%2FNew_York") -> Optional[float]:
    """Airport ASOS readings — same source Wunderground/Polymarket uses."""
    url = (
        f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
        f"?station={station}&data=tmpf"
        f"&year1={d.year}&month1={d.month}&day1={d.day}"
        f"&year2={d.year}&month2={d.month}&day2={d.day}"
        f"&tz={tz}&format=comma&latlon=no&missing=M&trace=T&direct=no&report_type=3"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "weather-verify/1.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            text = r.read().decode()
    except Exception:
        return None
    temps = []
    for line in text.splitlines():
        if not line.startswith(f"{station},"):
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue
        try:
            temp = float(parts[2])
            temps.append(temp)  # include all hours — Wunderground counts midnight onwards
        except (ValueError, IndexError):
            pass
    if not temps:
        return None
    return max(temps) if temps else None

# ── P&L ──────────────────────────────────────────────────────────────────────

def calc_pnl(direction: str, market_p: float, bet_won: bool, size: float) -> float:
    if direction == "YES":
        return size * (1 / market_p - 1) if bet_won else -size
    else:
        no_p = 1 - market_p
        return size * (1 / no_p - 1) if bet_won else -size

def _inf(s: str) -> float:
    s = s.strip()
    if s in ("inf", "Infinity"):  return math.inf
    if s in ("-inf", "-Infinity"): return -math.inf
    return float(s)

# ── Main ──────────────────────────────────────────────────────────────────────

def run(summary_only: bool = False):
    if not LOG_FILE.exists():
        print("signals.csv not found. Run: python3 bot.py --log")
        return

    with open(LOG_FILE, newline="") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print("signals.csv is empty.")
        return

    updated = 0
    cache = {}

    for row in rows:
        if row.get("result"):
            continue  # already verified
        d = date.fromisoformat(row["date"])
        if d >= date.today():
            continue  # market hasn't resolved yet

        station, tz = _station_for_bracket(row["bracket"])
        cache_key = (d, station)
        if cache_key not in cache:
            print(f"  Fetching actual high for {d} ({station})...")
            cache[cache_key] = fetch_actual_high(d, station, tz)

        actual = cache[cache_key]
        if actual is None:
            continue  # data not available yet (archive lag ~2-5 days)

        lo = _inf(row["bracket_lo"])
        hi = _inf(row["bracket_hi"])
        bracket_won = lo <= actual <= hi

        direction  = row["direction"]
        market_p   = float(row["market_price"])
        trade_size = float(row["trade_size"])
        bet_won    = (direction == "YES" and bracket_won) or \
                     (direction == "NO"  and not bracket_won)

        pnl = calc_pnl(direction, market_p, bet_won, trade_size)

        row["actual_high"] = f"{actual:.1f}"
        row["pnl"]         = f"{pnl:.2f}"
        row["result"]      = "WIN" if bet_won else "LOSS"
        updated += 1

    # Write back
    with open(LOG_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n  Updated {updated} outcome(s).\n")

    # ── Summary ───────────────────────────────────────────────────────────────
    verified = [r for r in rows if r.get("result")]
    if not verified:
        print("  No verified signals yet.")
        return

    total_pnl = sum(float(r["pnl"]) for r in verified)
    wins      = sum(1 for r in verified if r["result"] == "WIN")
    n         = len(verified)

    print(f"  {'Date':<12} {'Bracket':<12} {'Mkt':>6} {'Model':>7} {'Edge':>8}  "
          f"{'Dir':<5} {'Actual':>7}  {'Result':<6} {'P&L':>8}")
    print(f"  {'─'*76}")

    cumulative = 0.0
    for r in sorted(verified, key=lambda x: x["date"]):
        cumulative += float(r["pnl"])
        actual_str = f"{float(r['actual_high']):.0f}°F" if r.get("actual_high") else "?"
        edge_str   = f"{float(r['edge']):+.1f}pp"
        print(
            f"  {r['date']:<12}"
            f" {r['bracket']:<12}"
            f" {float(r['market_price'])*100:>5.1f}%"
            f" {float(r['model_prob'])*100:>6.1f}%"
            f" {edge_str:>8}"
            f"  {r['direction']:<5}"
            f" {actual_str:>7}"
            f"  {r['result']:<6}"
            f" ${float(r['pnl']):>+6.2f}"
        )

    print(f"\n  {'─'*50}")
    print(f"  Signals verified: {n}")
    print(f"  Win rate:         {wins/n*100:.1f}%  ({wins}/{n})")
    print(f"  Total P&L:        ${total_pnl:+.2f}")
    if n:
        print(f"  Avg per signal:   ${total_pnl/n:+.2f}")

    pending = [r for r in rows if not r.get("result")]
    if pending:
        future  = sum(1 for r in pending if date.fromisoformat(r["date"]) >= date.today())
        no_data = len(pending) - future
        if future:  print(f"\n  {future} signal(s) pending — market not yet resolved.")
        if no_data: print(f"  {no_data} signal(s) waiting on archive data (lag ~2-5 days).")
    print()


if __name__ == "__main__":
    summary_only = "--summary" in sys.argv
    run(summary_only=summary_only)
