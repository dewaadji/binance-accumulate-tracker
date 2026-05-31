# Accumulation Radar - Project Guide

## Project Overview

This is a **crypto trading decision assistant** that detects smart-money accumulation patterns in perpetual futures markets and classifies each token's lifecycle state (WAKING_UP → READY → TRIGGERED → ACTIVE_TREND → LATE). It's a pure Python automation tool that:
- Scans for sideways accumulation patterns (low volume + tight range = accumulation phase)
- Monitors Open Interest (OI) anomalies (capital flowing in/out)
- Classifies every interesting token into a `trade_state` (13 lifecycle states) so the output answers *what to do* rather than just *what's hot*
- Persists hourly snapshots so it can compute 1h/3h deltas across runs
- Sends Telegram notifications grouped by **Actionable / Alert / Active-but-Late / Avoid** buckets

**Core thesis**: Smart money accumulates before markup → long sideways + low volume = accumulation → OI spike = large capital entering → markup likely next. The strategy scorers (Momentum Chase, Combined, Ambush, Reversal) are *origins* — they explain why a token is on the radar. The lifecycle classifier decides what action it deserves *right now*.

## Architecture

### Single-file monolith
- `accumulation_radar.py` (~2800 lines) - all logic in one file
- SQLite database: `accumulation.db`
  - `watchlist` — daily pool with v2 fields (`range_position_pct`, `breakout_state`, `pool_setup_state`, `pool_quality_score`, `entry_readiness_score`)
  - `signal_tracker` — performance tracking, now enriched with `trade_state`, `origin_pool_setup_state`, `action_label`
  - `hourly_token_snapshots` — per-symbol per-hour snapshots for 1h/3h delta computation (7-day retention)
  - `alerts`, `app_state` — unchanged
- JSON files: `data/btc_journal/YYYY-MM.json` — stores daily BTC analysis history
- No external dependencies except `requests` library

### Key modules (all in accumulation_radar.py)
1. **Pool scanner** — daily scan for sideways accumulation candidates, emits `breakout_state` and `pool_setup_state`
2. **Lifecycle classifier** — `classify_trade_state()` maps current price/OI/funding + prior snapshots to one of 13 trade states
3. **OI monitor** — hourly scan, persists snapshot per token, runs 4 origin strategies + classifier, outputs by lifecycle bucket
4. **Strategy scorers (origins only)** — Momentum Chase / Combined / Ambush / Reversal — still computed but no longer drive the output layout
5. **Signal tracker** — `/review` shows win rate by both `signal_type` (origin) and `trade_state` (lifecycle)
6. **BTC bias analyzer** — multi-factor daily BTC direction analysis
7. **Telegram bot** — notifications + command listener (`/btc`, `/review`)

## Data Sources (all free, no API keys)

| Data | Endpoint | Notes |
|------|----------|-------|
| Market cap | Binance spot `/bapi/composite/v1/public/marketing/symbol/list` | 434 coins in one request |
| Candles | Binance futures `/fapi/v1/klines` | Historical price data |
| 24h stats | Binance futures `/fapi/v1/ticker/24hr` | Volume, price change |
| OI history | Binance futures `/futures/data/openInterestHist` | Open Interest tracking |
| Funding rate | Binance futures `/fapi/v1/premiumIndex` | All funding rates in one call |

**Market cap fallback chain**: Binance spot API → CMC supply from OI endpoint → rough estimate formula

## Commands & Modes

```bash
python3 accumulation_radar.py pool    # Daily: scan for accumulation candidates
python3 accumulation_radar.py oi      # Hourly: OI anomalies + strategy scores
python3 accumulation_radar.py btc     # Daily: BTC bias brief (7-factor analysis)
python3 accumulation_radar.py review  # Generate signal tracker performance report
python3 accumulation_radar.py full    # Run pool + oi together
```

## Key Parameters & Thresholds

### Accumulation pool criteria
- `MIN_SIDEWAYS_DAYS = 45` - minimum sideways consolidation period
- `MAX_RANGE_PCT = 80` - max price range during sideways (loose for operator charts)
- `MAX_AVG_VOL_USD = 20_000_000` - low volume threshold ($20M)
- `MIN_DATA_DAYS = 50` - minimum historical data required

### OI anomaly detection
- `MIN_OI_DELTA_PCT = 3.0` - OI must change by at least 3%
- `MIN_OI_USD = 2_000_000` - minimum OI threshold ($2M)
- `VOL_BREAKOUT_MULT = 3.0` - volume > 3x average = breakout

### Strategy weights (used as *origin* tags, not the final ranking)
**Momentum Chase**: funding rate ranking (short-term squeeze plays)
**Combined**: 25 pts each for funding/mcap/sideways/OI (balanced)
**Ambush**: mcap 35 + OI 30 + sideways 20 + funding 15 (early positioning)
**Reversal**: aggressive short build / long-squeeze fuel / failed breakout

## Lifecycle States (v2)

Every token classified in OI mode lands in exactly one `trade_state` and is routed to one of 4 output buckets:

### 📍 Actionable Now
- `READY_LONG` — OI 1h ≥ 15% + price > 0 + near resistance, not extended → "Prepare entry. Wait for close breakout or retest."
- `TRIGGERED_LONG` — breakout confirmed (2 closes > range_high) + OI 1h ≥ 15% + volume confirmed → "Entry allowed with risk management."
- `READY_SHORT` — OI 1h > 10% + price 1h < -2% + funding ≥ 0 → "Wait for lower low or failed reclaim."
- `TRIGGERED_SHORT` — breakdown confirmed + failed reclaim + OI 1h > 10% → "Short allowed. Stop above failed reclaim."

### 🟠 Alert / Next Setup
- `EARLY_UNDERFLOW` — OI 1h in [3, 15)% + price flat (|Δ| < 3%) → "Alert only. Wait for OI acceleration + price breakout."

### 🔥 Active but Late
- `ACTIVE_TREND` — price 3h > 20% + OI 3h > 30% + funding < 0 → "Hold/manage. New entry only on pullback/retest."
- `LATE_LONG` — price 24h > 100% OR price_from_breakout > 30% OR breakout_state EXTENDED → "NO CHASE. Retest only."
- `LATE_SHORT` — price 24h < -20% → "Do not chase short. Wait bounce/retest."

### ⚠️ Avoid / Deprioritize
- `NO_CONFIRMATION` — OI ≤ 0 + price flat → "Deprioritize / watch only."
- `SHORT_COVERING_ONLY` — price 1h > 0 + OI 1h < 0 → "No fresh long unless OI turns positive again."
- `DISTRIBUTION_RISK` — price stalling + OI still rising + price 24h > 30% → "Avoid new long. Watch reversal."
- `EXIT_WARNING` — price 24h > 30% + OI 1h < -10% → "Take profit / tighten stop. No new long."
- `INVALIDATED` — setup broken

### Pool Setup States (from daily pool scan)
- `SLEEPING_ACCUMULATION` — low volume, still inside range
- `WAKING_UP` — vol_breakout 1.5–3x, still inside range (early signal)
- `ARMED_INSIDE_RANGE` — vol_breakout ≥ 3x, still inside range (volume anomaly without price breakout yet)
- `PRICE_BREAKOUT_CONFIRMED` — range_position 90–125% (post-breakout retest window)
- `EXTENDED_BREAKOUT` — range_position > 125% (no fresh long, retest only)

## Configuration

### Environment (.env.oi)
```bash
TG_BOT_TOKEN=your_bot_token          # Telegram bot token
TG_CHAT_ID=your_chat_id              # Telegram chat ID
DB_PATH=accumulation.db              # SQLite database path
```

### Crontab (recommended schedule)
```crontab
0 0 * * *    python3 accumulation_radar.py pool    # Daily pool scan
30 0 * * *   python3 accumulation_radar.py btc     # Daily BTC brief
30 * * * *   python3 accumulation_radar.py oi      # Hourly OI scan
```

### Telegram commands
- `/btc` - today's BTC bias brief
- `/review` - signal tracker performance
- `/help` - command list

## Code Conventions

- **No classes** - pure functional style with global state
- **Direct API calls** - no abstraction layers, requests library only
- **Inline SQL** - SQLite queries written directly in functions
- **Minimal error handling** - fail fast, log to stdout/stderr
- **UTC timestamps** - all times in UTC, convert to CST for display
- **Emoji indicators** - 🔥🧊💎💤⚡🎯 used extensively in output

## Important Constraints

1. **Only 10x+ moves matter** - goal is operator-driven explosions like RAVE 138x, not slow uptrends
2. **Accumulation can last 3-4 months** - sideways range can stretch to 124%
3. **Short fuel is critical** - negative funding = more shorts = more squeeze fuel
4. **No AI, no paid APIs** - pure Python + free Binance data = $0/month cost
5. **Single timezone** - everything in UTC, display in CST (UTC+8)

### Output format (Hourly OI)

Tokens are routed by `trade_state` into 4 buckets, sorted within each:

```
📍 ACTIONABLE NOW (READY/TRIGGERED)
🟢 ALLO — TRIGGERED_LONG
   Origin: PRICE_BREAKOUT_CONFIRMED | via: combined+heat | PoolQ:74 | Sideways 91d | Vol 5.8x
   Now: Move +42% | OI +87% (6h) | Funding -0.190%
   1h Δ: Price +5% | OI +18% | Funding -0.05 → -0.19
   3h Δ: Price +18% | OI +52%
   Transition: ARMED → READY_LONG → TRIGGERED_LONG
   Action: Entry allowed with risk management.
   Risk: Stop below retest low or range reclaim · Reduce size if 24h > 50%

🟠 ALERT / NEXT SETUP (EARLY_UNDERFLOW)
🔥 ACTIVE BUT LATE (ACTIVE_TREND / LATE_LONG / LATE_SHORT)
⚠️ AVOID / DEPRIORITIZE (NO_CONFIRMATION / SHORT_COVERING / EXIT_WARNING / DISTRIBUTION_RISK)

📚 Strategy Roster (origins)
   Momentum: COIN1, COIN2, ...
   Combined: COIN1, COIN2, ...
```

**On first run** (no prior snapshot yet) most tokens are classified as `NO_CONFIRMATION` because 1h/3h deltas are None — this is by design (graceful degradation). Tokens triggering rules that don't need deltas (LATE_LONG via `price_from_breakout > 30`, LATE_SHORT via `price_24h < -20`) still classify correctly. The system becomes fully useful after 2 hourly runs.

## When Making Changes (v2 considerations)

### Adding new strategies
- Add scoring logic to the `oi` mode section
- Update Telegram notification format
- Consider adding to `AGENTS.md` strategy documentation

### Modifying thresholds
- Test against historical data first (check `accumulation.db` for past signals)
- Document reasoning in commit message
- Update README.md if user-facing

### Database changes
- SQLite schema is created on first run
- Add migrations carefully - no ORM, raw SQL only
- Backup `accumulation.db` before schema changes

### API changes
- Binance API is rate-limited but generous
- Add exponential backoff for failures
- Cache aggressively (market cap data changes slowly)

## Testing Approach

- **No unit tests** - this is a research/trading tool, not production software
- **Manual verification** - run commands and check Telegram output
- **Historical backtesting** - compare signals against actual 10x+ moves
- **Live monitoring** - let it run on cron and observe false positives

## Common Tasks

### Add a new signal indicator
1. Add calculation logic in the relevant mode (`pool` or `oi`)
2. Update database schema if persistence needed
3. Add to Telegram notification format
4. Document in README.md

### Debug missed signals
1. Check `accumulation.db` - was the coin in the pool?
2. Review OI history - did it meet `MIN_OI_DELTA_PCT`?
3. Check volume - did it trigger `VOL_BREAKOUT_MULT`?
4. Verify funding rate - was it negative enough?

### Adjust for market conditions
- Bull market: tighten `MIN_OI_DELTA_PCT`, lower `MAX_AVG_VOL_USD`
- Bear market: loosen `MAX_RANGE_PCT`, increase `MIN_SIDEWAYS_DAYS`
- High volatility: raise `MIN_OI_USD` to filter noise

## Deployment

### Local (development)
```bash
python3 accumulation_radar.py oi  # Test manually
```

### Docker (production)
```bash
docker-compose up -d  # Runs cron + Telegram listener
docker logs -f accumulation-radar
```

### VPS (recommended)
- Ubuntu 20.04+ with Python 3.8+
- Set up crontab as shown above
- Monitor logs: `tail -f data/logs/accumulation.log`

## Performance Notes

- Pool scan: ~2-3 minutes (scans 400+ coins)
- OI scan: ~30-60 seconds (only scans pool candidates)
- Database: SQLite is fine for this workload (< 1000 rows)
- Memory: < 100MB typical usage
- API calls: ~50-100 per hour (well within Binance limits)

## Known Limitations

1. **Binance-only** - no multi-exchange support
2. **USDT perps only** - no spot or coin-margined futures
3. **No backtesting engine** - manual historical analysis required
4. **No position sizing** - user must calculate risk manually
5. **No auto-trading** - signals only, execution is manual
