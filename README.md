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

## Results (as of May 2026)

| Metric | Value |
|---|---|
| Total P&L | +$72.94 |
| Win Rate | 58.8% (10/17) |
| Total Staked | $264 |
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

## Disclaimer

This is for educational and entertainment purposes. Prediction market trading involves financial risk. Past performance does not guarantee future results. Always trade responsibly.

---

*Built with Open-Meteo · Iowa State ASOS · Polymarket Gamma API*
