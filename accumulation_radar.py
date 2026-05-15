#!/usr/bin/env python3
"""
Accumulation Radar v1 - detect sideways smart-money accumulation + OI anomalies

Core logic (from Patrick):
1. Smart money must accumulate before a markup move -> long sideways action + low volume = accumulation in progress
2. OI explosion = large capital entering and building positions = markup may be next
3. When both signals overlap, the setup is strongest

Two modules:
A. Sideways accumulation pool (scan once per day) -> find coins currently being accumulated
B. OI anomaly monitor (scan hourly) -> alert immediately when a coin in the pool shows OI anomalies

Data source: Binance futures API (free public data, zero cost)
"""

import json
import os
import sys
import time
import requests
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

# === Load .env ===
env_file = Path(__file__).parent / ".env.oi"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

# === Config ===
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
FAPI = "https://fapi.binance.com"
_default_db_path = Path(__file__).parent / "accumulation.db"
DB_PATH = Path(os.getenv("DB_PATH", str(_default_db_path)))
TG_POLL_COMMANDS_IN_OI = os.getenv("TG_POLL_COMMANDS_IN_OI", "1").strip() == "1"

# Accumulation pool parameters
MIN_SIDEWAYS_DAYS = 45        # At least 45 sideways days
MAX_RANGE_PCT = 80            # Sideways period price range < 80% (loose threshold for operator-driven charts)
MAX_AVG_VOL_USD = 20_000_000  # Average daily volume < $20M (low volume suggests accumulation)
MIN_DATA_DAYS = 50            # At least 50 days of data

# OI anomaly parameters
MIN_OI_DELTA_PCT = 3.0        # OI change must be at least 3%
MIN_OI_USD = 2_000_000        # Minimum OI threshold: $2M

# Volume breakout parameter
VOL_BREAKOUT_MULT = 3.0       # Daily volume > 3x average = breakout
BLOCKED_ALERT_SENT = False
LAST_API_FAILURES = {}

# Journal config
_default_journal_dir = Path(__file__).parent / "data" / "journal"
if DB_PATH.is_absolute() and str(DB_PATH).startswith("/data/"):
    _default_journal_dir = DB_PATH.parent / "journal"
JOURNAL_DIR = Path(os.getenv("JOURNAL_DIR", str(_default_journal_dir)))

_default_spot_journal_dir = Path(__file__).parent / "data" / "spot_journal"
if DB_PATH.is_absolute() and str(DB_PATH).startswith("/data/"):
    _default_spot_journal_dir = DB_PATH.parent / "spot_journal"
SPOT_JOURNAL_DIR = Path(os.getenv("SPOT_JOURNAL_DIR", str(_default_spot_journal_dir)))


def ensure_journal_dir():
    """Ensure journal directory exists."""
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)


def load_journal(month_str):
    """Load journal JSON for a given month (YYYY-MM)."""
    ensure_journal_dir()
    path = JOURNAL_DIR / f"{month_str}.json"
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {"month": month_str, "btc_briefs": [], "trades": []}
    return {"month": month_str, "btc_briefs": [], "trades": []}


def save_journal(month_str, data):
    """Save journal JSON for a given month."""
    ensure_journal_dir()
    path = JOURNAL_DIR / f"{month_str}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def ensure_spot_journal_dir():
    """Ensure spot journal directory exists."""
    SPOT_JOURNAL_DIR.mkdir(parents=True, exist_ok=True)


def load_spot_journal(month_str):
    """Load spot journal JSON for a given month (YYYY-MM)."""
    ensure_spot_journal_dir()
    path = SPOT_JOURNAL_DIR / f"{month_str}.json"
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {"month": month_str, "trades": []}
    return {"month": month_str, "trades": []}


def save_spot_journal(month_str, data):
    """Save spot journal JSON for a given month."""
    ensure_spot_journal_dir()
    path = SPOT_JOURNAL_DIR / f"{month_str}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def notify_data_blocked(reason=""):
    """Send a one-time alert when upstream market data appears to be blocked."""
    global BLOCKED_ALERT_SENT
    if BLOCKED_ALERT_SENT:
        return
    BLOCKED_ALERT_SENT = True

    r = (reason or "").lower()
    looks_blocked = any(
        s in r
        for s in (
            "http 403",
            "http 418",
            "http 451",
            "forbidden",
            "blocked",
            "connection reset",
        )
    )
    msg = "Data gagal didapat dari API"
    if looks_blocked:
        msg = "Data gagal didapat, kemungkinan diblokir ISP/provider"
    if reason:
        msg = f"{msg}\nReason: {reason}"
    send_telegram(msg)


def api_get(endpoint, params=None):
    """Send a Binance API request."""
    url = f"{FAPI}{endpoint}"
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                if endpoint in LAST_API_FAILURES:
                    del LAST_API_FAILURES[endpoint]
                return resp.json()
            elif resp.status_code in (403, 418, 451):
                body = (resp.text or "").strip().replace("\n", " ")[:220]
                LAST_API_FAILURES[endpoint] = f"HTTP {resp.status_code} {body}".strip()
                notify_data_blocked(f"HTTP {resp.status_code} from {endpoint}")
                return None
            elif resp.status_code == 429:
                body = (resp.text or "").strip().replace("\n", " ")[:220]
                LAST_API_FAILURES[endpoint] = f"HTTP 429 {body}".strip()
                time.sleep(2)
            else:
                body = (resp.text or "").strip().replace("\n", " ")[:220]
                LAST_API_FAILURES[endpoint] = f"HTTP {resp.status_code} {body}".strip()
                return None
        except requests.exceptions.RequestException as e:
            # Connection-level errors often indicate ISP/provider-level blocking.
            err = str(e).lower()
            LAST_API_FAILURES[endpoint] = str(e)
            if "forbidden" in err or "blocked" in err or "connection reset" in err:
                notify_data_blocked(f"request error on {endpoint}: {e}")
            time.sleep(1)
    return None


def init_db():
    """Initialize the database."""
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS watchlist (
        symbol TEXT PRIMARY KEY,
        coin TEXT,
        added_date TEXT,
        sideways_days INT,
        range_pct REAL,
        avg_vol REAL,
        low_price REAL,
        high_price REAL,
        current_price REAL,
        score REAL,
        status TEXT DEFAULT 'watching',
        last_oi_alert TEXT,
        notes TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT,
        alert_type TEXT,
        alert_time TEXT,
        price REAL,
        oi_delta_pct REAL,
        vol_ratio REAL,
        details TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS signal_tracker (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT,
        coin TEXT,
        signal_type TEXT,
        signal_time TEXT,
        signal_price REAL,
        range_high REAL,
        range_low REAL,
        entry_price REAL,
        entry_time TEXT,
        status TEXT DEFAULT 'pending',
        outcome_price REAL,
        outcome_time TEXT,
        pnl_pct REAL,
        score REAL,
        notes TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS app_state (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    conn.commit()
    return conn


def get_app_state(conn, key, default=None):
    c = conn.cursor()
    row = c.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_app_state(conn, key, value):
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO app_state(key, value) VALUES(?, ?)", (key, str(value)))
    conn.commit()


def get_all_perp_symbols():
    """Fetch all USDT perpetual symbols."""
    info = api_get("/fapi/v1/exchangeInfo")
    if not info:
        return []
    return [s["symbol"] for s in info["symbols"]
            if s["quoteAsset"] == "USDT" 
            and s["contractType"] == "PERPETUAL"
            and s["status"] == "TRADING"]


def analyze_accumulation(symbol, klines):
    """Analyze the accumulation characteristics of one coin."""
    if len(klines) < MIN_DATA_DAYS:
        return None
    
    data = []
    for k in klines:
        data.append({
            "ts": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "vol": float(k[7]),  # quote volume (USDT)
        })
    
    coin = symbol.replace("USDT", "")
    
    # === Exclude stablecoins and index products ===
    EXCLUDE = {"USDC", "USDP", "TUSD", "FDUSD", "BTCDOM", "DEFI", "USDM"}
    if coin in EXCLUDE:
        return None
    
    # === Exclude coins that already exploded and crashed ===
    # Compare the last 7 days with the prior average price; skip if already up >300%
    recent_7d = data[-7:]
    prior = data[:-7]
    if not prior:
        return None
    
    recent_avg_px = sum(d["close"] for d in recent_7d) / len(recent_7d)
    prior_avg_px = sum(d["close"] for d in prior) / len(prior)
    
    if prior_avg_px > 0 and ((recent_avg_px - prior_avg_px) / prior_avg_px) > 3.0:
        return None  # Already up 300%+, too late
    
    # === Find the sideways range ===
    # Search backward from the most recent data to find the longest sideways period
    # Key rule: it must be truly sideways (slope near zero); slow bleed is not sideways
    best_sideways = 0
    best_range = 0
    best_low = 0
    best_high = 0
    best_avg_vol = 0
    best_slope_pct = 0
    
    # Use a sliding window from the minimum sideways period to the full history
    for window in range(MIN_SIDEWAYS_DAYS, len(prior) + 1):
        window_data = prior[-window:]
        lows = [d["low"] for d in window_data]
        highs = [d["high"] for d in window_data]
        
        w_low = min(lows)
        w_high = max(highs)
        
        if w_low <= 0:
            continue
        
        range_pct = ((w_high - w_low) / w_low) * 100
        
        if range_pct <= MAX_RANGE_PCT:
            avg_vol = sum(d["vol"] for d in window_data) / len(window_data)
            if avg_vol <= MAX_AVG_VOL_USD:
                # Use linear regression for slope: slow bleed or vertical markup is not sideways
                closes = [d["close"] for d in window_data]
                n = len(closes)
                x_mean = (n - 1) / 2.0
                y_mean = sum(closes) / n
                num = sum((i - x_mean) * (c - y_mean) for i, c in enumerate(closes))
                den = sum((i - x_mean) ** 2 for i in range(n))
                slope = num / den if den > 0 else 0
                # Cumulative change as a percentage of the starting price
                slope_pct = (slope * n / closes[0] * 100) if closes[0] > 0 else 0
                
                # Slope filter: cumulative change beyond +/-20% is not sideways
                if abs(slope_pct) > 20:
                    continue
                
                if window > best_sideways:
                    best_sideways = window
                    best_range = range_pct
                    best_low = w_low
                    best_high = w_high
                    best_avg_vol = avg_vol
                    best_slope_pct = slope_pct
    
    if best_sideways < MIN_SIDEWAYS_DAYS:
        return None
    
    # === Compute accumulation score ===
    # Longer sideways action is better because accumulation takes time
    days_score = min(best_sideways / 90, 1.0) * 25  # Full 25 points at 90 days
    
    # Narrower range is better because price control is tighter
    range_score = max(0, (1 - best_range / MAX_RANGE_PCT)) * 20  # Narrower is better, max 20
    
    # Lower volume is better because dead volume often means supply is concentrated
    vol_score = max(0, (1 - best_avg_vol / MAX_AVG_VOL_USD)) * 20  # Lower is better, max 20
    
    # Has volume started expanding recently? A breakout in volume is an activation signal
    recent_vol = sum(d["vol"] for d in recent_7d) / len(recent_7d)
    vol_breakout = recent_vol / best_avg_vol if best_avg_vol > 0 else 0
    breakout_score = min(vol_breakout / VOL_BREAKOUT_MULT, 1.0) * 15  # Volume expansion bonus, max 15
    
    # Lower market cap usually means more upside
    # Rough market-cap estimate from current price * avg daily quote volume * multiplier
    # The notification flow later supplements this with a more realistic market cap
    est_mcap = data[-1]["close"] * best_avg_vol * 30  # Rough estimate
    if est_mcap > 0 and est_mcap < 50_000_000:
        mcap_score = 20  # Full score below $50M
    elif est_mcap < 100_000_000:
        mcap_score = 15
    elif est_mcap < 200_000_000:
        mcap_score = 10
    elif est_mcap < 500_000_000:
        mcap_score = 5
    else:
        mcap_score = 0
    
    total_score = days_score + range_score + vol_score + breakout_score + mcap_score
    
    # Flatness bonus: the closer the slope is to zero, the better
    flatness_bonus = max(0, (1 - abs(best_slope_pct) / 20)) * 5
    total_score += flatness_bonus
    
    # Status label
    if vol_breakout >= VOL_BREAKOUT_MULT:
        status = "🔥Volume Breakout"
    elif vol_breakout >= 1.5:
        status = "⚡Volume Picking Up"
    else:
        status = "💤Accumulating"
    
    return {
        "symbol": symbol,
        "coin": coin,
        "sideways_days": best_sideways,
        "range_pct": best_range,
        "slope_pct": best_slope_pct,
        "low_price": best_low,
        "high_price": best_high,
        "avg_vol": best_avg_vol,
        "current_price": data[-1]["close"],
        "recent_vol": recent_vol,
        "vol_breakout": vol_breakout,
        "score": total_score,
        "status": status,
        "data_days": len(data),
    }


def scan_accumulation_pool():
    """Scan the market and find coins that appear to be under accumulation."""
    print("📊 Scanning the full market for accumulation candidates...")
    
    symbols = get_all_perp_symbols()
    if not symbols:
        notify_data_blocked("no symbols returned from exchangeInfo")
        return []
    print(f"  Total contracts: {len(symbols)}")
    
    results = []
    
    for i, sym in enumerate(symbols):
        klines = api_get("/fapi/v1/klines", {
            "symbol": sym, "interval": "1d", "limit": 180
        })
        
        if klines and isinstance(klines, list):
            r = analyze_accumulation(sym, klines)
            if r:
                results.append(r)
        
        if (i + 1) % 10 == 0:
            time.sleep(0.5)
        if (i + 1) % 100 == 0:
            print(f"  Progress: {i+1}/{len(symbols)}... found {len(results)} so far")
    
    results.sort(key=lambda x: x["score"], reverse=True)
    print(f"  ✅ Found {len(results)} accumulation candidates")
    return results


def scan_oi_changes(watchlist_symbols):
    """Scan the watchlist for OI anomalies."""
    print(f"📊 Scanning OI anomalies ({len(watchlist_symbols)} symbols)...")
    
    alerts = []
    
    for sym in watchlist_symbols:
        # OI history
        oi_hist = api_get("/futures/data/openInterestHist", {
            "symbol": sym, "period": "1h", "limit": 3
        })
        
        if not oi_hist or len(oi_hist) < 2:
            continue
        
        prev_oi = float(oi_hist[-2]["sumOpenInterestValue"])
        curr_oi = float(oi_hist[-1]["sumOpenInterestValue"])
        
        if prev_oi <= 0 or curr_oi < MIN_OI_USD:
            continue
        
        delta_pct = ((curr_oi - prev_oi) / prev_oi) * 100
        
        if abs(delta_pct) >= MIN_OI_DELTA_PCT:
            # Get current price
            ticker = api_get("/fapi/v1/ticker/24hr", {"symbol": sym})
            if not ticker:
                continue
            
            price = float(ticker["lastPrice"])
            vol_24h = float(ticker["quoteVolume"])
            px_chg = float(ticker["priceChangePercent"])
            
            # Get funding rate
            funding = api_get("/fapi/v1/fundingRate", {"symbol": sym, "limit": 1})
            fr = float(funding[0]["fundingRate"]) if funding else 0
            
            coin = sym.replace("USDT", "")
            
            alerts.append({
                "symbol": sym,
                "coin": coin,
                "price": price,
                "oi_usd": curr_oi,
                "oi_delta_pct": delta_pct,
                "oi_delta_usd": curr_oi - prev_oi,
                "vol_24h": vol_24h,
                "px_chg_pct": px_chg,
                "funding_rate": fr,
            })
        
        time.sleep(0.3)
    
    alerts.sort(key=lambda x: abs(x["oi_delta_pct"]), reverse=True)
    print(f"  ✅ Found {len(alerts)} OI anomalies")
    return alerts


def format_usd(v):
    if v >= 1e9: return f"${v/1e9:.1f}B"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def build_pool_report(results, top_n=25):
    """Build the accumulation-pool report."""
    if not results:
        return ""
    
    now = datetime.now(timezone(timedelta(hours=8)))
    
    lines = [
        f"🏦 **Accumulation Radar** - Pool Update",
        f"⏰ {now.strftime('%Y-%m-%d %H:%M')} WIB",
        f"━━━━━━━━━━━━━━━━━━",
        f"Scanned {len(results)} contracts. Candidates found:",
        "",
    ]
    
    # Groups: breakout > warming up > still accumulating
    firing = [r for r in results if "Volume Breakout" in r["status"]]
    warming = [r for r in results if "Volume Picking Up" in r["status"]]
    sleeping = [r for r in results if "Accumulating" in r["status"]]
    
    if firing:
        lines.append(f"🔥 **Volume Breakout** ({len(firing)}) - Highest priority")
        for r in firing[:10]:
            lines.append(
                f"  🔥 **{r['coin']}** | Score:{r['score']:.0f} | "
                f"Sideways {r['sideways_days']}d | Range {r['range_pct']:.0f}% | "
                f"Volume {r['vol_breakout']:.1f}x"
            )
            lines.append(
                f"     ${r['current_price']:.6f} | "
                f"Range: ${r['low_price']:.6f}~${r['high_price']:.6f} | "
                f"Avg d-vol: {format_usd(r['avg_vol'])}"
            )
        lines.append("")
    
    if warming:
        lines.append(f"⚡ **Volume Picking Up** ({len(warming)}) - On watch")
        for r in warming[:10]:
            lines.append(
                f"  ⚡ {r['coin']} | Score:{r['score']:.0f} | "
                f"Sideways {r['sideways_days']}d | Range {r['range_pct']:.0f}% | "
                f"Vol {r['vol_breakout']:.1f}x"
            )
        lines.append("")
    
    if sleeping:
        lines.append(f"💤 **Accumulating** ({len(sleeping)}) - Keep monitoring")
        for r in sleeping[:15]:
            lines.append(
                f"  💤 {r['coin']} | Score:{r['score']:.0f} | "
                f"Sideways {r['sideways_days']}d | Range {r['range_pct']:.0f}% | "
                f"Avg d-vol: {format_usd(r['avg_vol'])}"
            )
    
    return "\n".join(lines)


def build_oi_alert_report(alerts, watchlist_coins):
    """Build the OI anomaly report for the watchlist."""
    if not alerts:
        return ""
    
    now = datetime.now(timezone(timedelta(hours=8)))
    
    # Split into in-pool vs out-of-pool
    in_pool = [a for a in alerts if a["symbol"] in watchlist_coins]
    out_pool = [a for a in alerts if a["symbol"] not in watchlist_coins]
    
    lines = [
        f"📊 **OI Anomaly Scan** [Accumulation Pool]",
        f"⏰ {now.strftime('%Y-%m-%d %H:%M')} WIB",
        f"━━━━━━━━━━━━━━━━━━",
        "",
    ]
    
    if in_pool:
        lines.append(f"🎯 **In-Pool Anomalies** ({len(in_pool)}) ⚠️ Priority watch")
        for a in in_pool[:10]:
            emoji = "🟢" if a["oi_delta_pct"] > 0 else "🔴"
            lines.append(
                f"  {emoji} **{a['coin']}** | OI: {a['oi_delta_pct']:+.1f}% "
                f"({format_usd(a['oi_usd'])}) | Price: {a['px_chg_pct']:+.1f}%"
            )
            # Signal interpretation
            if a["oi_delta_pct"] > 0 and abs(a["px_chg_pct"]) < 3:
                lines.append(f"     ⚡ Underflow! OI is rising while price is flat = position building")
            elif a["oi_delta_pct"] > 0 and a["px_chg_pct"] > 3:
                lines.append(f"     🚀 Breakout in progress! OI and price are rising together")
        lines.append("")
    
    if out_pool:
        lines.append(f"📋 Out-of-Pool Anomalies ({len(out_pool)})")
        for a in out_pool[:8]:
            emoji = "🟢" if a["oi_delta_pct"] > 0 else "🔴"
            lines.append(
                f"  {emoji} {a['coin']} | OI: {a['oi_delta_pct']:+.1f}% | "
                f"Price: {a['px_chg_pct']:+.1f}%"
            )
    
    return "\n".join(lines)


def send_telegram_plain(text):
    """Send a Telegram message as plain text (no Markdown parsing)."""
    if not TG_BOT_TOKEN:
        print("\n[TG] No token, stdout:\n")
        print(text)
        return

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 3800:
            chunks.append(current)
            current = line
        else:
            current += "\n" + line if current else line
    if current:
        chunks.append(current)

    for chunk in chunks:
        try:
            resp = requests.post(url, json={
                "chat_id": TG_CHAT_ID,
                "text": chunk,
            }, timeout=10)
            print(f"[TG] Sent plain {'✓' if resp.status_code == 200 else '✗'} ({len(chunk)} chars)")
        except Exception as e:
            print(f"[TG] Error: {e}")
        time.sleep(0.5)


def send_telegram(text):
    """Send a Telegram message."""
    if not TG_BOT_TOKEN:
        print("\n[TG] No token, stdout:\n")
        print(text)
        return
    
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    
    # Send in chunks (Telegram limit is 4096 chars)
    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 3800:
            chunks.append(current)
            current = line
        else:
            current += "\n" + line if current else line
    if current:
        chunks.append(current)
    
    for chunk in chunks:
        try:
            resp = requests.post(url, json={
                "chat_id": TG_CHAT_ID,
                "text": chunk,
                "parse_mode": "Markdown"
            }, timeout=10)
            if resp.status_code == 200:
                print(f"[TG] Sent ✓ ({len(chunk)} chars)")
            else:
                # Fall back to plain text if Markdown fails
                resp2 = requests.post(url, json={
                    "chat_id": TG_CHAT_ID,
                    "text": chunk.replace("*", "").replace("_", ""),
                }, timeout=10)
                print(f"[TG] Sent plain ({'✓' if resp2.status_code == 200 else '✗'})")
        except Exception as e:
            print(f"[TG] Error: {e}")
        time.sleep(0.5)


def save_watchlist(conn, results):
    """Save the pool to the database."""
    c = conn.cursor()
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    
    for r in results:
        c.execute("""INSERT OR REPLACE INTO watchlist 
            (symbol, coin, added_date, sideways_days, range_pct, avg_vol, 
             low_price, high_price, current_price, score, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (r["symbol"], r["coin"], now, r["sideways_days"], r["range_pct"],
             r["avg_vol"], r["low_price"], r["high_price"], r["current_price"],
             r["score"], r["status"]))
    
    conn.commit()
    print(f"  💾 Saved {len(results)} symbols to the database")


def load_watchlist_symbols(conn):
    """Load the watchlist symbols from the database."""
    c = conn.cursor()
    c.execute("SELECT symbol FROM watchlist WHERE status != 'removed'")
    return [row[0] for row in c.fetchall()]


def scan_short_fuel():
    """Strategy 2: short fuel - rising price + negative funding + high OI."""
    print("📊 Scanning short fuel (negative funding + rising coins)...")
    
    tickers = api_get("/fapi/v1/ticker/24hr")
    premiums = api_get("/fapi/v1/premiumIndex")
    
    if not tickers or not premiums:
        return [], []
    
    funding_map = {p["symbol"]: float(p["lastFundingRate"]) 
                   for p in premiums if p["symbol"].endswith("USDT")}
    
    fuel_targets = []     # Already rising + negative funding = active squeeze
    squeeze_targets = []  # Extremely negative funding + no big move yet = potential squeeze
    
    for t in tickers:
        sym = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        
        px_chg = float(t["priceChangePercent"])
        vol = float(t["quoteVolume"])
        fr = funding_map.get(sym, 0)
        coin = sym.replace("USDT", "")
        price = float(t["lastPrice"])
        
        item = {
            "coin": coin, "symbol": sym,
            "px_chg": px_chg, "funding": fr,
            "vol": vol, "price": price,
        }
        
        # Active squeeze: price >5% + negative funding + volume >$5M
        if px_chg > 5 and fr < -0.0003 and vol > 5_000_000:
            item["fuel_score"] = abs(fr) * 10000 * px_chg
            fuel_targets.append(item)
        
        # Potential squeeze: very negative funding + not up too much yet (<10%) + volume >$2M
        elif fr < -0.0005 and px_chg < 10 and vol > 2_000_000:
            item["fuel_score"] = abs(fr) * 10000
            squeeze_targets.append(item)
    
    fuel_targets.sort(key=lambda x: x["fuel_score"], reverse=True)
    squeeze_targets.sort(key=lambda x: x["fuel_score"], reverse=True)
    
    print(f"  ✅ Active squeezes: {len(fuel_targets)}, potential squeezes: {len(squeeze_targets)}")
    return fuel_targets, squeeze_targets


def build_fuel_report(fuel_targets, squeeze_targets):
    """Build the short-fuel report."""
    if not fuel_targets and not squeeze_targets:
        return ""
    
    now = datetime.now(timezone(timedelta(hours=8)))
    lines = [
        f"🔥 **Short Fuel Scan**",
        f"⏰ {now.strftime('%Y-%m-%d %H:%M')} WIB",
        f"━━━━━━━━━━━━━━━━━━",
        f"Logic: negative funding = lots of shorts, which can fuel squeezes and generate funding income",
        "",
    ]
    
    if fuel_targets:
        lines.append(f"🚀 **Active Squeezes** ({len(fuel_targets)}) - price is up and shorts are still holding")
        for t in fuel_targets[:8]:
            fr_pct = t["funding"] * 100
            flag = "🎯Extreme!" if fr_pct < -0.1 else "⚠️"
            lines.append(
                f"  {flag} **{t['coin']}** | Move {t['px_chg']:+.1f}% | "
                f"Funding 🧊{fr_pct:.4f}% | Vol {format_usd(t['vol'])}"
            )
        lines.append("")
    
    if squeeze_targets:
        lines.append(f"🎯 **Potential Squeezes** ({len(squeeze_targets)}) - deeply negative funding, not up too much yet")
        for t in squeeze_targets[:8]:
            fr_pct = t["funding"] * 100
            lines.append(
                f"  🧊 {t['coin']} | Price {t['px_chg']:+.1f}% | "
                f"Funding {fr_pct:.4f}% | Vol {format_usd(t['vol'])}"
            )
    
    return "\n".join(lines)


def save_signals(conn, chase, combined, ambush, reversal, coin_data, pool_map, now_str):
    """Save top signals from each strategy to signal_tracker for performance tracking."""
    c = conn.cursor()
    to_save = []
    seen = set()

    cutoff = (datetime.now(timezone(timedelta(hours=8))) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    c.execute("SELECT coin, signal_type FROM signal_tracker WHERE signal_time > ?", (cutoff,))
    for row in c.fetchall():
        seen.add((row[0], row[1]))

    def add_sig(coin, symbol, sig_type, price, score_val, rh=0, rl=0, n=""):
        key = (coin, sig_type)
        if key not in seen:
            to_save.append((symbol, coin, sig_type, now_str, price, rh, rl, score_val, n))
            seen.add(key)

    for s in chase[:5]:
        price = s.get("price", 0)
        pool = pool_map.get(s["sym"], {})
        add_sig(s["coin"], s["sym"], "momentum_chase", price, abs(s["fr_pct"]) * 10000,
                rh=pool.get("high_price", 0), rl=pool.get("low_price", 0),
                n=f"Funding {s['fr_pct']:.3f}% {s.get('trend','')}")

    for s in combined[:5]:
        price = s.get("price", 0)
        pool = pool_map.get(s["sym"], {})
        add_sig(s["coin"], s["sym"], "combined", price, s["total"],
                rh=pool.get("high_price", 0), rl=pool.get("low_price", 0),
                n=f"F{s['f_sc']} M{s['m_sc']} S{s['s_sc']} O{s['o_sc']}")

    for s in ambush[:5]:
        price = s.get("price", 0)
        pool = pool_map.get(s["sym"], {})
        sig_type = "underflow" if (s["d6h"] > 2 and abs(s["px_chg"]) < 5) else "ambush"
        add_sig(s["coin"], s["sym"], sig_type, price, s["total"],
                rh=pool.get("high_price", 0), rl=pool.get("low_price", 0),
                n=f"OI{s['d6h']:+.0f}% MCap {format_usd(s['est_mcap'])} Sw{s['sw_days']}d")

    for s in reversal[:5]:
        price = s.get("price", 0)
        pool = pool_map.get(s["sym"], {})
        add_sig(s["coin"], s["sym"], "reversal", price, s["rev_score"],
                rh=pool.get("high_price", 0), rl=pool.get("low_price", 0),
                n=f"OI{s['d6h']:+.0f}% Px{s['px_chg']:+.0f}% {' '.join(s['rev_tags'][:3])}")

    hot_coins = sorted([d for d in coin_data.values() if d["heat"] > 0], key=lambda x: x["heat"], reverse=True)
    for s in hot_coins[:5]:
        price = s.get("price", 0)
        pool = pool_map.get(s["sym"], {})
        n_parts = []
        if s["in_cg"]: n_parts.append("CG")
        if s["vol_surge"]: n_parts.append("Vol")
        add_sig(s["coin"], s["sym"], "heat", price, s["heat"],
                rh=pool.get("high_price", 0), rl=pool.get("low_price", 0),
                n="+".join(n_parts) if n_parts else "")

    for item in to_save:
        c.execute("""INSERT INTO signal_tracker
            (symbol, coin, signal_type, signal_time, signal_price, range_high, range_low, score, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", item)

    conn.commit()
    if to_save:
        print(f"  📝 Tracked {len(to_save)} new signals")


def check_breakouts(conn, ticker_map):
    """Check pending signals for breakout confirmation or expiry."""
    c = conn.cursor()
    now = datetime.now(timezone(timedelta(hours=8)))
    now_str = now.strftime("%Y-%m-%d %H:%M")
    cutoff_30d = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M")

    c.execute("""SELECT id, symbol, coin, signal_type, signal_time, signal_price,
                 range_high, range_low, status FROM signal_tracker
                 WHERE status = 'pending'""")

    updated = 0
    for row in c.fetchall():
        sig_id, symbol, coin, sig_type, sig_time, sig_price, rh, rl, status = row

        # Reversal signals are short setups — don't use range_high breakout for entry
        if sig_type == "reversal":
            if sig_time < cutoff_30d:
                c.execute("UPDATE signal_tracker SET status='expired', outcome_time=? WHERE id=?", (now_str, sig_id))
                updated += 1
            continue

        if sig_time < cutoff_30d:
            c.execute("UPDATE signal_tracker SET status='expired', outcome_time=? WHERE id=?", (now_str, sig_id))
            updated += 1
            continue

        if rh <= 0:
            continue

        tk = ticker_map.get(symbol, {})
        current_price = tk.get("price", 0) if isinstance(tk, dict) else 0
        if current_price <= 0:
            continue

        if current_price > rh:
            c.execute("""UPDATE signal_tracker SET status='entered', entry_price=?, entry_time=?
                         WHERE id=?""", (current_price, now_str, sig_id))
            updated += 1
            print(f"    🎯 {coin} breakout confirmed! Signal {sig_price:.6f} -> entry {current_price:.6f}")

    if updated:
        conn.commit()
        print(f"  ✅ Updated {updated} breakout signals")


def build_tracking_recap(conn):
    """Build a short performance recap for inclusion in Telegram reports."""
    c = conn.cursor()

    c.execute("SELECT status, COUNT(*) FROM signal_tracker GROUP BY status")
    counts = {row[0]: row[1] for row in c.fetchall()}

    pending = counts.get("pending", 0)
    entered = counts.get("entered", 0)
    expired = counts.get("expired", 0)

    if pending + entered == 0:
        return ""

    lines = ["", "📊 **Signal Tracking Recap**",
             f"  {pending} pending | {entered} entered | {expired} expired"]

    c.execute("""SELECT coin, signal_type, signal_price, entry_price,
                 (entry_price - signal_price) / signal_price * 100
                 FROM signal_tracker WHERE status = 'entered' ORDER BY entry_time DESC""")
    entered_signals = c.fetchall()

    if entered_signals:
        profits = [s[4] for s in entered_signals if s[4] is not None]
        if profits:
            winners = sum(1 for p in profits if p > 0)
            avg_pnl = sum(profits) / len(profits)
            lines.append(f"  In profit: {winners}/{len(profits)} | Avg +{avg_pnl:.1f}%")

            best = max(entered_signals, key=lambda x: x[4] or -999)
            worst = min(entered_signals, key=lambda x: x[4] or 999)
            lines.append(f"  🔥 Best: {best[0]} +{best[4]:.1f}% | 🔴 Worst: {worst[0]} {worst[4]:.1f}%")

    c.execute("""SELECT signal_type, COUNT(*),
                 SUM(CASE WHEN entry_price > signal_price THEN 1 ELSE 0 END)
                 FROM signal_tracker WHERE status = 'entered' GROUP BY signal_type""")
    for row in c.fetchall():
        sig_type, total, wins = row
        if total > 0:
            rate = wins / total * 100
            emoji = "🎯" if sig_type == "underflow" else "🔥" if sig_type == "momentum_chase" else "📊" if sig_type == "combined" else "🔻" if sig_type == "reversal" else "🌟"
            lines.append(f"  {emoji} {sig_type}: {wins}/{total} ({rate:.0f}%)")

    return "\n".join(lines)


def score_reversal(coin_data, pool_map, conn):
    """Score coins for short/reversal setups. Returns sorted list."""
    c = conn.cursor()
    reversal = []

    for sym, d in coin_data.items():
        score = 0
        tags = []
        rh = d.get("range_high", 0)
        rl = d.get("range_low", 0)
        price = d.get("price", 0)

        # 1. Aggressive Short Build: OI rising + price falling (40 pts max)
        if d["d6h"] > 5 and d["px_chg"] < -3:
            score += 40
            tags.append("ShortBuild")
        elif d["d6h"] > 3 and d["px_chg"] < -1:
            score += 30
            tags.append("ShortBuild")
        elif d["d6h"] > 1 and d["px_chg"] < 0:
            score += 15
            tags.append("ShortBuild")

        # 2. Long Squeeze Fuel: funding extremely positive + price stalled (25 pts max)
        if d["fr_pct"] > 0.1 and abs(d["px_chg"]) < 3:
            score += 25
            tags.append(f"LongSqueeze")
        elif d["fr_pct"] > 0.05 and abs(d["px_chg"]) < 5:
            score += 15
            tags.append(f"LongSqueeze")
        elif d["fr_pct"] > 0.03 and d["px_chg"] <= 0:
            score += 10

        # 3. Range Distribution: price near/above accumulation range high (20 pts max)
        if rh > 0 and price > 0:
            if price > rh:
                score += 20
                tags.append("RangeDist")
            elif price > rh * 0.95:
                score += 10
                tags.append("NearTop")

        # 4. Range Breakdown: price below accumulation range low (15 pts max)
        if rl > 0 and price > 0 and d["in_pool"]:
            if price < rl:
                score += 15
                tags.append("BelowRange")
            elif price < rl * 1.05:
                score += 5

        # 5. Failed Breakout bonus: was entered long, now below range high (+10 bonus)
        if rh > 0 and price > 0 and price < rh:
            c.execute("""SELECT COUNT(*) FROM signal_tracker
                         WHERE coin=? AND signal_type != 'reversal'
                         AND status='entered' AND entry_time > ?""",
                      (d["coin"], (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")))
            if c.fetchone()[0] > 0:
                score += 10
                tags.append("FailedBreakout")

        if score >= 35:
            reversal.append({**d, "rev_score": score, "rev_tags": tags})

    reversal.sort(key=lambda x: x["rev_score"], reverse=True)
    return reversal


def generate_review_report(conn):
    """Generate a review report string (for CLI or Telegram)."""
    c = conn.cursor()
    now = datetime.now(timezone(timedelta(hours=8)))
    now_str = now.strftime("%Y-%m-%d %H:%M")

    tracked_syms = set()
    c.execute("SELECT DISTINCT symbol FROM signal_tracker")
    for row in c.fetchall():
        tracked_syms.add(row[0])

    price_map = {}
    for sym in tracked_syms:
        tk = api_get("/fapi/v1/ticker/24hr", {"symbol": sym})
        if tk:
            price_map[sym] = float(tk["lastPrice"])
        time.sleep(0.1)

    # Update pending -> entered or expired
    for sig_id, sym, sig_type, rh in c.execute("SELECT id, symbol, signal_type, range_high FROM signal_tracker WHERE status='pending'").fetchall():
        sig_time_row = c.execute("SELECT signal_time FROM signal_tracker WHERE id=?", (sig_id,)).fetchone()
        if sig_time_row:
            cutoff = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M")
            if sig_time_row[0] < cutoff:
                c.execute("UPDATE signal_tracker SET status='expired', outcome_time=? WHERE id=?", (now_str, sig_id))
            elif sig_type == "reversal":
                continue  # Reversal signals don't enter on range_high breakout
            elif rh > 0 and price_map.get(sym, 0) > rh:
                c.execute("UPDATE signal_tracker SET status='entered', entry_price=?, entry_time=? WHERE id=?",
                         (price_map[sym], now_str, sig_id))
    conn.commit()

    lines = [
        f"Signal Tracker Review",
        f"{now.strftime('%Y-%m-%d %H:%M')} WIB",
        "",
    ]

    c.execute("""SELECT signal_type, COUNT(*),
                 SUM(CASE WHEN status='entered' THEN 1 ELSE 0 END),
                 SUM(CASE WHEN status='expired' THEN 1 ELSE 0 END),
                 SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END)
                 FROM signal_tracker GROUP BY signal_type""")

    for row in c.fetchall():
        sig_type, total, entered_count, expired_count, pending_count = row
        c.execute("""SELECT AVG((entry_price - signal_price) / signal_price * 100)
                     FROM signal_tracker WHERE signal_type=? AND status='entered'""", (sig_type,))
        avg_pnl = c.fetchone()[0]

        win_count = c.execute("""SELECT COUNT(*) FROM signal_tracker
                                 WHERE signal_type=? AND status='entered' AND entry_price > signal_price""",
                              (sig_type,)).fetchone()[0]

        wr = (win_count / entered_count * 100) if entered_count > 0 else 0
        pnl_str = f"avg +{avg_pnl:.1f}%" if avg_pnl and avg_pnl > 0 else f"avg {avg_pnl:.1f}%" if avg_pnl else "N/A"
        lines.append(f"  {sig_type}: {total} total | {entered_count} entered | {pending_count} pending | {expired_count} expired")
        lines.append(f"    Win rate: {win_count}/{entered_count} ({wr:.0f}%) | {pnl_str}")

    lines.append("")
    lines.append("Recent Entered (last 7 days):")
    cutoff_7d = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
    c.execute("""SELECT coin, symbol, signal_type, entry_price, entry_time
                 FROM signal_tracker WHERE status='entered' AND entry_time > ?
                 ORDER BY entry_time DESC LIMIT 15""", (cutoff_7d,))

    for row in c.fetchall():
        coin, sym, sig_type, entry_price, entry_time = row
        current_price = price_map.get(sym, entry_price)
        current_pnl = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
        # Reversal signals are short: price down = win
        if sig_type == "reversal":
            pnl_emoji = "+" if current_pnl < 0 else "-"
        else:
            pnl_emoji = "+" if current_pnl > 0 else "-"
        lines.append(f"  {pnl_emoji} {coin:<8} {sig_type:<18} | Ent: {entry_price:.6f} Now: {current_price:.6f} ({current_pnl:+.1f}%)")

    lines.append("")
    lines.append("Pending (waiting for breakout):")
    c.execute("""SELECT coin, signal_type, signal_price, range_high
                 FROM signal_tracker WHERE status='pending' ORDER BY signal_time DESC LIMIT 10""")

    for row in c.fetchall():
        coin, sig_type, sig_price, rh = row
        dist = ((rh - sig_price) / sig_price * 100) if rh > 0 and sig_price > 0 else 0
        dist_str = f"need +{dist:.1f}%" if dist > 0 else "no range"
        lines.append(f"  {coin:<8} {sig_type:<18} | Price: {sig_price:.6f} | {dist_str}")

    total_signals = c.execute("SELECT COUNT(*) FROM signal_tracker").fetchone()[0]
    total_entered = c.execute("SELECT COUNT(*) FROM signal_tracker WHERE status='entered'").fetchone()[0]
    total_wins = c.execute("SELECT COUNT(*) FROM signal_tracker WHERE status='entered' AND entry_price > signal_price").fetchone()[0]
    overall_wr = (total_wins / total_entered * 100) if total_entered > 0 else 0
    c.execute("SELECT AVG((entry_price - signal_price) / signal_price * 100) FROM signal_tracker WHERE status='entered'")
    overall_avg = c.fetchone()[0]

    lines.append("")
    lines.append(f"Overall: {total_signals} signals | {total_entered} entered | {total_wins} wins | {overall_wr:.0f}% win rate")
    if overall_avg:
        lines.append(f"Avg entry P&L: {overall_avg:+.1f}%")

    return "\n".join(lines)


# ═══════════════════════════════════════════
# Trade Journal — BTC brief, /limit, sync, /perps
# ═══════════════════════════════════════════

def compute_ema(values, period):
    """Compute EMA for a list of values."""
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def compute_rsi(closes, period=14):
    """Compute RSI from a list of closing prices."""
    if len(closes) < period + 1:
        return None
    gains, losses = 0, 0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = diff if diff > 0 else 0
        loss = -diff if diff < 0 else 0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    rs = avg_gain / avg_loss if avg_loss > 0 else float("inf")
    return 100 - (100 / (1 + rs))


def generate_btc_brief():
    """Generate BTC daily bias brief using multi-factor analysis."""
    print("📊 Generating BTC daily brief...")

    sym = "BTCUSDT"

    # Fetch 90-day daily klines
    klines = api_get("/fapi/v1/klines", {"symbol": sym, "interval": "1d", "limit": 90})
    if not klines or len(klines) < 60:
        print("  ❌ Not enough BTC kline data")
        return None

    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    vols = [float(k[7]) for k in klines]
    current_price = closes[-1]

    # EMAs
    ema21 = compute_ema(closes, 21)
    ema55 = compute_ema(closes, 55)
    ema200 = compute_ema(closes, 200) if len(closes) >= 200 else None

    # RSI 14
    rsi14 = compute_rsi(closes, 14)

    # Support / Resistance from last 20 swing highs/lows
    recent_highs = highs[-20:]
    recent_lows = lows[-20:]
    resistance = sorted(set(recent_highs), reverse=True)[:2]
    support = sorted(set(recent_lows))[:2]

    # Volume ratio (last 5 days vs 20-day avg)
    vol_5d = sum(vols[-5:]) / 5 if len(vols) >= 5 else 0
    vol_20d_avg = sum(vols[-21:-1]) / 20 if len(vols) >= 22 else vol_5d
    volume_ratio = vol_5d / vol_20d_avg if vol_20d_avg > 0 else 1

    # Funding rate
    funding_raw = api_get("/fapi/v1/fundingRate", {"symbol": sym, "limit": 4})
    funding_rate = 0
    funding_trend = "neutral"
    if funding_raw and len(funding_raw) >= 2:
        funding_rate = float(funding_raw[-1]["fundingRate"])
        prev_fr = float(funding_raw[-3]["fundingRate"]) if len(funding_raw) >= 3 else funding_rate
        if funding_rate < prev_fr - 0.0001:
            funding_trend = "negative"
        elif funding_rate > prev_fr + 0.0001:
            funding_trend = "positive"

    # OI history (24h delta)
    oi_hist = api_get("/futures/data/openInterestHist", {"symbol": sym, "period": "1h", "limit": 25})
    oi_delta_pct = 0
    if oi_hist and len(oi_hist) >= 24:
        curr_oi = float(oi_hist[-1]["sumOpenInterestValue"])
        prev_24h_oi = float(oi_hist[0]["sumOpenInterestValue"])
        if prev_24h_oi > 0:
            oi_delta_pct = ((curr_oi - prev_24h_oi) / prev_24h_oi) * 100

    # === Bias determination (7 factors, each +1 / 0 / -1) ===
    factors = 0
    signals = []

    # Factor 1: Price vs EMA21
    if ema21:
        if current_price > ema21 * 1.01:
            factors += 1
            signals.append(f"Price above EMA21 (${ema21:,.0f})")
        elif current_price < ema21 * 0.99:
            factors -= 1
            signals.append(f"Price below EMA21 (${ema21:,.0f})")
        else:
            signals.append(f"Price near EMA21 (${ema21:,.0f})")

    # Factor 2: EMA21 vs EMA55
    if ema21 and ema55:
        if ema21 > ema55 * 1.005:
            factors += 1
            signals.append(f"EMA21 > EMA55 (bullish alignment)")
        elif ema21 < ema55 * 0.995:
            factors -= 1
            signals.append(f"EMA21 < EMA55 (bearish alignment)")
        else:
            signals.append(f"EMA21 ∼ EMA55 (neutral)")

    # Factor 3: EMA55 vs EMA200
    if ema55 and ema200:
        if ema55 > ema200 * 1.01:
            factors += 1
            signals.append(f"EMA55 > EMA200 (long-term bullish)")
        elif ema55 < ema200 * 0.99:
            factors -= 1
            signals.append(f"EMA55 < EMA200 (long-term bearish)")
        else:
            signals.append(f"EMA55 ∼ EMA200 (neutral)")

    # Factor 4: RSI
    if rsi14 is not None:
        if rsi14 > 60:
            factors += 1
            signals.append(f"RSI {rsi14:.1f} (bullish momentum)")
        elif rsi14 < 40:
            factors -= 1
            signals.append(f"RSI {rsi14:.1f} (bearish momentum)")
        else:
            signals.append(f"RSI {rsi14:.1f} (neutral zone)")

    # Factor 5: Funding trend
    if funding_trend == "negative":
        factors -= 1
        signals.append(f"Funding trend negative ({funding_rate:+.4%})")
    elif funding_trend == "positive":
        factors += 1
        signals.append(f"Funding trend positive ({funding_rate:+.4%})")
    else:
        signals.append(f"Funding neutral ({funding_rate:+.4%})")

    # Factor 6: OI direction
    if oi_delta_pct > 2:
        factors += 1
        signals.append(f"OI rising {oi_delta_pct:+.1f}% (capital flowing in)")
    elif oi_delta_pct < -2:
        factors -= 1
        signals.append(f"OI declining {oi_delta_pct:+.1f}% (capital exiting)")
    else:
        signals.append(f"OI flat {oi_delta_pct:+.1f}%")

    # Factor 7: Volume
    if volume_ratio > 1.3:
        factors += 1
        signals.append(f"Volume {volume_ratio:.1f}x avg (strong participation)")
    elif volume_ratio < 0.7:
        factors -= 1
        signals.append(f"Volume {volume_ratio:.1f}x avg (low participation)")
    else:
        signals.append(f"Volume {volume_ratio:.1f}x avg (normal)")

    # Bias & confidence
    if factors >= 3:
        bias = "bullish"
    elif factors <= -3:
        bias = "bearish"
    else:
        bias = "neutral"

    abs_score = abs(factors)
    if abs_score >= 5:
        confidence = "high"
    elif abs_score >= 3:
        confidence = "medium"
    else:
        confidence = "low"

    # Summary
    ema_status = "bullish alignment" if factors > 0 and ema21 and ema55 and ema21 > ema55 else \
                 "bearish alignment" if factors < 0 and ema21 and ema55 and ema21 < ema55 else \
                 "mixed/neutral alignment"
    summary = f"BTC bias {bias.upper()}. Price ${current_price:,.0f}, {ema_status}, RSI {rsi14:.1f}, " \
              f"funding {funding_rate:+.4%} ({funding_trend}), OI {oi_delta_pct:+.1f}%."

    brief = {
        "date": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
        "bias": bias,
        "confidence": confidence,
        "price": round(current_price, 2),
        "ema21": round(ema21, 2) if ema21 else 0,
        "ema55": round(ema55, 2) if ema55 else 0,
        "ema200": round(ema200, 2) if ema200 else 0,
        "rsi14": round(rsi14, 1) if rsi14 else 0,
        "funding_rate": round(funding_rate, 6),
        "funding_trend": funding_trend,
        "oi_delta_pct": round(oi_delta_pct, 2),
        "volume_ratio": round(volume_ratio, 2),
        "support": round(support[0], 2) if support else 0,
        "resistance": round(resistance[0], 2) if resistance else 0,
        "signals": signals,
        "summary": summary,
    }

    # Save to journal
    now_wib = datetime.now(timezone(timedelta(hours=8)))
    month_str = now_wib.strftime("%Y-%m")
    journal = load_journal(month_str)
    journal["btc_briefs"].append(brief)
    save_journal(month_str, journal)

    full_brief = format_btc_brief_message(brief)
    send_telegram(full_brief)

    print(f"  ✅ BTC brief saved: {bias.upper()} ({confidence})")
    return brief


def format_btc_brief_message(brief):
    """Build Telegram message string from a BTC brief dict."""
    emoji = "🟢" if brief.get("bias") == "bullish" else "🔴" if brief.get("bias") == "bearish" else "🟡"
    confidence = brief.get("confidence", "low")
    conf_indicator = "◆◆◆" if confidence == "high" else "◆◆" if confidence == "medium" else "◆"
    price = brief.get("price", 0)
    ema21 = brief.get("ema21", 0)
    ema55 = brief.get("ema55", 0)
    ema200 = brief.get("ema200", 0)
    rsi14 = brief.get("rsi14", 0)
    funding_rate = brief.get("funding_rate", 0)
    funding_trend = brief.get("funding_trend", "neutral")
    oi_delta_pct = brief.get("oi_delta_pct", 0)
    volume_ratio = brief.get("volume_ratio", 0)
    support = brief.get("support", 0)
    resistance = brief.get("resistance", 0)
    signals = brief.get("signals", [])
    summary = brief.get("summary", "")

    lines_brief = [
        f"📊 **BTC Daily Brief** — {brief['date']} WIB",
        f"",
        f"{emoji} **{brief.get('bias', '').upper()}** ({confidence} confidence {conf_indicator})",
        f"Price: ${price:,.0f}",
        f"",
        f"📉 **Technicals**:",
        f"EMA21 ${ema21:,.0f} | EMA55 ${ema55:,.0f}" + (f" | EMA200 ${ema200:,.0f}" if ema200 else ""),
        f"RSI 14: {rsi14:.1f}",
        f"Key support: ${support:,.0f} | Resistance: ${resistance:,.0f}",
        f"",
        f"💸 **Funding & OI**:",
        f"Funding: {funding_rate:+.4%} ({funding_trend} trend)",
        f"OI 24h: {oi_delta_pct:+.1f}%",
        f"",
        f"📊 Volume: {volume_ratio:.1f}x of 20d avg",
        f"",
        f"**Signals**:",
    ]
    for sig in signals:
        lines_brief.append(f"  • {sig}")
    lines_brief.append(f"")
    lines_brief.append(f"**Summary**: {summary}")

    return "\n".join(lines_brief)


def get_btc_brief_today():
    """Get today's BTC brief. If not yet generated and past 00:30 UTC, auto-generate.
    Returns (message_string, brief_dict_or_None)."""
    now_wib = datetime.now(timezone(timedelta(hours=8)))
    today_str = now_wib.strftime("%Y-%m-%d")
    month_str = now_wib.strftime("%Y-%m")

    journal = load_journal(month_str)

    for brief in journal.get("btc_briefs", []):
        if brief.get("date") == today_str:
            return format_btc_brief_message(brief), brief

    # Not found — check if we should auto-generate
    now_utc = datetime.now(timezone.utc)
    cutoff_utc = now_utc.replace(hour=0, minute=30, second=0, microsecond=0)
    if now_utc < cutoff_utc:
        return f"📊 **BTC Daily Brief** — {today_str} WIB\n\n⏳ Brief not yet available. It runs at 00:30 UTC (08:30 WIB).\nPlease check back after that time.", None

    print(f"[BTC] No brief for {today_str}, auto-generating...")
    brief = generate_btc_brief()
    if brief:
        return format_btc_brief_message(brief), brief
    return "❌ Failed to generate BTC brief. Check logs.", None


def parse_limit_command(text):
    """Parse /limit command and return trade dict or error string.
    Format: /limit <direction> <symbol> <entry> lev <x> invalid <price> sl <price> tp1 <price> [tp2...]"""
    parts = text.strip().split()
    if len(parts) < 10:
        return "❌ Usage: /limit <long|short> <SYMBOL> <entry> lev <x> invalid <price> sl <price> tp1 <price> [tp2 <price> ...]"

    direction = parts[1].lower()
    if direction not in ("long", "short"):
        return f"❌ Invalid direction '{direction}'. Use 'long' or 'short'."

    coin = parts[2].upper()
    symbol = f"{coin}USDT"

    try:
        entry = float(parts[3])
    except ValueError:
        return f"❌ Invalid entry price: {parts[3]}"

    # Parse keyword-value pairs
    keywords = {}
    i = 4
    while i < len(parts):
        kw = parts[i].lower()
        if kw in ("lev", "invalid", "sl"):
            if i + 1 >= len(parts):
                return f"❌ Missing value for '{kw}'"
            keywords[kw] = parts[i + 1]
            i += 2
        elif kw.startswith("tp"):
            if i + 1 >= len(parts):
                return f"❌ Missing value for '{kw}'"
            keywords[kw] = parts[i + 1]
            i += 2
        else:
            return f"❌ Unknown keyword: {kw}. Use: lev, invalid, sl, tp1, tp2, ..."

    # Validate required fields
    if "lev" not in keywords:
        return "❌ Missing 'lev' (leverage)."
    if "invalid" not in keywords:
        return "❌ Missing 'invalid' price."
    if "sl" not in keywords:
        return "❌ Missing 'sl' (stop loss) price."

    try:
        lev = int(float(keywords["lev"]))
        invalid_price = float(keywords["invalid"])
        sl_price = float(keywords["sl"])
    except ValueError:
        return "❌ Invalid numeric value for lev, invalid, or sl."
    if lev <= 0:
        return "❌ Leverage must be a positive integer."

    # Parse TPs
    targets = []
    tp_keys = sorted([k for k in keywords if k.startswith("tp")], key=lambda x: int(x[2:]))
    for k in tp_keys:
        try:
            targets.append(float(keywords[k]))
        except ValueError:
            return f"❌ Invalid price for {k}."

    if not targets:
        return "❌ At least one tp1 is required."

    # Validate direction logic
    if direction == "short":
        if entry <= 0:
            return "❌ Entry price must be positive."
        if sl_price <= entry:
            return f"❌ For SHORT: SL ({sl_price}) must be above entry ({entry})."
        if invalid_price == entry:
            return f"❌ Invalid price cannot equal entry price ({entry})."
        if not (targets[0] <= invalid_price <= sl_price):
            return f"❌ For SHORT: invalid ({invalid_price}) must be between TP1 ({targets[0]}) and SL ({sl_price})."
        for tp in targets:
            if tp >= entry:
                return f"❌ For SHORT: TP ({tp}) must be below entry ({entry})."
        # TPs should be sorted descending (closest to farthest)
        if targets != sorted(targets, reverse=True):
            return "❌ For SHORT: TP prices should be ordered from highest to lowest (tp1 closest to entry)."
    else:  # long
        if entry <= 0:
            return "❌ Entry price must be positive."
        if sl_price >= entry:
            return f"❌ For LONG: SL ({sl_price}) must be below entry ({entry})."
        if invalid_price == entry:
            return f"❌ Invalid price cannot equal entry price ({entry})."
        if not (sl_price <= invalid_price <= targets[0]):
            return f"❌ For LONG: invalid ({invalid_price}) must be between SL ({sl_price}) and TP1 ({targets[0]})."
        for tp in targets:
            if tp <= entry:
                return f"❌ For LONG: TP ({tp}) must be above entry ({entry})."
        # TPs should be sorted ascending (closest to farthest)
        if targets != sorted(targets):
            return "❌ For LONG: TP prices should be ordered from lowest to highest (tp1 closest to entry)."

    # Calculate risk
    risk_r = abs(entry - sl_price)

    trade = {
        "id": "",
        "created_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "direction": direction,
        "symbol": symbol,
        "coin": coin,
        "entry": entry,
        "lev": lev,
        "invalid": invalid_price,
        "sl": sl_price,
        "targets": targets,
        "status": "pending",
        "risk_r": round(risk_r, 2),
        "tp_status": [{"price": tp, "hit": False, "hit_at": None} for tp in targets],
        "sl_hit": False,
        "sl_hit_at": None,
        "entry_filled": False,
        "entry_filled_at": None,
        "invalidated": False,
        "invalidated_at": None,
        "all_tps_hit": False,
        "last_sync": None,
        "notes": "",
    }

    # Generate ID
    now_wib = datetime.now(timezone(timedelta(hours=8)))
    trade["id"] = f"{now_wib.strftime('%m-%d')}-{coin}-{int(entry)}"

    return trade


def parse_position_command(text):
    """Parse /position command and return trade dict or error string.
    Format: /position <long|short> <SYMBOL> <entry> lev <x> sl <price> tp1 <price> [tp2...]"""
    parts = text.strip().split()
    if len(parts) < 9:
        return "❌ Usage: /position <long|short> <SYMBOL> <entry> lev <x> sl <price> tp1 <price> [tp2 <price> ...]"

    direction = parts[1].lower()
    if direction not in ("long", "short"):
        return f"❌ Invalid direction '{direction}'. Use 'long' or 'short'."

    coin = parts[2].upper()
    symbol = f"{coin}USDT"

    try:
        entry = float(parts[3])
    except ValueError:
        return f"❌ Invalid entry price: {parts[3]}"

    # Parse keyword-value pairs
    keywords = {}
    i = 4
    while i < len(parts):
        kw = parts[i].lower()
        if kw in ("lev", "sl"):
            if i + 1 >= len(parts):
                return f"❌ Missing value for '{kw}'"
            keywords[kw] = parts[i + 1]
            i += 2
        elif kw.startswith("tp"):
            if i + 1 >= len(parts):
                return f"❌ Missing value for '{kw}'"
            keywords[kw] = parts[i + 1]
            i += 2
        elif kw == "invalid":
            return "❌ /position does not use 'invalid'. For limit orders use /limit."
        else:
            return f"❌ Unknown keyword: {kw}. Use: lev, sl, tp1, tp2, ..."

    if "lev" not in keywords:
        return "❌ Missing 'lev' (leverage)."
    if "sl" not in keywords:
        return "❌ Missing 'sl' (stop loss) price."

    try:
        lev = int(float(keywords["lev"]))
        sl_price = float(keywords["sl"])
    except ValueError:
        return "❌ Invalid numeric value for lev or sl."
    if lev <= 0:
        return "❌ Leverage must be a positive integer."

    targets = []
    tp_keys = sorted([k for k in keywords if k.startswith("tp")], key=lambda x: int(x[2:]))
    for k in tp_keys:
        try:
            targets.append(float(keywords[k]))
        except ValueError:
            return f"❌ Invalid price for {k}."

    if not targets:
        return "❌ At least one tp1 is required."

    # Direction validation
    if direction == "short":
        if entry <= 0:
            return "❌ Entry price must be positive."
        if sl_price <= entry:
            return f"❌ For SHORT: SL ({sl_price}) must be above entry ({entry})."
        for tp in targets:
            if tp >= entry:
                return f"❌ For SHORT: TP ({tp}) must be below entry ({entry})."
        if targets != sorted(targets, reverse=True):
            return "❌ For SHORT: TP prices should be ordered from highest to lowest (tp1 closest to entry)."
    else:
        if entry <= 0:
            return "❌ Entry price must be positive."
        if sl_price >= entry:
            return f"❌ For LONG: SL ({sl_price}) must be below entry ({entry})."
        for tp in targets:
            if tp <= entry:
                return f"❌ For LONG: TP ({tp}) must be above entry ({entry})."
        if targets != sorted(targets):
            return "❌ For LONG: TP prices should be ordered from lowest to highest (tp1 closest to entry)."

    now_wib = datetime.now(timezone(timedelta(hours=8)))
    now_str = now_wib.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    risk_r = abs(entry - sl_price)

    trade = {
        "id": f"{now_wib.strftime('%m-%d')}-{coin}-{int(entry)}",
        "created_at": now_str,
        "direction": direction,
        "symbol": symbol,
        "coin": coin,
        "entry": entry,
        "lev": lev,
        "invalid": None,
        "sl": sl_price,
        "targets": targets,
        "status": "active",
        "risk_r": round(risk_r, 2),
        "tp_status": [{"price": tp, "hit": False, "hit_at": None} for tp in targets],
        "sl_hit": False,
        "sl_hit_at": None,
        "entry_filled": True,
        "entry_filled_at": now_str,
        "invalidated": False,
        "invalidated_at": None,
        "all_tps_hit": False,
        "last_sync": now_str,
        "notes": "",
    }

    return trade


def process_position_command(text):
    """Process /position command: parse, check current price, save to journal.
    Returns (reply_message, trade_or_None)."""
    result = parse_position_command(text)
    if isinstance(result, str):
        return result, None

    trade = result
    direction = trade["direction"]
    symbol = trade["symbol"]

    # Fetch current price
    ticker = api_get("/fapi/v1/ticker/price", {"symbol": symbol})
    if not ticker or "price" not in ticker:
        return "⚠️ Could not fetch current price. Trade saved but price not verified.", trade

    current_price = float(ticker["price"])
    events = []

    # Check SL hit
    if direction == "short":
        sl_hit = current_price >= trade["sl"]
    else:
        sl_hit = current_price <= trade["sl"]

    if sl_hit:
        trade["sl_hit"] = True
        trade["sl_hit_at"] = trade["created_at"]
        trade["status"] = "stopped_out"
        events.append(f"💀 SL already hit at {trade['sl']} (current: {current_price})")

    # Check TPs hit
    if not trade["sl_hit"]:
        for i, tp in enumerate(trade["tp_status"]):
            if direction == "short":
                tp_hit = current_price <= tp["price"]
            else:
                tp_hit = current_price >= tp["price"]
            if tp_hit:
                tp["hit"] = True
                tp["hit_at"] = trade["created_at"]
                r_achieved = abs(tp["price"] - trade["entry"]) / trade["risk_r"] if trade["risk_r"] > 0 else 0
                events.append(f"🎯 TP{i+1} ({tp['price']}) already hit — {r_achieved:.1f}R")

        if all(tp["hit"] for tp in trade["tp_status"]):
            trade["all_tps_hit"] = True
            trade["status"] = "completed"
            if not any("TP" in e for e in events):
                events.append("✅ All TPs already hit")

    # Save to journal
    add_trade_to_journal(trade)

    # Build reply
    direction_emoji = "🔴 SHORT" if direction == "short" else "🟢 LONG"
    status_emoji = {"active": "📈", "stopped_out": "💀", "completed": "🏆"}.get(trade["status"], "")
    tps_lines = []
    for i, tp in enumerate(trade["tp_status"]):
        mark = " ✅" if tp["hit"] else ""
        r_val = abs(tp["price"] - trade["entry"]) / trade["risk_r"] if trade["risk_r"] > 0 else 0
        tps_lines.append(f"TP{i+1}: {tp['price']} ({r_val:.1f}R){mark}")
    reply = (
        f"{status_emoji} Position saved: {direction_emoji} {trade['coin']} @ {trade['entry']} (Lev {trade.get('lev', 1)}x)\n"
        f"SL: {trade['sl']} | Current: {current_price}\n"
        + "\n".join(tps_lines)
    )
    if events:
        reply += "\n\n" + "\n".join(events)

    return reply, trade


def add_trade_to_journal(trade):
    """Add a trade entry to the current month's journal."""
    now_wib = datetime.now(timezone(timedelta(hours=8)))
    month_str = now_wib.strftime("%Y-%m")
    journal = load_journal(month_str)
    journal["trades"].append(trade)
    save_journal(month_str, journal)
    return True


def remove_pending_limit_trade_from_journal(trade_id, month_str=None):
    """Remove a pending /limit trade from the current month's journal by trade id."""
    if month_str is None:
        now_wib = datetime.now(timezone(timedelta(hours=8)))
        month_str = now_wib.strftime("%Y-%m")

    journal = load_journal(month_str)
    trades = journal.get("trades", [])

    kept = []
    removed = []
    for t in trades:
        if t.get("id") != trade_id:
            kept.append(t)
            continue

        is_limit = t.get("invalid") is not None
        is_pending = t.get("status") == "pending" and not t.get("entry_filled", False)
        if is_limit and is_pending:
            removed.append(t)
        else:
            kept.append(t)

    if not removed:
        return False, "❌ Tidak bisa delete: id tidak ditemukan, atau setup bukan pending /limit."

    journal["trades"] = kept
    save_journal(month_str, journal)
    return True, f"🗑️ Deleted {len(removed)} pending /limit setup: {trade_id}"


def parse_spot_command(text):
    """Parse /spot command and return trade dict or error string.
    Format: /spot <long> <SYMBOL> <entry> sl <price> tp1 <price> [tp2...]"""
    parts = text.strip().split()
    if len(parts) < 7:
        return "❌ Usage: /spot <long> <SYMBOL> <entry> sl <price> tp1 <price> [tp2 <price> ...]"

    direction = parts[1].lower()
    if direction not in ("long", "buy"):
        return "❌ Spot hanya mendukung LONG/BUY."

    coin = parts[2].upper()

    try:
        entry = float(parts[3])
    except ValueError:
        return f"❌ Invalid entry price: {parts[3]}"

    keywords = {}
    i = 4
    while i < len(parts):
        kw = parts[i].lower()
        if kw == "sl":
            if i + 1 >= len(parts):
                return f"❌ Missing value for '{kw}'"
            keywords[kw] = parts[i + 1]
            i += 2
        elif kw.startswith("tp"):
            if i + 1 >= len(parts):
                return f"❌ Missing value for '{kw}'"
            keywords[kw] = parts[i + 1]
            i += 2
        elif kw == "lev":
            return "❌ Spot tidak memakai leverage (hapus 'lev')."
        else:
            return f"❌ Unknown keyword: {kw}. Use: sl, tp1, tp2, ..."

    if "sl" not in keywords:
        return "❌ Missing 'sl' (stop loss) price."

    try:
        sl_price = float(keywords["sl"])
    except ValueError:
        return "❌ Invalid numeric value for sl."

    targets = []
    tp_keys = sorted([k for k in keywords if k.startswith("tp")], key=lambda x: int(x[2:]))
    for k in tp_keys:
        try:
            targets.append(float(keywords[k]))
        except ValueError:
            return f"❌ Invalid price for {k}."

    if not targets:
        return "❌ At least one tp1 is required."

    if entry <= 0:
        return "❌ Entry price must be positive."
    if sl_price >= entry:
        return f"❌ For SPOT LONG: SL ({sl_price}) must be below entry ({entry})."
    for tp in targets:
        if tp <= entry:
            return f"❌ For SPOT LONG: TP ({tp}) must be above entry ({entry})."
    if targets != sorted(targets):
        return "❌ For SPOT LONG: TP prices should be ordered from lowest to highest (tp1 closest to entry)."

    now_wib = datetime.now(timezone(timedelta(hours=8)))
    now_str = now_wib.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    risk_r = abs(entry - sl_price)
    trade = {
        "id": f"{now_wib.strftime('%m-%d')}-{coin}-{int(entry)}",
        "created_at": now_str,
        "market": "spot",
        "direction": "long",
        "coin": coin,
        "entry": entry,
        "sl": sl_price,
        "targets": targets,
        "status": "active",
        "risk_r": round(risk_r, 2),
        "tp_status": [{"price": tp, "hit": False, "hit_at": None} for tp in targets],
        "sl_hit": False,
        "sl_hit_at": None,
        "notes": "",
    }
    return trade


def add_spot_trade_to_journal(trade):
    """Add a spot trade entry to the current month's spot journal."""
    now_wib = datetime.now(timezone(timedelta(hours=8)))
    month_str = now_wib.strftime("%Y-%m")
    journal = load_spot_journal(month_str)
    journal["trades"].append(trade)
    save_spot_journal(month_str, journal)
    return True


def generate_spot_stats(month_str=None):
    """Generate monthly spot trade statistics."""
    if month_str is None:
        now_wib = datetime.now(timezone(timedelta(hours=8)))
        month_str = now_wib.strftime("%Y-%m")

    journal = load_spot_journal(month_str)
    trades = journal.get("trades", [])
    if not trades:
        return "🟩 **Spot Journal** — {}\n\nNo trades recorded this month.".format(month_str)

    total = len(trades)
    status_counts = {}
    for t in trades:
        s = t.get("status", "active")
        status_counts[s] = status_counts.get(s, 0) + 1

    resolved = [t for t in trades if t.get("status") in ("completed", "stopped_out")]
    winners = [t for t in resolved if t["status"] == "completed"]
    win_rate = (len(winners) / len(resolved) * 100) if resolved else 0

    rr_values = []
    roi_values = []
    for t in resolved:
        if t.get("status") == "completed":
            best_tp = None
            for tp in t["tp_status"]:
                if tp["hit"]:
                    best_tp = tp["price"]
            if best_tp and t.get("risk_r", 0) > 0:
                r_val = (best_tp - t["entry"]) / t["risk_r"]
                roi = (best_tp - t["entry"]) / t["entry"] * 100
                rr_values.append(r_val)
                roi_values.append(roi)
        else:
            rr_values.append(-1.0)
            roi_values.append(-(t.get("entry", 0) - t.get("sl", t.get("entry", 0) - 1)) / t.get("entry", 1) * 100)

    avg_rr = sum(rr_values) / len(rr_values) if rr_values else 0
    avg_roi = sum(roi_values) / len(roi_values) if roi_values else 0
    best_rr = max(rr_values) if rr_values else 0
    worst_rr = min(rr_values) if rr_values else 0

    best_trade = ""
    worst_trade = ""
    if rr_values:
        best_idx = rr_values.index(best_rr)
        worst_idx = rr_values.index(worst_rr)
        bcoin = resolved[best_idx].get("coin", "??")
        best_trade = f"{bcoin} +{best_rr:.1f}R (+{roi_values[best_idx]:.1f}%)"
        wcoin = resolved[worst_idx].get("coin", "??")
        worst_trade = f"{wcoin} {worst_rr:.1f}R ({roi_values[worst_idx]:.1f}%)"

    active = status_counts.get("active", 0)
    completed = status_counts.get("completed", 0)
    stopped = status_counts.get("stopped_out", 0)

    lines = [
        f"🟩 **Spot Journal** — {month_str}",
        f"",
        f"Trades: {total} | ✅ Complete: {completed} | ❌ Stopped: {stopped}",
        f"📌 Active: {active}",
        f"",
    ]
    if resolved:
        lines.append(f"Win Rate: {win_rate:.1f}% ({completed}/{len(resolved)} resolved)")
        lines.append(f"Avg R:R: {avg_rr:+.1f}R | Avg ROI: {avg_roi:+.1f}%")
        lines.append(f"")
        lines.append(f"🏆 Best: {best_trade}")
        lines.append(f"💀 Worst: {worst_trade}")

    return "\n".join(lines)


def check_level_crossing(trade, klines):
    """Check if price crossed any key levels using 1h candle data.
    Returns updated trade dict and list of events."""
    if not klines or len(klines) < 2:
        return trade, []

    direction = trade["direction"]
    events = []

    for k in klines:
        high = float(k[2])
        low = float(k[3])
        kline_ts = k[0]

        # For SHORT: entry/invalid/SL are ABOVE, TPs are BELOW
        # Entry/SL/Invalid crossed when high >= level
        # TP crossed when low <= level
        if direction == "short":
            cross_up = lambda level: high >= level
            cross_down = lambda level: low <= level
        else:
            # For LONG: entry/invalid/SL are BELOW, TPs are ABOVE
            # Entry/SL/Invalid crossed when low <= level
            # TP crossed when high >= level
            cross_up = lambda level: high >= level
            cross_down = lambda level: low <= level

        if direction == "short":
            crosses_entry = cross_up(trade["entry"])
            crosses_invalid = trade["invalid"] is not None and cross_up(trade["invalid"])
            crosses_sl = cross_up(trade["sl"])
        else:
            crosses_entry = cross_down(trade["entry"])
            crosses_invalid = trade["invalid"] is not None and cross_down(trade["invalid"])
            crosses_sl = cross_down(trade["sl"])

        # Priority: invalid > SL > entry > TPs
        if not trade["invalidated"] and not trade["entry_filled"] and crosses_invalid:
            trade["invalidated"] = True
            trade["invalidated_at"] = datetime.fromtimestamp(kline_ts / 1000, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")
            trade["status"] = "invalidated"
            events.append(f"🚫 {trade['coin']} {trade['direction'].upper()}: Invalidated at {trade['invalid']}")
            break

        if not trade["entry_filled"] and crosses_entry:
            trade["entry_filled"] = True
            trade["entry_filled_at"] = datetime.fromtimestamp(kline_ts / 1000, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")
            trade["status"] = "active"
            events.append(f"✅ {trade['coin']} {trade['direction'].upper()}: Entry filled at {trade['entry']}")

        if trade["entry_filled"] and not trade["sl_hit"] and crosses_sl:
            trade["sl_hit"] = True
            trade["sl_hit_at"] = datetime.fromtimestamp(kline_ts / 1000, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")
            trade["status"] = "stopped_out"
            events.append(f"💀 {trade['coin']} {trade['direction'].upper()}: Stopped out at {trade['sl']}")
            break

        if trade["entry_filled"]:
            for i, tp in enumerate(trade["tp_status"]):
                if not tp["hit"]:
                    if direction == "short":
                        hit = cross_down(tp["price"])
                    else:
                        hit = cross_up(tp["price"])
                    if hit:
                        tp["hit"] = True
                        tp["hit_at"] = datetime.fromtimestamp(kline_ts / 1000, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")
                        r_achieved = abs(tp["price"] - trade["entry"]) / trade["risk_r"] if trade["risk_r"] > 0 else 0
                        events.append(f"🎯 {trade['coin']} TP{i+1} ({tp['price']}) hit — {r_achieved:.1f}R")

            # Check if all TPs hit
            if all(tp["hit"] for tp in trade["tp_status"]):
                trade["all_tps_hit"] = True
                trade["status"] = "completed"
                break

        # Update after each candle
        trade["last_sync"] = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")

    return trade, events


def sync_trades():
    """Sync all active trades with current price conditions (every 12h)."""
    print("📊 Syncing trade journal...")

    now_wib = datetime.now(timezone(timedelta(hours=8)))
    month_str = now_wib.strftime("%Y-%m")
    journal = load_journal(month_str)

    if not journal.get("trades"):
        print("  No trades to sync.")
        return

    all_events = []
    updated_indices = []

    for idx, trade in enumerate(journal["trades"]):
        # Skip terminal trades
        if trade["status"] in ("completed", "stopped_out", "invalidated"):
            continue

        sym = trade["symbol"]
        # Fetch 1h klines for last 12 hours
        klines = api_get("/fapi/v1/klines", {"symbol": sym, "interval": "1h", "limit": 13})
        if not klines:
            print(f"  ⚠️ No kline data for {sym}, skipping")
            continue

        updated_trade, events = check_level_crossing(trade, klines)
        if events:
            journal["trades"][idx] = updated_trade
            updated_indices.append(idx)
            all_events.extend(events)

        time.sleep(0.1)

    if updated_indices:
        save_journal(month_str, journal)
        print(f"  ✅ Updated {len(updated_indices)} trades: {len(all_events)} events")

        # Send Telegram report
        lines = [
            f"📊 **Trade Sync Report** — {now_wib.strftime('%Y-%m-%d %H:%M')} WIB",
            f"",
        ]
        for event in all_events:
            lines.append(f"  {event}")

        # Summary of active trades
        active_trades = [t for t in journal["trades"] if t["status"] in ("pending", "active")]
        if active_trades:
            lines.append(f"")
            lines.append(f"**Active Trades** ({len(active_trades)}):")
            for t in active_trades:
                tps_hit = sum(1 for tp in t["tp_status"] if tp["hit"])
                tps_total = len(t["tp_status"])
                lines.append(
                    f"  {t['coin']} {t['direction'].upper()} | "
                    f"Entry: {t['entry']} | SL: {t['sl']} | Status: {t['status']} | "
                    f"TPs: {tps_hit}/{tps_total}"
                )

        send_telegram("\n".join(lines))
    else:
        print("  No trade status changes.")


def generate_perps_stats(month_str=None):
    """Generate monthly perps trade statistics."""
    if month_str is None:
        now_wib = datetime.now(timezone(timedelta(hours=8)))
        month_str = now_wib.strftime("%Y-%m")

    journal = load_journal(month_str)
    trades = journal.get("trades", [])

    if not trades:
        return "📈 Perps Journal — {}\n\nNo trades recorded this month.".format(month_str)

    total = len(trades)
    status_counts = {}
    for t in trades:
        s = t.get("status", "pending")
        status_counts[s] = status_counts.get(s, 0) + 1

    # Resolved trades (completed or stopped_out, not pending/invalidated)
    resolved = [t for t in trades if t.get("status") in ("completed", "stopped_out")]
    winners = [t for t in resolved if t["status"] == "completed"]
    losers = [t for t in resolved if t["status"] == "stopped_out"]

    win_rate = (len(winners) / len(resolved) * 100) if resolved else 0

    # Calculate R:R and ROI for resolved trades
    rr_values = []
    roi_values = []
    for t in resolved:
        lev = int(t.get("lev", 1) or 1)
        if lev <= 0:
            lev = 1
        if t.get("status") == "completed":
            # Best TP hit
            best_tp = None
            for tp in t["tp_status"]:
                if tp["hit"]:
                    best_tp = tp["price"]
            if best_tp and t.get("risk_r", 0) > 0:
                if t.get("direction") == "short":
                    r_val = (t["entry"] - best_tp) / t["risk_r"]
                    roi = (t["entry"] - best_tp) / t["entry"] * 100 * lev
                else:
                    r_val = (best_tp - t["entry"]) / t["risk_r"]
                    roi = (best_tp - t["entry"]) / t["entry"] * 100 * lev
                rr_values.append(r_val)
                roi_values.append(roi)
        else:  # stopped_out
            rr_values.append(-1.0)
            if t.get("direction") == "short":
                roi_values.append(-(t.get("sl", t.get("entry", 0) + 1) - t.get("entry", 0)) / t.get("entry", 1) * 100 * lev)
            else:
                roi_values.append(-(t.get("entry", 0) - t.get("sl", t.get("entry", 0) - 1)) / t.get("entry", 1) * 100 * lev)

    avg_rr = sum(rr_values) / len(rr_values) if rr_values else 0
    avg_roi = sum(roi_values) / len(roi_values) if roi_values else 0
    best_rr = max(rr_values) if rr_values else 0
    worst_rr = min(rr_values) if rr_values else 0

    # Find best and worst trade names
    best_trade = ""
    worst_trade = ""
    if rr_values:
        best_idx = rr_values.index(best_rr)
        worst_idx = rr_values.index(worst_rr)
        bcoin = resolved[best_idx].get("coin", "??")
        bdir = resolved[best_idx].get("direction", "??").upper()
        best_trade = f"{bcoin} {bdir} +{best_rr:.1f}R (+{roi_values[best_idx]:.1f}%)"
        wcoin = resolved[worst_idx].get("coin", "??")
        wdir = resolved[worst_idx].get("direction", "??").upper()
        worst_trade = f"{wcoin} {wdir} {worst_rr:.1f}R ({roi_values[worst_idx]:.1f}%)"

    # Long vs Short breakdown
    longs = [t for t in trades if t.get("direction") == "long"]
    shorts = [t for t in trades if t.get("direction") == "short"]
    long_w = sum(1 for t in longs if t.get("status") == "completed")
    long_l = sum(1 for t in longs if t.get("status") == "stopped_out")
    long_p = sum(1 for t in longs if t.get("status") == "pending")
    long_a = sum(1 for t in longs if t.get("status") == "active")
    long_i = sum(1 for t in longs if t.get("status") == "invalidated")
    short_w = sum(1 for t in shorts if t.get("status") == "completed")
    short_l = sum(1 for t in shorts if t.get("status") == "stopped_out")
    short_p = sum(1 for t in shorts if t.get("status") == "pending")
    short_a = sum(1 for t in shorts if t.get("status") == "active")
    short_i = sum(1 for t in shorts if t.get("status") == "invalidated")

    pending = status_counts.get("pending", 0)
    active = status_counts.get("active", 0)
    completed = status_counts.get("completed", 0)
    stopped = status_counts.get("stopped_out", 0)
    invalid = status_counts.get("invalidated", 0)

    lines = [
        f"📈 **Perps Journal** — {month_str}",
        f"",
        f"Trades: {total} | ✅ Complete: {completed} | ❌ Stopped: {stopped}",
        f"⏳ Active: {active} | 🕐 Pending: {pending} | 🚫 Invalid: {invalid}",
        f"",
    ]
    if resolved:
        lines.append(f"Win Rate: {win_rate:.1f}% ({completed}/{len(resolved)} resolved)")
        lines.append(f"Avg R:R: {avg_rr:+.1f}R | Avg ROI: {avg_roi:+.1f}%")
        lines.append(f"")
        lines.append(f"🏆 Best: {best_trade}")
        lines.append(f"💀 Worst: {worst_trade}")
        lines.append(f"")
    if longs:
        long_detail = f"{len(longs)} ({long_w}W/"
        parts_l = []
        if long_l: parts_l.append(f"{long_l}L")
        if long_a: parts_l.append(f"{long_a}A")
        if long_p: parts_l.append(f"{long_p}P")
        if long_i: parts_l.append(f"{long_i}I")
        long_detail += "/".join(parts_l) + ")"
        lines.append(f"Long: {long_detail}")
    if shorts:
        short_detail = f"{len(shorts)} ({short_w}W/"
        parts_s = []
        if short_l: parts_s.append(f"{short_l}L")
        if short_a: parts_s.append(f"{short_a}A")
        if short_p: parts_s.append(f"{short_p}P")
        if short_i: parts_s.append(f"{short_i}I")
        short_detail += "/".join(parts_s) + ")"
        lines.append(f"Short: {short_detail}")

    pending_limit_setups = [
        t for t in trades
        if t.get("status") == "pending"
        and not t.get("entry_filled", False)
        and t.get("invalid") is not None
    ]
    if pending_limit_setups:
        lines.append(f"")
        lines.append(f"**Pending /limit (deletable)** — {len(pending_limit_setups)}")
        max_list = 30
        for t in pending_limit_setups[:max_list]:
            lev = int(t.get("lev", 1) or 1)
            if lev <= 0:
                lev = 1
            lines.append(
                f"  {t.get('coin','??')} {t.get('direction','??').upper()} | "
                f"Entry {t.get('entry','?')} | Lev {lev}x | "
                f"ID: {t.get('id','')}"
            )
        if len(pending_limit_setups) > max_list:
            lines.append(f"  ... +{len(pending_limit_setups) - max_list} more")

    return "\n".join(lines)


LAST_TG_UPDATE_ID = 0


def check_telegram_commands(conn):
    """Check Telegram for pending /review commands (passive, called after oi scan)."""
    global LAST_TG_UPDATE_ID
    if not TG_BOT_TOKEN:
        return

    try:
        stored = get_app_state(conn, "last_tg_update_id", "0")
        try:
            stored_id = int(stored or 0)
        except Exception:
            stored_id = 0
        if stored_id > LAST_TG_UPDATE_ID:
            LAST_TG_UPDATE_ID = stored_id

        params = {"timeout": 2}
        if LAST_TG_UPDATE_ID > 0:
            params["offset"] = LAST_TG_UPDATE_ID + 1

        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates"
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code != 200:
            print(f"[TG] getUpdates returned HTTP {resp.status_code}: {(resp.text or '')[:200]}")
            return

        data = resp.json()
        if not data.get("ok"):
            print(f"[TG] getUpdates not ok: {data}")
            return

        updates = data.get("result", [])
        print(f"[TG] getUpdates: {len(updates)} pending updates")

        review_sent = False
        for update in updates:
            if "message" not in update:
                LAST_TG_UPDATE_ID = update["update_id"]
                continue

            msg = update["message"]
            text = msg.get("text", "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))

            if chat_id != TG_CHAT_ID:
                LAST_TG_UPDATE_ID = update["update_id"]
                continue

            if text.startswith("/limit"):
                print(f"[TG] /limit received: {text[:80]}")
                result = parse_limit_command(text)
                if isinstance(result, str):
                    # Error message
                    send_telegram_plain(result)
                    print(f"[TG] /limit error: {result[:80]}")
                else:
                    add_trade_to_journal(result)
                    # Build confirmation
                    direction_emoji = "🔴 SHORT" if result["direction"] == "short" else "🟢 LONG"
                    tps_lines = []
                    for i, tp in enumerate(result["tp_status"]):
                        r_val = abs(tp["price"] - result["entry"]) / result["risk_r"] if result["risk_r"] > 0 else 0
                        tps_lines.append(f"TP{i+1}: {tp['price']} ({r_val:.1f}R)")
                    reply = (
                        f"✅ Trade saved: {direction_emoji} {result['coin']} @ {result['entry']} (Lev {result.get('lev', 1)}x)\n"
                        f"SL: {result['sl']} | Invalid: {result['invalid']}\n"
                        + "\n".join(tps_lines)
                    )
                    reply += f"\n\nID: {result['id']}"
                    send_telegram_plain(reply)
                    print(f"[TG] Trade saved: {result['id']}")
                LAST_TG_UPDATE_ID = update["update_id"]
            elif text.startswith("/position"):
                print(f"[TG] /position received: {text[:80]}")
                reply, _ = process_position_command(text)
                send_telegram_plain(reply)
                print(f"[TG] /position processed")
                LAST_TG_UPDATE_ID = update["update_id"]
            elif text.startswith("/delete") or text.startswith("/del"):
                parts = text.strip().split()
                if len(parts) < 2:
                    send_telegram_plain("❌ Usage: /delete <trade_id>\nExample: /delete 05-15-BTC-81000")
                else:
                    _, msg = remove_pending_limit_trade_from_journal(parts[1])
                    send_telegram_plain(msg)
                LAST_TG_UPDATE_ID = update["update_id"]
            elif text == "/spot":
                try:
                    stats_text = generate_spot_stats()
                    send_telegram_plain(stats_text)
                except Exception as e:
                    print(f"[TG] Spot stats failed: {e}")
                    send_telegram_plain("Error generating spot stats. Check logs.")
                LAST_TG_UPDATE_ID = update["update_id"]
            elif text.startswith("/spot"):
                result = parse_spot_command(text)
                if isinstance(result, str):
                    send_telegram_plain(result)
                else:
                    add_spot_trade_to_journal(result)
                    tps_lines = []
                    for i, tp in enumerate(result["tp_status"]):
                        r_val = abs(tp["price"] - result["entry"]) / result["risk_r"] if result["risk_r"] > 0 else 0
                        tps_lines.append(f"TP{i+1}: {tp['price']} ({r_val:.1f}R)")
                    reply = (
                        f"✅ Spot trade saved: 🟢 LONG {result['coin']} @ {result['entry']}\n"
                        f"SL: {result['sl']}\n"
                        + "\n".join(tps_lines)
                    )
                    send_telegram_plain(reply)
                LAST_TG_UPDATE_ID = update["update_id"]
            elif text == "/perps":
                if not review_sent:
                    print("[TG] /perps received, generating stats...")
                    try:
                        stats_text = generate_perps_stats()
                        send_telegram_plain(stats_text)
                        print("[TG] Perps stats sent")
                    except Exception as e:
                        print(f"[TG] Perps stats failed: {e}")
                        send_telegram_plain("Error generating perps stats. Check logs.")
                    review_sent = True
                LAST_TG_UPDATE_ID = update["update_id"]
            elif text == "/btc":
                if not review_sent:
                    print("[TG] /btc received, fetching brief...")
                    try:
                        msg, _ = get_btc_brief_today()
                        send_telegram(msg)
                        print("[TG] BTC brief sent")
                    except Exception as e:
                        print(f"[TG] BTC brief failed: {e}")
                        send_telegram_plain("Error fetching BTC brief. Check logs.")
                    review_sent = True
                LAST_TG_UPDATE_ID = update["update_id"]
            elif text == "/review":
                if not review_sent:
                    print("[TG] /review received, generating report...")
                    try:
                        report_text = generate_review_report(conn)
                        send_telegram_plain(report_text)
                        print("[TG] Review report sent")
                    except Exception as e:
                        print(f"[TG] Review generation failed: {e}")
                        send_telegram_plain("Error generating review report. Check logs.")
                    review_sent = True
                LAST_TG_UPDATE_ID = update["update_id"]
            elif text == "/help":
                send_telegram_plain(
                    "Commands:\n"
                    "/btc - Today's BTC bias brief\n"
                    "/review - Signal tracker performance report\n"
                    "/limit - Add limit setup (e.g. /limit short BTC 81000 lev 20 invalid 81400 sl 81500 tp1 79000 tp2 78000)\n"
                    "/delete - Delete pending /limit setup (e.g. /delete 05-15-BTC-81000)\n"
                    "/position - Add market position (e.g. /position short BTC 80000 lev 20 sl 81000 tp1 79000 tp2 78000)\n"
                    "/perps - Monthly perps trade stats\n"
                    "/spot - Spot journal stats\n"
                    "/spot <...> - Add spot position (e.g. /spot long BTC 81000 sl 80000 tp1 83000 tp2 85000)"
                )
                LAST_TG_UPDATE_ID = update["update_id"]
            else:
                LAST_TG_UPDATE_ID = update["update_id"]

        if LAST_TG_UPDATE_ID > stored_id:
            set_app_state(conn, "last_tg_update_id", str(LAST_TG_UPDATE_ID))
    except Exception as e:
        print(f"[TG] Command check error: {e}")


def listen_commands():
    """Poll Telegram indefinitely for /review commands."""
    global LAST_TG_UPDATE_ID
    if not TG_BOT_TOKEN:
        print("No Telegram bot token configured")
        return

    conn = init_db()
    stored = get_app_state(conn, "last_tg_update_id", "0")
    try:
        stored_id = int(stored or 0)
    except Exception:
        stored_id = 0
    if stored_id > LAST_TG_UPDATE_ID:
        LAST_TG_UPDATE_ID = stored_id

    print("Listening for Telegram commands (/review)...")

    while True:
        try:
            params = {"timeout": 30}
            if LAST_TG_UPDATE_ID > 0:
                params["offset"] = LAST_TG_UPDATE_ID + 1

            url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates"
            resp = requests.get(url, params=params, timeout=35)

            if resp.status_code != 200:
                print(f"[TG] getUpdates HTTP {resp.status_code}")
                time.sleep(5)
                continue

            data = resp.json()
            if not data.get("ok"):
                print(f"[TG] getUpdates not ok: {data}")
                time.sleep(5)
                continue

            updates = data.get("result", [])
            if updates:
                print(f"[TG] Received {len(updates)} update(s)")

            for update in updates:
                if "message" not in update:
                    LAST_TG_UPDATE_ID = update["update_id"]
                    continue

                msg = update["message"]
                text = msg.get("text", "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))

                if chat_id != TG_CHAT_ID:
                    LAST_TG_UPDATE_ID = update["update_id"]
                    continue

                if text.startswith("/limit"):
                    print(f"[TG] /limit received: {text[:80]}")
                    result = parse_limit_command(text)
                    if isinstance(result, str):
                        send_telegram_plain(result)
                        print(f"[TG] /limit error: {result[:80]}")
                    else:
                        add_trade_to_journal(result)
                        direction_emoji = "🔴 SHORT" if result["direction"] == "short" else "🟢 LONG"
                        tps_lines = []
                        for i, tp in enumerate(result["tp_status"]):
                            r_val = abs(tp["price"] - result["entry"]) / result["risk_r"] if result["risk_r"] > 0 else 0
                            tps_lines.append(f"TP{i+1}: {tp['price']} ({r_val:.1f}R)")
                        reply = (
                            f"✅ Trade saved: {direction_emoji} {result['coin']} @ {result['entry']} (Lev {result.get('lev', 1)}x)\n"
                            f"SL: {result['sl']} | Invalid: {result['invalid']}\n"
                            + "\n".join(tps_lines)
                        )
                        reply += f"\n\nID: {result['id']}"
                        send_telegram_plain(reply)
                        print(f"[TG] Trade saved: {result['id']}")
                elif text.startswith("/position"):
                    print(f"[TG] /position received: {text[:80]}")
                    reply, _ = process_position_command(text)
                    send_telegram_plain(reply)
                    print(f"[TG] /position processed")
                elif text.startswith("/delete") or text.startswith("/del"):
                    parts = text.strip().split()
                    if len(parts) < 2:
                        send_telegram_plain("❌ Usage: /delete <trade_id>\nExample: /delete 05-15-BTC-81000")
                    else:
                        _, msg = remove_pending_limit_trade_from_journal(parts[1])
                        send_telegram_plain(msg)
                elif text == "/perps":
                    print("[TG] /perps received")
                    try:
                        stats_text = generate_perps_stats()
                        send_telegram_plain(stats_text)
                        print("[TG] Perps stats sent")
                    except Exception as e:
                        print(f"[TG] Perps stats error: {e}")
                        send_telegram_plain(f"Error: {e}")
                elif text == "/spot":
                    print("[TG] /spot received")
                    try:
                        stats_text = generate_spot_stats()
                        send_telegram_plain(stats_text)
                        print("[TG] Spot stats sent")
                    except Exception as e:
                        print(f"[TG] Spot stats error: {e}")
                        send_telegram_plain(f"Error: {e}")
                elif text.startswith("/spot"):
                    print(f"[TG] /spot received: {text[:80]}")
                    result = parse_spot_command(text)
                    if isinstance(result, str):
                        send_telegram_plain(result)
                        print(f"[TG] /spot error: {result[:80]}")
                    else:
                        add_spot_trade_to_journal(result)
                        tps_lines = []
                        for i, tp in enumerate(result["tp_status"]):
                            r_val = abs(tp["price"] - result["entry"]) / result["risk_r"] if result["risk_r"] > 0 else 0
                            tps_lines.append(f"TP{i+1}: {tp['price']} ({r_val:.1f}R)")
                        reply = (
                            f"✅ Spot trade saved: 🟢 LONG {result['coin']} @ {result['entry']}\n"
                            f"SL: {result['sl']}\n"
                            + "\n".join(tps_lines)
                        )
                        send_telegram_plain(reply)
                        print(f"[TG] Spot trade saved: {result['id']}")
                elif text == "/btc":
                    print("[TG] /btc received")
                    try:
                        msg, _ = get_btc_brief_today()
                        send_telegram(msg)
                        print("[TG] BTC brief sent")
                    except Exception as e:
                        print(f"[TG] BTC brief error: {e}")
                        send_telegram_plain(f"Error: {e}")
                elif text == "/review":
                    print("[TG] /review received")
                    try:
                        report_text = generate_review_report(conn)
                        send_telegram_plain(report_text)
                        print("[TG] Review report sent")
                    except Exception as e:
                        print(f"[TG] Review error: {e}")
                        send_telegram_plain(f"Error: {e}")
                elif text == "/help":
                    send_telegram_plain(
                        "Commands:\n"
                        "/btc - Today's BTC bias brief\n"
                        "/review - Signal tracker performance report\n"
                        "/limit - Add limit setup (e.g. /limit short BTC 81000 lev 20 invalid 81400 sl 81500 tp1 79000 tp2 78000)\n"
                        "/delete - Delete pending /limit setup (e.g. /delete 05-15-BTC-81000)\n"
                        "/position - Add market position (e.g. /position short BTC 80000 lev 20 sl 81000 tp1 79000 tp2 78000)\n"
                        "/perps - Monthly perps trade stats\n"
                        "/spot - Spot journal stats\n"
                        "/spot <...> - Add spot position (e.g. /spot long BTC 81000 sl 80000 tp1 83000 tp2 85000)"
                    )

                LAST_TG_UPDATE_ID = update["update_id"]
                if LAST_TG_UPDATE_ID > stored_id:
                    set_app_state(conn, "last_tg_update_id", str(LAST_TG_UPDATE_ID))
                    stored_id = LAST_TG_UPDATE_ID
        except Exception as e:
            print(f"[TG] Poll error: {e}")
            time.sleep(5)


def review_signals(conn):
    """Full performance review (CLI mode)."""
    report = generate_review_report(conn)
    print(report)
    c = conn.cursor()
    return c.execute("SELECT COUNT(*) FROM signal_tracker").fetchone()[0]


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    
    print(f"🏦 Accumulation Radar v1 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   Mode: {mode}\n")
    
    conn = init_db()
    
    if mode in ("full", "pool"):
        # === Module A: update the accumulation pool ===
        results = scan_accumulation_pool()
        
        if results:
            save_watchlist(conn, results)
            report = build_pool_report(results)
            if report:
                send_telegram(report)
    
    if mode in ("full", "oi"):
        # === Combined scan: OI + funding + accumulation in one pass ===
        watchlist = load_watchlist_symbols(conn)
        watchlist_set = set(watchlist)
        
        if not watchlist:
            print("⚠️ Watchlist is empty, run `pool` mode first")
            notify_data_blocked("watchlist empty after loading from DB")
            conn.close()
            return
        
        # 1. Fetch market-wide funding + ticker data
        tickers_raw = api_get("/fapi/v1/ticker/24hr")
        premiums_raw = api_get("/fapi/v1/premiumIndex")

        if not tickers_raw or not premiums_raw:
            print("❌ API request failed, retry in 60s...")
            time.sleep(60)
            tickers_raw = tickers_raw or api_get("/fapi/v1/ticker/24hr")
            premiums_raw = premiums_raw or api_get("/fapi/v1/premiumIndex")

        if not tickers_raw or not premiums_raw:
            print("❌ API request failed after retry")
            parts = []
            if not tickers_raw:
                parts.append(f"ticker failed: {LAST_API_FAILURES.get('/fapi/v1/ticker/24hr', 'unknown')}")
            if not premiums_raw:
                parts.append(f"premium failed: {LAST_API_FAILURES.get('/fapi/v1/premiumIndex', 'unknown')}")
            notify_data_blocked(" | ".join(parts) if parts else "ticker/premium endpoints returned empty data")
            conn.close()
            return
        
        ticker_map = {}
        for t in tickers_raw:
            if t["symbol"].endswith("USDT"):
                ticker_map[t["symbol"]] = {
                    "px_chg": float(t["priceChangePercent"]),
                    "vol": float(t["quoteVolume"]),
                    "price": float(t["lastPrice"]),
                }
        
        funding_map = {}
        for p in premiums_raw:
            if p["symbol"].endswith("USDT"):
                funding_map[p["symbol"]] = float(p["lastFundingRate"])
        
        # 1.5 Fetch real circulating market caps from the Binance spot API
        mcap_map = {}  # coin name -> marketCap
        try:
            import requests as _req
            _r = _req.get("https://www.binance.com/bapi/composite/v1/public/marketing/symbol/list", timeout=10)
            if _r.status_code == 200:
                for item in _r.json().get("data", []):
                    name = item.get("name", "")
                    mc = item.get("marketCap", 0)
                    if name and mc:
                        mcap_map[name] = float(mc)
                print(f"✅ Pulled real market caps for {len(mcap_map)} coins")
        except Exception as e:
            print(f"⚠️ Market-cap API failed, using fallback: {e}")
        
        # 2. Fetch heat data (CoinGecko Trending + volume surges)
        heat_map = {}  # coin name -> heat_score (0-100)
        cg_trending = set()
        try:
            import requests as _req
            _r = _req.get("https://api.coingecko.com/api/v3/search/trending", timeout=10)
            if _r.status_code == 200:
                for item in _r.json().get("coins", []):
                    sym = item["item"]["symbol"].upper()
                    rank = item["item"].get("score", 99)
                    cg_trending.add(sym)
                    heat_map[sym] = heat_map.get(sym, 0) + max(50 - rank * 3, 10)  # top1=50 pts, top10=20 pts
                print(f"🔥 CoinGecko Trending: {len(cg_trending)} coins")
        except Exception as e:
            print(f"⚠️ CoinGecko Trending failed: {e}")
        
        # Volume surge detection (24h volume vs 5-day average)
        vol_surge_coins = set()
        for sym, tk in ticker_map.items():
            coin = sym.replace("USDT", "")
            vol_24h = tk["vol"]
            # Quick 5-day average volume check; exact detail can be refined later
            # First, only consider coins with 24h volume > $20M
            if vol_24h > 20_000_000:
                kl = api_get("/fapi/v1/klines", {"symbol": sym, "interval": "1d", "limit": 6})
                if kl and len(kl) >= 5:
                    avg_5d = sum(float(k[7]) for k in kl[:-1]) / (len(kl)-1)
                    if avg_5d > 0:
                        ratio = vol_24h / avg_5d
                        if ratio >= 2.5:  # Volume expanded by at least 2.5x
                            vol_surge_coins.add(coin)
                            heat_map[coin] = heat_map.get(coin, 0) + min(ratio * 10, 50)  # Cap at 50 points
                    time.sleep(0.05)
        
        print(f"📈 Volume surges (>=2.5x): {len(vol_surge_coins)} coins")
        # Double heat
        dual_heat = cg_trending & vol_surge_coins
        if dual_heat:
            for coin in dual_heat:
                heat_map[coin] = heat_map.get(coin, 0) + 20  # Double-signal bonus
            print(f"🔥🔥 Dual heat: {dual_heat}")
        
        # 3. Read accumulation data from the database
        c2 = conn.cursor()
        c2.execute("SELECT symbol, score, sideways_days, range_pct, avg_vol, status, low_price, high_price, current_price FROM watchlist")
        pool_map = {}
        for row in c2.fetchall():
            pool_map[row[0]] = {"pool_score": row[1], "sideways_days": row[2], "range_pct": row[3], "avg_vol": row[4], "status": row[5], "low_price": row[6], "high_price": row[7], "current_price": row[8]}
        
        # 4. Scan OI for volume-expanding pool members + top-100 by volume
        scan_syms = set()
        for sym, pd in pool_map.items():
            if "Volume" in pd.get("status", ""):
                scan_syms.add(sym)
        top_by_vol = sorted(ticker_map.items(), key=lambda x: x[1]["vol"], reverse=True)[:100]
        for sym, _ in top_by_vol:
            scan_syms.add(sym)
        
        oi_map = {}
        for i, sym in enumerate(scan_syms):
            oi_hist = api_get("/futures/data/openInterestHist", {"symbol": sym, "period": "1h", "limit": 6})
            if oi_hist and len(oi_hist) >= 2:
                curr = float(oi_hist[-1]["sumOpenInterestValue"])
                prev_1h = float(oi_hist[-2]["sumOpenInterestValue"])
                prev_6h = float(oi_hist[0]["sumOpenInterestValue"])
                d1h = ((curr - prev_1h) / prev_1h * 100) if prev_1h > 0 else 0
                d6h = ((curr - prev_6h) / prev_6h * 100) if prev_6h > 0 else 0
                circ_supply = float(oi_hist[-1].get("CMCCirculatingSupply", 0))
                oi_map[sym] = {"oi_usd": curr, "d1h": d1h, "d6h": d6h, "circ_supply": circ_supply}
            if (i+1) % 10 == 0:
                time.sleep(0.5)
        
        # 5. Score the three strategies independently
        
        # Shared preprocessing
        all_syms = set(list(pool_map.keys()) + list(oi_map.keys()))
        coin_data = {}
        for sym in all_syms:
            tk = ticker_map.get(sym, {})
            if not tk: continue
            pool = pool_map.get(sym, {})
            oi = oi_map.get(sym, {})
            fr = funding_map.get(sym, 0)
            coin = sym.replace("USDT", "")
            
            d6h = oi.get("d6h", 0)
            fr_pct = fr * 100
            oi_usd = oi.get("oi_usd", 0)
            # Real circulating market cap: spot API first, then CMC supply from OI endpoint, then rough estimate
            if coin in mcap_map:
                est_mcap = mcap_map[coin]
            else:
                circ_supply = oi.get("circ_supply", 0)
                price = tk.get("price", 0) if isinstance(tk, dict) else 0
                if circ_supply > 0 and price > 0:
                    est_mcap = circ_supply * price
                else:
                    est_mcap = max(tk["vol"] * 0.3, oi_usd * 2) if oi_usd > 0 else tk["vol"] * 0.3
            sw_days = pool.get("sideways_days", 0) if pool else 0
            pool_sc = pool.get("pool_score", 0) if pool else 0
            
            heat = heat_map.get(coin, 0)
            
            coin_data[sym] = {
                "coin": coin, "sym": sym,
                "px_chg": tk["px_chg"], "vol": tk["vol"],
                "price": tk["price"],
                "fr_pct": fr_pct, "d6h": d6h,
                "oi_usd": oi_usd, "est_mcap": est_mcap,
                "sw_days": sw_days, "pool_sc": pool_sc,
                "in_pool": bool(pool), "heat": heat,
                "in_cg": coin in cg_trending,
                "vol_surge": coin in vol_surge_coins,
                "range_high": pool.get("high_price", 0) if pool else 0,
                "range_low": pool.get("low_price", 0) if pool else 0,
            }
        
        # ═══════════════════════════════════════
        # Strategy 1: momentum chase - pure funding ranking
        # ═══════════════════════════════════════
        chase = []
        for sym, d in coin_data.items():
            if d["px_chg"] > 3 and d["fr_pct"] < -0.005 and d["vol"] > 1_000_000:
                # Check funding trend
                fr_hist = api_get("/fapi/v1/fundingRate", {"symbol": sym, "limit": 5})
                fr_rates = [float(f["fundingRate"]) * 100 for f in fr_hist] if fr_hist else [d["fr_pct"]]
                fr_prev = fr_rates[-2] if len(fr_rates) >= 2 else d["fr_pct"]
                fr_delta = d["fr_pct"] - fr_prev
                
                trend = "🔥Accelerating" if fr_delta < -0.05 else "⬇️Turned Negative" if fr_delta < -0.01 else "➡️" if abs(fr_delta) < 0.01 else "⬆️Rebounding"
                
                chase.append({**d, "fr_delta": fr_delta, "trend": trend,
                              "rates": " → ".join([f"{x:.3f}" for x in fr_rates[-3:]])})
                time.sleep(0.2)
        
        # Sort purely by funding rate, most negative first
        chase.sort(key=lambda x: x["fr_pct"])
        
        # ═══════════════════════════════════════
        # Strategy 2: combined - balanced across all four dimensions
        # ═══════════════════════════════════════
        combined = []
        for sym, d in coin_data.items():
            # Funding score (25) - more negative is better
            fr = d["fr_pct"]
            if fr < -0.5: f_sc = 25
            elif fr < -0.1: f_sc = 22
            elif fr < -0.05: f_sc = 18
            elif fr < -0.03: f_sc = 14
            elif fr < -0.01: f_sc = 10
            elif fr < 0: f_sc = 5
            else: f_sc = 0
            
            # Market-cap score (25) - use real circulating market cap
            mc = d["est_mcap"]
            if mc > 0 and mc < 50e6: m_sc = 25
            elif mc < 100e6: m_sc = 22
            elif mc < 200e6: m_sc = 20
            elif mc < 300e6: m_sc = 17
            elif mc < 500e6: m_sc = 12
            elif mc < 1e9: m_sc = 7
            else: m_sc = 0
            
            # Sideways score (25)
            sw = d["sw_days"]
            if sw >= 120: s_sc = 25
            elif sw >= 90: s_sc = 22
            elif sw >= 75: s_sc = 18
            elif sw >= 60: s_sc = 14
            elif sw >= 45: s_sc = 10
            else: s_sc = 0
            
            # OI score (25)
            abs6 = abs(d["d6h"])
            if abs6 >= 15: o_sc = 25
            elif abs6 >= 8: o_sc = 22
            elif abs6 >= 5: o_sc = 18
            elif abs6 >= 3: o_sc = 14
            elif abs6 >= 2: o_sc = 10
            else: o_sc = 0
            
            total = f_sc + m_sc + s_sc + o_sc
            if total < 75: continue
            
            combined.append({**d, "total": total,
                            "f_sc": f_sc, "m_sc": m_sc, "s_sc": s_sc, "o_sc": o_sc})
        
        combined.sort(key=lambda x: x["total"], reverse=True)
        
        # ═══════════════════════════════════════
        # Strategy 3: ambush - market cap > OI > sideways > funding
        # ═══════════════════════════════════════
        ambush = []
        for sym, d in coin_data.items():
            if not d["in_pool"]: continue  # Must be in the accumulation pool
            if d["px_chg"] > 50: continue  # Exclude coins that already exploded
            
            # 1. Market cap (35) - the lower, the better
            mc = d["est_mcap"]
            if mc > 0 and mc < 50e6: m_sc = 35
            elif mc < 100e6: m_sc = 32
            elif mc < 150e6: m_sc = 28
            elif mc < 200e6: m_sc = 25
            elif mc < 300e6: m_sc = 20
            elif mc < 500e6: m_sc = 12
            elif mc < 1e9: m_sc = 5
            else: m_sc = 0
            
            # 2. OI anomaly (30) - rising OI plus low market cap is excellent
            abs6 = abs(d["d6h"])
            if abs6 >= 10: o_sc = 30
            elif abs6 >= 5: o_sc = 25
            elif abs6 >= 3: o_sc = 20
            elif abs6 >= 2: o_sc = 14
            elif abs6 >= 1: o_sc = 8
            else: o_sc = 0
            # Underflow bonus: OI rises while price stays flat
            if d["d6h"] > 2 and abs(d["px_chg"]) < 5:
                o_sc = min(o_sc + 5, 30)
            
            # 3. Sideways action (20)
            sw = d["sw_days"]
            if sw >= 120: s_sc = 20
            elif sw >= 90: s_sc = 17
            elif sw >= 75: s_sc = 14
            elif sw >= 60: s_sc = 10
            elif sw >= 45: s_sc = 6
            else: s_sc = 0
            
            # 4. Negative funding (15) - negative funding is a bonus
            fr = d["fr_pct"]
            if fr < -0.1: f_sc = 15
            elif fr < -0.05: f_sc = 12
            elif fr < -0.03: f_sc = 9
            elif fr < -0.01: f_sc = 6
            elif fr < 0: f_sc = 3
            else: f_sc = 0
            
            total = m_sc + o_sc + s_sc + f_sc
            if total < 75: continue
            
            ambush.append({**d, "total": total,
                          "m_sc": m_sc, "o_sc": o_sc, "s_sc": s_sc, "f_sc": f_sc})
        
        ambush.sort(key=lambda x: x["total"], reverse=True)
        reversal = score_reversal(coin_data, pool_map, conn)

        # 5.5 Save signals to tracker and check pending breakouts
        now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        save_signals(conn, chase, combined, ambush, reversal, coin_data, pool_map, now_str)
        check_breakouts(conn, ticker_map)

        # ═══════════════════════════════════════
        # 6. Build notification + worth-watching highlights
        # ═══════════════════════════════════════
        def mcap_str(v):
            if v >= 1e6: return f"${v/1e6:.0f}M"
            if v >= 1e3: return f"${v/1e3:.0f}K"
            return f"${v:.0f}"
        
        now = datetime.now(timezone(timedelta(hours=8)))
        lines = [
            f"🏦 **Smart Money Radar** - Three Strategies + Heat",
            f"⏰ {now.strftime('%Y-%m-%d %H:%M')} WIB",
        ]
        
        # Table 0: heat ranking (most important, put first)
        hot_coins = sorted(
            [d for d in coin_data.values() if d["heat"] > 0],
            key=lambda x: x["heat"], reverse=True
        )
        if hot_coins:
            lines.append(f"\n🔥 **Heat Ranking** (CG trending + volume surge)")
            for s in hot_coins[:8]:
                tags = []
                if s["in_cg"]: tags.append("🌐CG Trending")
                if s["vol_surge"]: tags.append("📈Vol Surge")
                oi_tag = f"OI{s['d6h']:+.0f}%" if abs(s["d6h"]) >= 3 else ""
                if oi_tag: tags.append(f"⚡{oi_tag}")
                if s["in_pool"]: tags.append(f"💤Pool {s['sw_days']}d")
                fr_tag = f"🧊{s['fr_pct']:.2f}%" if s["fr_pct"] < -0.03 else ""
                if fr_tag: tags.append(fr_tag)
                lines.append(
                    f"  {s['coin']:<8} ~{mcap_str(s['est_mcap'])} Move {s['px_chg']:+.0f}% | {' '.join(tags)}"
                )
        
        # Table 1: momentum chase
        lines.append(f"\n🔥 **Momentum Chase** (ranked by funding)")
        if chase:
            for s in chase[:8]:
                lines.append(
                    f"  {s['coin']:<7} Funding {s['fr_pct']:+.3f}% {s['trend']}"
                    f" | Move {s['px_chg']:+.0f}% | ~{mcap_str(s['est_mcap'])}"
                )
        else:
            lines.append("  None yet (requires move >3% + negative funding)")
        
        # Table 2: combined
        lines.append(f"\n📊 **Combined** (Funding + Market Cap + Sideways + OI, 25 each)")
        for s in combined[:8]:
            dims = []
            if s["f_sc"] >= 10: dims.append(f"🧊{s['fr_pct']:.2f}%")
            if s["m_sc"] >= 12: dims.append(f"💎{mcap_str(s['est_mcap'])}")
            if s["s_sc"] >= 10: dims.append(f"💤{s['sw_days']}d")
            if s["o_sc"] >= 10: dims.append(f"⚡OI{s['d6h']:+.0f}%")
            lines.append(
                f"  {s['coin']:<7} {s['total']} pts | {' '.join(dims)}"
            )
        
        # Table 3: ambush
        lines.append(f"\n🎯 **Ambush** (Market Cap 35 + OI 30 + Sideways 20 + Funding 15)")
        for s in ambush[:8]:
            tags = [f"~{mcap_str(s['est_mcap'])}"]
            if abs(s["d6h"]) >= 2: tags.append(f"OI{s['d6h']:+.0f}%")
            if s["d6h"] > 2 and abs(s["px_chg"]) < 5: tags.append("🎯Underflow")
            if s["sw_days"] >= 45: tags.append(f"Sideways {s['sw_days']}d")
            if s["fr_pct"] < -0.01: tags.append(f"Funding {s['fr_pct']:.2f}%")
            lines.append(
                f"  {s['coin']:<7} {s['total']} pts | {' '.join(tags)}"
            )

        # Table 4: reversal watch (short setups)
        lines.append(f"\n🔻 **Reversal Watch** (Short setup candidates)")
        if reversal:
            for s in reversal[:8]:
                tag_str = " ".join(s["rev_tags"][:3])
                extra = []
                if s["fr_pct"] > 0.03:
                    extra.append(f"Fnd+{s['fr_pct']:.2f}%")
                extr_str = " ".join(extra)
                lines.append(
                    f"  {s['coin']:<7} {s['rev_score']} pts | OI{s['d6h']:+.0f}% Px{s['px_chg']:+.0f}%"
                    f" | ~{mcap_str(s['est_mcap'])} | {tag_str} {extr_str}".strip()
                )
        else:
            lines.append("  None yet (no strong short signals)")

        # Worth-watching highlights
        highlights = []
        
        # Heat + pool overlap = strongest early signal
        hot_pool = [d for d in coin_data.values() if d["heat"] > 0 and d["in_pool"]]
        for s in sorted(hot_pool, key=lambda x: x["heat"], reverse=True)[:2]:
            tags = []
            if s["in_cg"]: tags.append("CG Trending")
            if s["vol_surge"]: tags.append("Volume Surge")
            highlights.append(f"🔥💤 {s['coin']} heat ({'+'.join(tags)}) + {s['sw_days']}d in accumulation = OI may follow")
        
        # Heat + OI already rising = move is underway
        hot_oi = [d for d in coin_data.values() if d["heat"] > 0 and d["d6h"] > 5]
        for s in sorted(hot_oi, key=lambda x: x["d6h"], reverse=True)[:2]:
            if s["coin"] not in " ".join(highlights):
                highlights.append(f"🔥⚡ {s['coin']} heat + OI{s['d6h']:+.0f}% are rising together!")
        
        # Top two momentum names with accelerating funding deterioration
        chase_fire = [s for s in chase[:5] if "Accelerating" in s.get("trend", "")]
        for s in chase_fire[:2]:
            highlights.append(f"🔥 {s['coin']} funding {s['fr_pct']:.3f}% is deteriorating faster, shorts keep flooding in")
        
        # Coins appearing across multiple tables
        chase_coins = set(s["coin"] for s in chase[:10])
        combined_coins = set(s["coin"] for s in combined[:10])
        ambush_coins = set(s["coin"] for s in ambush[:10])
        reversal_coins = set(s["coin"] for s in reversal[:10])

        # Shared between momentum chase and combined
        overlap_2 = chase_coins & combined_coins
        if overlap_2:
            for c in list(overlap_2)[:2]:
                highlights.append(f"⭐ {c} appears in both Momentum Chase and Combined")

        # Reversal coins also in combined/ambush = conflicting signals = interesting
        rev_in_combined = reversal_coins & combined_coins
        if rev_in_combined:
            for c in list(rev_in_combined)[:1]:
                highlights.append(f"⚠️ {c} in both Reversal + Combined = conflicting signal, watch closely")

        # Reversal coins also in the pool (was accumulation, now reversing)
        rev_in_pool = [s for s in reversal[:10] if s["in_pool"]]
        for s in rev_in_pool[:1]:
            if s["coin"] not in [h.split(" ")[1] for h in highlights]:
                highlights.append(f"🔻 {s['coin']} in accumulation pool but showing short signal = thesis may be broken")
        
        # Ambush names showing underflow
        ambush_dark = [s for s in ambush[:10] if s["d6h"] > 2 and abs(s["px_chg"]) < 5]
        for s in ambush_dark[:2]:
            highlights.append(f"🎯 {s['coin']} underflow! OI{s['d6h']:+.0f}% while price is flat, market cap only {mcap_str(s['est_mcap'])}")
        
        # Ambush names with very low market cap + OI anomaly
        ambush_gem = [s for s in ambush[:10] if s["est_mcap"] < 100e6 and abs(s["d6h"]) >= 3]
        for s in ambush_gem[:2]:
            if s["coin"] not in [h.split(" ")[1] for h in highlights]:
                highlights.append(f"💎 {s['coin']} low market cap {mcap_str(s['est_mcap'])} + OI{s['d6h']:+.0f}% makes it a top ambush candidate")

        # Short/reversal highlights
        rev_short_build = [s for s in reversal[:10] if s["d6h"] > 3 and s["px_chg"] < -2]
        for s in rev_short_build[:2]:
            highlights.append(f"🔻 {s['coin']} aggressive short build! OI{s['d6h']:+.0f}% + price {s['px_chg']:+.0f}%")

        rev_squeeze = [s for s in reversal[:10] if s["fr_pct"] > 0.05 and abs(s["px_chg"]) < 3]
        for s in rev_squeeze[:1]:
            highlights.append(f"🔻 {s['coin']} funding +{s['fr_pct']:.2f}% stalled = long squeeze fuel")

        rev_failed = [s for s in reversal[:10] if any("FailedBreakout" in t for t in s.get("rev_tags", []))]
        for s in rev_failed[:1]:
            highlights.append(f"🔻 {s['coin']} failed breakout: price fell back below range high")

        rev_below = [s for s in reversal[:10] if s.get("in_pool") and s.get("range_low", 0) > 0 and s.get("price", 0) < s.get("range_low", 0)]
        for s in rev_below[:1]:
            highlights.append(f"🔻 {s['coin']} below accumulation range low = structure broken")

        if highlights:
            lines.append(f"\n💡 **Worth Watching**")
            for h in highlights[:10]:
                lines.append(f"  {h}")
        
        # What To Do guide (actionable, not just emoji legend)
        lines.append(f"\n📖 **How To Trade These Signals**")
        lines.append("")
        lines.append("  🎯 **Underflow** (OI↑ Price flat) — Highest priority")
        lines.append("     → Alert at range high, enter on VOLUME-CONFIRMED breakout (>3x)")
        lines.append("     → Stop: below range low | Target: 2-3x range width")
        lines.append("  🔥 **Momentum Chase** (neg funding + price moving)")
        lines.append("     → Wait for pullback, don't chase green candles")
        lines.append("     → Stop: -5% | Exit: funding flips above -0.01%")
        lines.append("  💤 **Ambush / Pool Only** (accumulating, no move yet)")
        lines.append("     → Not actionable yet -- add to Binance watchlist")
        lines.append("     → Wait for: OI spike OR volume breakout OR funding flip")
        lines.append("")
        lines.append("  📊 **OI-Price Matrix**:")
        lines.append("     OI↑ Price↑ = trend | OI↑ Price→ = 🎯underflow | OI↑ Price↓ = shorts | OI↓ Price↑ = squeeze")
        lines.append("")
        lines.append("  🔻 **Reversal Watch** (Short setups)")
        lines.append("     → Best signal: OI rising + price falling = whales building shorts")
        lines.append("     → Enter after first lower low, stop above recent swing high")
        lines.append("     → Target: 1-2x sideways range downward")
        lines.append("     → Failed breakout: short when price closes back below range high")

        report = "\n".join(lines)

        # Append signal tracking recap if there are tracked signals
        tracking_recap = build_tracking_recap(conn)
        if tracking_recap:
            report += "\n" + tracking_recap
            if len(report) < 3500:
                send_telegram(report)
            else:
                send_telegram(report)
        else:
            send_telegram(report)
        if TG_POLL_COMMANDS_IN_OI:
            check_telegram_commands(conn)
    
    if mode == "btc":
        conn.close()
        generate_btc_brief()
        print("\n✅ BTC brief complete")
        return

    if mode == "sync":
        conn.close()
        sync_trades()
        print("\n✅ Trade sync complete")
        return

    if mode == "review":
        review_signals(conn)
        print("\n✅ Review complete")
        conn.close()
        return

    if mode == "perps":
        conn.close()
        stats = generate_perps_stats()
        if stats:
            send_telegram_plain(stats)
        print(stats)
        print("\n✅ Perps stats complete")
        return

    if mode == "listen":
        conn.close()
        listen_commands()
        return

    conn.close()
    print("\n✅ Done")


if __name__ == "__main__":
    main()
