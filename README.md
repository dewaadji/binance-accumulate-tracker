# 🏦 Accumulation Radar

Fully automated scanning for smart-money accumulation signals across the crypto perpetual futures market: sideways accumulation detection + OI anomaly monitoring + three independent strategy scores. Pure Python, zero AI cost, Telegram notifications.

## Core Idea

> Smart money has to accumulate before a markup move -> long sideways trading + low volume = accumulation in progress -> OI spike = large capital entering -> markup likely next

- **Only 10x+ counts as a real explosion**. The goal is to catch operator-driven charts like `RAVE 138x` and `STO 38x`, not slow fundamental uptrends.
- The accumulation phase can last 3-4 months, and the sideways range can stretch as wide as 124%.
- Short fuel matters: after a move starts, you still need a lot of people shorting it. No shorts means no fuel for the next squeeze.

## Three Independent Strategies

### 🔥 Momentum Chase - Pure Funding Ranking (short-term squeeze)
| Metric | Meaning |
|------|------|
| Negative funding | More negative = more traders are short = more short fuel |
| 🔥Accelerating | Funding is more negative than last period, shorts are still adding |
| ⬇️Turned Negative | Funding flipped from positive to negative, fresh shorts just entered |
| ⬆️Rebounding | Shorts are fading, fuel is decreasing |

Requirements: price change `> 3%` + negative funding + volume `> $1M`

### 📊 Combined - Four Balanced Dimensions (25 points each = 100)
| Icon | Dimension | Max |
|------|------|------|
| 🧊 | Funding rate (more negative is better) | 25 |
| 💎 | Market cap (lower is better) | 25 |
| 💤 | Sideways days (longer is better) | 25 |
| ⚡ | OI change (larger is better) | 25 |

### 🎯 Ambush - Early Positioning (mid/long-term)
| Dimension | Weight | Logic |
|------|------|------|
| 💎 Market cap | **35** | Full score below `$50M`; lower market cap = more upside |
| ⚡ OI | 30 | OI anomaly = large capital entering |
| 💤 Sideways | 20 | Full score at `>= 120` days; measures accumulation time |
| 🧊 Funding | 15 | Negative funding is a bonus |

Requirements: inside the accumulation pool + price gain `< 50%`

### 💡 Worth Watching (automatic alerts)
- 🔥 Funding is getting worse fast - shorts are piling in aggressively
- ⭐ Listed in two rankings - multiple signals align
- 🎯 Underflow - OI changes while price stays flat, the classic accumulation signal
- 💎 Low market cap + OI anomaly - top ambush candidate

## Data Sources

All data comes from free public APIs. No API key required:

| Data | Endpoint | Notes |
|------|------|------|
| Real circulating market cap | Binance spot `bapi/composite/v1/public/marketing/symbol/list` | One request returns market caps for 434 coins |
| Candles / market data | Binance futures `/fapi/v1/klines`, `/fapi/v1/ticker/24hr` | Historical candles + 24h market data |
| OI history | Binance futures `/futures/data/openInterestHist` | Includes CMC circulating supply as fallback |
| Funding rate | Binance futures `/fapi/v1/premiumIndex` | Fetches all funding rates in one request |

**Three-level market-cap fallback**: Binance spot API -> futures OI endpoint `CMCCirculatingSupply * price` -> rough estimate formula

## Install & Configure

```bash
git clone https://github.com/connectfarm1/accumulation-radar.git
cd accumulation-radar

# Python 3.8+ is enough, the only dependency is requests
pip install requests

# Configure Telegram notifications (optional)
cp .env.example .env.oi
# Edit .env.oi and fill in your TG_BOT_TOKEN and TG_CHAT_ID
```

### Create A Telegram Bot
1. Open [@BotFather](https://t.me/BotFather) and send `/newbot`
2. Get your bot token
3. Send a message to your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to get your chat ID

## Usage

```bash
# Run once per day: scan the full market for accumulation-pool candidates
python3 accumulation_radar.py pool

# Run once per hour: three strategy scores + OI anomaly monitoring
python3 accumulation_radar.py oi

# BTC daily bias brief (multi-factor analysis)
python3 accumulation_radar.py btc

# Sync all active trades with current price conditions
python3 accumulation_radar.py sync

# Generate monthly perps trade statistics
python3 accumulation_radar.py perps

# Start Telegram command listener (handles /limit, /perps, /review, /help)
python3 accumulation_radar.py listen

# Run everything (pool + oi)
python3 accumulation_radar.py full
```

### Recommended Crontab

```crontab
# Update the accumulation pool every day at 00:00 UTC
0 0 * * *  cd /path/to/accumulation-radar && python3 accumulation_radar.py pool >> data/logs/accumulation.log 2>&1

# BTC daily brief at 00:30 UTC (after pool scan)
30 0 * * * cd /path/to/accumulation-radar && python3 accumulation_radar.py btc >> data/logs/accumulation_btc.log 2>&1

# Scan OI anomalies + three strategy scores every hour at :30
30 * * * *  cd /path/to/accumulation-radar && python3 accumulation_radar.py oi >> data/logs/accumulation_oi.log 2>&1

# Sync trade journal every 12 hours
0 */12 * * * cd /path/to/accumulation-radar && python3 accumulation_radar.py sync >> data/logs/accumulation_sync.log 2>&1
```

## Sample Notification

```
🏦 Smart Money Radar - Three Strategies
⏰ 2026-04-24 09:51 CST

🔥 Momentum Chase (ranked by funding)
  RED     Funding -1.003% 🔥Accelerating | +17% | ~$57M
  KAT     Funding -0.627% 🔥Accelerating | +45% | ~$36M
  MOVR    Funding -0.146% 🔥Accelerating | +56% | ~$30M

📊 Combined (Funding + Market Cap + Sideways + OI, 25 each)
  MOVR    86 pts | 🧊-0.15% 💎$30M 💤71d ⚡OI-22%
  KAT     75 pts | 🧊-0.63% 💎$36M ⚡OI+33%

🎯 Ambush (Market Cap 35 + OI 30 + Sideways 20 + Funding 15)
  RARE    82 pts | ~$18M OI-24% Sideways 75d
  SAGA    74 pts | ~$15M OI+4% 🎯Underflow Sideways 77d

💡 Worth Watching
  🔥 RED funding -1.003% is accelerating lower, shorts are still flooding in
  🎯 SAGA underflow! OI +4% while price is flat, market cap only $15M

📖 Legend
  Negative funding = many shorts (fuel) | 🔥Accelerating/⬇️Turned Negative/⬆️Rebounding = funding trend
  💎 Market cap | 💤 Sideways days (accumulation duration) | ⚡ OI change (capital anomaly)
  🎯 Underflow = OI moves while price does not (accumulation signal)
```

## How To Read OI Signals

| OI | Price | Signal | Meaning |
|----|------|------|------|
| ↑ | ↑ | 🟢 Aggressive long build | Trend confirmation |
| ↑ | ↓ | 🔴 Aggressive short build | Shorts opening positions |
| ↑ | Flat | ⚡ Underflow | **Best ambush setup** |
| ↓ | ↑ | 💪 Squeeze | Shorts getting liquidated |
| ↓ | ↓ | 💨 Exit wave | Longs stopping out |

## Trade Journal

A built-in trade journal for tracking limit trade setups. All data stored as JSON files under `data/journal/YYYY-MM.json`.

### BTC Daily Brief (`btc` mode)

Multi-factor BTC bias analysis that runs daily at 00:30 UTC. Uses 7 factors to determine bias:

| Factor | Signal |
|--------|--------|
| EMA 21/55/200 alignment | Bullish/bearish/neutral alignment |
| RSI 14 | Overbought/mid/oversold zone |
| Funding rate trend | Positive/negative/neutral |
| OI 24h delta | Capital flowing in/out |
| Volume vs 20d avg | Conviction level |
| Support/resistance | Nearest key levels from swing highs/lows |

Result is saved to the journal and sent via Telegram with a confidence rating (High/Medium/Low).

### Telegram Commands

The app can long-poll Telegram for commands. Start the listener:

```bash
python3 accumulation_radar.py listen
```

Or enable passive command checking in `oi` mode by setting `TG_POLL_COMMANDS_IN_OI=1` in `.env.oi`.

| Command | Description | Example |
|---------|-------------|---------|
| `/btc` | Show today's BTC bias brief (auto-generates if past 00:30 UTC) | `/btc` |
| `/limit` | Add a new limit trade setup | `/limit short BTC 81000 invalid 81400 sl 81500 tp1 79000 tp2 78000 tp3 77000` |
| `/perps` | Show current month perps trade statistics | `/perps` |
| `/review` | Signal tracker performance report | `/review` |
| `/help` | List available commands | `/help` |

#### `/limit` Format

```
/limit <long|short> <SYMBOL> <entry> invalid <price> sl <price> tp1 <price> [tp2 <price> ...]
```

- **direction**: `long` or `short`
- **symbol**: any USDT perpetual (e.g., `BTC`, `ETH`, `SOL`)
- **entry**: limit order entry price
- **invalid**: price level that invalidates the setup (before entry fills)
- **sl**: stop loss price
- **tp1, tp2, ...**: take profit levels (minimum 1, unlimited maximum)

Example:
```
/limit short BTC 81000 invalid 81400 sl 81500 tp1 79000 tp2 78000 tp3 77000
```

#### `/perps` Output Example

```
📈 Perps Journal — 2026-05

Trades: 15 | ✅ Complete: 6 | ❌ Stopped: 3
⏳ Active: 2 | 🕐 Pending: 3 | 🚫 Invalid: 1

Win Rate: 66.7% (6/9 resolved)
Avg R:R: +2.4R | Avg ROI: +3.8%

🏆 Best: BTC SHORT +6.0R (+7.4%)
💀 Worst: ETH LONG -1.0R (-2.1%)

Long: 6 (3W/2L/1A) | Short: 9 (3W/1L/1A/3P/1I)
```

### Trade Lifecycle

Trades progress through these statuses:

```
pending → active (entry filled) → completed (all TPs hit)
                               → stopped_out (SL hit)
pending → invalidated (price hit invalid level before entry filled)
```

The `sync` cron (every 12 hours) checks 1h kline data for each active trade, detecting level crossings to update statuses automatically.

## Cost

- **$0/month** - pure Python + public APIs
- No AI calls, no paid API keys
- Binance APIs are free with relaxed rate limits

## License

MIT
