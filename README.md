# 🌦️ Polymarket Weather Bot

> An ensemble weather model that finds pricing inefficiencies in Polymarket temperature markets — and bets on them.

Built live on YouTube by [@rainmoneymaker](https://polymarket.com/@rainmoneymaker).

---

## What it does

- Pulls a **173-member ensemble forecast** (ECMWF IFS, ECMWF AIFS, NCEP GEFS, ICON) from Open-Meteo
- Applies a **city-specific bias correction** calibrated against historical ASOS airport data
- Fetches **live Polymarket market prices** for daily temperature brackets (NYC, Chicago, Miami)
- Calculates **edge** (model probability vs market price) and flags actionable signals
- Logs trades to `signals.csv` and auto-verifies results against official ASOS data

**No API keys required.** All data sources are free and public.

---

## Results (as of May 27, 2026)

| Metric | Value |
|---|---|
| Total P&L | +$77.01 |
| Win Rate | 60.9% (14/23) |
| Total Staked | $279 |
| ROI | +27.6% |

---

## Files

| File | What it does |
|---|---|
| `bot.py` | Main bot — runs ensemble analysis and prints signals |
| `verify.py` | Checks resolved trades against ASOS actuals, updates signals.csv |
| `signals.csv` | Full trade log with outcomes (use as backtest reference) |
| `performance.html` | Live P&L dashboard — open in any browser |
| `pnl-cards.html` | Polymarket-style P&L card generator for sharing wins |

---

## Requirements

- Python 3.8+
- No pip installs needed for core analysis
- Internet connection (fetches live data from Open-Meteo, Iowa State ASOS, Polymarket)

---

## Usage

**Run analysis for today (NYC by default):**
```bash
python3 bot.py
```

**Run for a specific city and date:**
```bash
python3 bot.py 2026-05-25 NYC
python3 bot.py 2026-05-25 CHI
python3 bot.py 2026-05-25 MIA
```

**Verify and update trade results:**
```bash
python3 verify.py --summary
```

**Open the performance dashboard:**
```bash
open performance.html
```

---

## How the edge is calculated

```
edge (pp) = model_probability - market_price
```

- **Negative edge on a bracket** → the market is overpricing YES → bet NO
- **Positive edge on a bracket** → the market is underpricing YES → bet YES
- Signals only fire above 5pp (NO) or 15pp (YES) threshold

---

## City bias corrections

The ensemble runs warm/cool vs reality depending on the city. These corrections are applied to the mean before signal generation:

| City | Bias | Notes |
|---|---|---|
| NYC | +1.4°F warm | 29-day LaGuardia analysis |
| MIA | −2.2°F cool | 4-day Miami Intl analysis |
| CHI | +1.4°F warm | Assumed NYC-equivalent (limited data) |
| LAX | +1.4°F warm | Assumed NYC-equivalent (limited data) |

---

## Key Learnings

### NWS as a tiebreaker
When the ensemble disagrees with market pricing, the **National Weather Service forecast** (`api.weather.gov`) is a powerful tiebreaker. NWS human forecasters often catch local effects the ensemble misses. If the ensemble says one thing and NWS says another, weight NWS heavily for single-day forecasts.

### MEAN_BUFFER: stay away from the middle
Bracket markets near the ensemble mean are high-variance bets — the model has genuine uncertainty there. The bot uses a **3°F buffer** around the ensemble mean: NO signals within 3°F of the corrected mean are flagged with a warning and should be traded conservatively or skipped.

### International markets (Tokyo, Singapore)
Polymarket has temperature markets for Asian cities using **single-degree Celsius brackets**. The bot can analyze these with ad-hoc scripts but lacks built-in Celsius support and bias calibration. Early observations:
- **Tokyo** (RJTT): ensemble appears to run slightly cool vs actual
- **Singapore** (WSSS): consistent underforecast similar to Miami's cool bias pattern — early signal is the ensemble undershoots by ~1.5°C

### Staking discipline
Sizing matters more than win rate. The two biggest wins (Chicago May 21: +$35.76, Miami May 17: +$24.52) came from higher-conviction plays with appropriate size. Minimum-stake trades ($1-2) are fine for data collection and international exploration.

---

## Disclaimer

This is for educational and entertainment purposes. Prediction market trading involves financial risk. Past performance does not guarantee future results. Always trade responsibly.

---

*Built with Open-Meteo · Iowa State ASOS · Polymarket Gamma API*
