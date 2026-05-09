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

        if score >= 15:
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

            if text == "/review":
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

                if text == "/review":
                    print("[TG] /review received")
                    try:
                        conn = init_db()
                        report_text = generate_review_report(conn)
                        send_telegram_plain(report_text)
                        conn.close()
                        print("[TG] Review report sent")
                    except Exception as e:
                        print(f"[TG] Review error: {e}")
                        send_telegram_plain(f"Error: {e}")
                elif text == "/help":
                    send_telegram_plain("Commands:\n/review - Signal tracker performance report")

                LAST_TG_UPDATE_ID = update["update_id"]
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
            if total < 25: continue
            
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
            if total < 20: continue
            
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
        check_telegram_commands(conn)
    
    if mode == "review":
        review_signals(conn)
        print("\n✅ Review complete")
        conn.close()
        return

    if mode == "listen":
        conn.close()
        listen_commands()
        return

    conn.close()
    print("\n✅ Done")


if __name__ == "__main__":
    main()
