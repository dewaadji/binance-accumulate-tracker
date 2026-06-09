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
import subprocess
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

# BTC brief journal config (for storing daily BTC analysis history)
_default_btc_journal_dir = Path(__file__).parent / "data" / "btc_journal"
if DB_PATH.is_absolute() and str(DB_PATH).startswith("/data/"):
    _default_btc_journal_dir = DB_PATH.parent / "btc_journal"
BTC_JOURNAL_DIR = Path(os.getenv("BTC_JOURNAL_DIR", str(_default_btc_journal_dir)))


def ensure_btc_journal_dir():
    """Ensure BTC journal directory exists."""
    BTC_JOURNAL_DIR.mkdir(parents=True, exist_ok=True)


def load_journal(month_str):
    """Load BTC brief journal JSON for a given month (YYYY-MM)."""
    ensure_btc_journal_dir()
    path = BTC_JOURNAL_DIR / f"{month_str}.json"
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {"month": month_str, "btc_briefs": []}
    return {"month": month_str, "btc_briefs": []}


def save_journal(month_str, data):
    """Save BTC brief journal JSON for a given month."""
    ensure_btc_journal_dir()
    path = BTC_JOURNAL_DIR / f"{month_str}.json"
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

    # v2 upgrade: hourly snapshot history for delta computation
    c.execute("""CREATE TABLE IF NOT EXISTS hourly_token_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        price REAL,
        price_24h_change_pct REAL,
        open_interest REAL,
        oi_change_pct_from_baseline REAL,
        funding_rate REAL,
        volume_24h REAL,
        quote_volume_24h REAL,
        pool_setup_state TEXT,
        breakout_state TEXT,
        trade_state TEXT,
        action TEXT,
        origin_strategies TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE INDEX IF NOT EXISTS idx_snapshot_symbol_time
        ON hourly_token_snapshots(symbol, timestamp DESC)""")

    # v2 upgrade: idempotent ALTER TABLE migrations
    migrations = [
        ("watchlist", "range_position_pct REAL"),
        ("watchlist", "distance_to_high_pct REAL"),
        ("watchlist", "breakout_state TEXT"),
        ("watchlist", "pool_setup_state TEXT"),
        ("watchlist", "pool_quality_score REAL"),
        ("watchlist", "entry_readiness_score REAL"),
        ("watchlist", "vol_breakout REAL"),
        ("signal_tracker", "trade_state TEXT"),
        ("signal_tracker", "origin_pool_setup_state TEXT"),
        ("signal_tracker", "action_label TEXT"),
    ]
    for table, col_def in migrations:
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass  # column already exists

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


# v2: in-session cache for 1h klines (avoid duplicate API calls during one OI scan)
_KLINE_1H_CACHE = {}


def get_recent_1h_klines(symbol, limit=24):
    """Fetch recent 1h klines for a symbol, cached per session."""
    cache_key = (symbol, limit)
    if cache_key in _KLINE_1H_CACHE:
        return _KLINE_1H_CACHE[cache_key]
    klines = api_get("/fapi/v1/klines", {"symbol": symbol, "interval": "1h", "limit": limit})
    if klines:
        _KLINE_1H_CACHE[cache_key] = klines
    return klines or []


def save_hourly_snapshot(conn, snapshot):
    """Insert one snapshot row into hourly_token_snapshots.

    snapshot keys: symbol, timestamp (ISO), price, price_24h_change_pct,
    open_interest, oi_change_pct_from_baseline, funding_rate, volume_24h,
    quote_volume_24h, pool_setup_state, breakout_state, trade_state, action,
    origin_strategies (list -> JSON string).
    """
    c = conn.cursor()
    origin = snapshot.get("origin_strategies") or []
    if isinstance(origin, list):
        origin_str = json.dumps(origin)
    else:
        origin_str = str(origin)
    c.execute(
        """INSERT INTO hourly_token_snapshots
            (symbol, timestamp, price, price_24h_change_pct, open_interest,
             oi_change_pct_from_baseline, funding_rate, volume_24h, quote_volume_24h,
             pool_setup_state, breakout_state, trade_state, action, origin_strategies)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (snapshot.get("symbol"), snapshot.get("timestamp"),
         snapshot.get("price"), snapshot.get("price_24h_change_pct"),
         snapshot.get("open_interest"), snapshot.get("oi_change_pct_from_baseline"),
         snapshot.get("funding_rate"), snapshot.get("volume_24h"),
         snapshot.get("quote_volume_24h"),
         snapshot.get("pool_setup_state"), snapshot.get("breakout_state"),
         snapshot.get("trade_state"), snapshot.get("action"), origin_str),
    )
    conn.commit()


def get_prior_snapshot(conn, symbol, hours_ago):
    """Return the snapshot row roughly `hours_ago` hours before now.

    Looks for a snapshot in a tolerance window centered on (now - hours_ago):
      - 1h delta: window ±20 min around 1h ago  → [now-80m, now-40m]
      - 3h delta: window ±20 min around 3h ago  → [now-200m, now-160m]
    Returns the row as a dict, or None if no match.
    """
    now = datetime.now(timezone.utc)
    target = now - timedelta(hours=hours_ago)
    tolerance = timedelta(minutes=20)
    win_start = (target - tolerance).strftime("%Y-%m-%dT%H:%M:%S")
    win_end = (target + tolerance).strftime("%Y-%m-%dT%H:%M:%S")

    c = conn.cursor()
    c.row_factory = sqlite3.Row
    row = c.execute(
        """SELECT * FROM hourly_token_snapshots
           WHERE symbol = ? AND timestamp BETWEEN ? AND ?
           ORDER BY ABS(julianday(timestamp) - julianday(?)) ASC
           LIMIT 1""",
        (symbol, win_start, win_end, target.strftime("%Y-%m-%dT%H:%M:%S")),
    ).fetchone()
    return dict(row) if row else None


def prune_old_snapshots(conn, days_to_keep=7):
    """Delete snapshots older than N days to bound DB size."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_to_keep)).strftime("%Y-%m-%dT%H:%M:%S")
    c = conn.cursor()
    c.execute("DELETE FROM hourly_token_snapshots WHERE timestamp < ?", (cutoff,))
    conn.commit()


# v2: lifecycle (trade_state) classification
ACTION_MAP = {
    "READY_LONG":          "Prepare entry. Wait for close breakout or retest.",
    "TRIGGERED_LONG":      "Entry allowed with risk management.",
    "ACTIVE_TREND":        "Hold/manage. New entry only on pullback/retest.",
    "LATE_LONG":           "NO CHASE. Retest only. Reduce priority.",
    "READY_SHORT":         "Wait for lower low or failed reclaim.",
    "TRIGGERED_SHORT":     "Short allowed. Stop above failed reclaim.",
    "LATE_SHORT":          "Do not chase short. Wait bounce/retest.",
    "EARLY_UNDERFLOW":     "Alert only. Wait for OI acceleration + price breakout.",
    "NO_CONFIRMATION":     "Deprioritize / watch only.",
    "SHORT_COVERING_ONLY": "No fresh long unless OI turns positive again.",
    "DISTRIBUTION_RISK":   "Avoid new long. Watch reversal or exit if long.",
    "EXIT_WARNING":        "Take profit / tighten stop. No new long.",
    "INVALIDATED":         "Skip. Setup invalidated.",
}

RISK_NOTES = {
    "READY_LONG":          ["Avoid market chase if candle already extended.",
                            "Stop below retest low / range reclaim level."],
    "TRIGGERED_LONG":      ["Stop below retest low or range reclaim.",
                            "Reduce size if 24h move already > 50%."],
    "ACTIVE_TREND":        ["Trail stop under last higher-low.",
                            "Do not market chase. Wait pullback."],
    "LATE_LONG":           ["No new long.",
                            "Only retest scalp with smaller size.",
                            "Watch OI rising while price stalls = long trap."],
    "READY_SHORT":         ["Wait for lower low / failed reclaim.",
                            "Avoid short if already dumped >20% in 24h."],
    "TRIGGERED_SHORT":     ["Stop above failed reclaim / recent swing high.",
                            "Skip if 24h move already < -20%."],
    "LATE_SHORT":          ["Do not chase. Wait bounce/retest."],
    "EARLY_UNDERFLOW":     ["Position not active. Watch OI acceleration."],
    "DISTRIBUTION_RISK":   ["OI up + price stalling after big move = potential long trap.",
                            "Reduce exposure if long."],
    "EXIT_WARNING":        ["Take profit / tighten stop.",
                            "No new long."],
    "SHORT_COVERING_ONLY": ["Price up + OI down = covering, not fresh trend.",
                            "Wait for OI to turn positive again."],
    "NO_CONFIRMATION":     ["No futures confirmation yet. Watch only."],
    "INVALIDATED":         ["Setup invalidated. Skip."],
}


def detect_breakout_confirmation(symbol, range_high):
    """True iff last 2 hourly closes are above range_high."""
    if range_high is None or range_high <= 0:
        return False
    klines = get_recent_1h_klines(symbol, limit=4)
    if not klines or len(klines) < 2:
        return False
    closes = [float(k[4]) for k in klines[-2:]]
    return all(c > range_high for c in closes)


def detect_breakdown_confirmation(symbol, range_low):
    """True iff last 2 hourly closes are below range_low."""
    if range_low is None or range_low <= 0:
        return False
    klines = get_recent_1h_klines(symbol, limit=4)
    if not klines or len(klines) < 2:
        return False
    closes = [float(k[4]) for k in klines[-2:]]
    return all(c < range_low for c in closes)


def detect_failed_reclaim(symbol, range_low):
    """After a breakdown, did price try to reclaim range_low but fail?
    Heuristic: in last 6h, the highest wick reached above range_low but the
    most recent close is back below it."""
    if range_low is None or range_low <= 0:
        return False
    klines = get_recent_1h_klines(symbol, limit=6)
    if not klines or len(klines) < 3:
        return False
    highs = [float(k[2]) for k in klines]
    last_close = float(klines[-1][4])
    return any(h > range_low for h in highs) and last_close < range_low


def classify_trade_state(coin_data, prior_1h, prior_3h, pool_row):
    """Lifecycle classification — returns dict with trade_state, action, transition,
    deltas, and the helper booleans used (for transparency in output).

    coin_data: dict with current `price`, `oi_usd`, `fr_pct`, `px_chg` (24h %),
               `sym` (symbol).
    prior_1h, prior_3h: rows from hourly_token_snapshots (or None).
    pool_row: matching watchlist row dict (or None) for range_high/low/breakout_state.
    """
    current_price = coin_data.get("price") or 0
    current_oi = coin_data.get("oi_usd") or 0
    current_funding = coin_data.get("fr_pct", 0) / 100.0 if coin_data.get("fr_pct") is not None else 0
    price_24h = coin_data.get("px_chg")  # already 24h %

    # ---- delta computations ----
    def pct_delta(curr, prev):
        if prev is None or prev == 0 or curr is None:
            return None
        return ((curr - prev) / prev) * 100

    price_1h = pct_delta(current_price, prior_1h.get("price") if prior_1h else None)
    oi_1h = pct_delta(current_oi, prior_1h.get("open_interest") if prior_1h else None)
    fund_1h = None
    if prior_1h and prior_1h.get("funding_rate") is not None:
        fund_1h = current_funding - prior_1h["funding_rate"]
    price_3h = pct_delta(current_price, prior_3h.get("price") if prior_3h else None)
    oi_3h = pct_delta(current_oi, prior_3h.get("open_interest") if prior_3h else None)

    # ---- pool context ----
    breakout_state = (pool_row or {}).get("breakout_state")
    pool_setup_state = (pool_row or {}).get("pool_setup_state")
    range_high = (pool_row or {}).get("high_price")
    range_low = (pool_row or {}).get("low_price")
    distance_to_high_pct = (pool_row or {}).get("distance_to_high_pct")
    vol_breakout = (pool_row or {}).get("vol_breakout")
    avg_vol = (pool_row or {}).get("avg_vol")

    price_from_breakout_pct = None
    if range_high and range_high > 0 and current_price > range_high:
        price_from_breakout_pct = ((current_price - range_high) / range_high) * 100

    is_extended = (
        breakout_state == "EXTENDED_BREAKOUT"
        or (price_24h is not None and price_24h > 100)
    )
    near_resistance = (
        distance_to_high_pct is not None and distance_to_high_pct < 10
        and breakout_state in ("INSIDE_RANGE_HIGH", "BREAKOUT_ZONE")
    )

    volume_confirmed = False
    if avg_vol and avg_vol > 0:
        cur_vol = coin_data.get("vol", 0)
        # vol_breakout is 7d/avg ratio from pool. For OI-mode we approximate via 24h vol.
        volume_confirmed = cur_vol >= 3 * avg_vol or (vol_breakout or 0) >= 3

    sym = coin_data.get("sym")
    breakout_confirmed = False
    breakdown_confirmed = False
    failed_reclaim = False
    if sym and range_high:
        breakout_confirmed = detect_breakout_confirmation(sym, range_high)
    if sym and range_low:
        breakdown_confirmed = detect_breakdown_confirmation(sym, range_low)
        if breakdown_confirmed:
            failed_reclaim = detect_failed_reclaim(sym, range_low)

    # ---- classification (priority order — first match wins) ----
    def safe_abs(x):
        return abs(x) if x is not None else 0

    trade_state = "NO_CONFIRMATION"

    # 1. LATE_LONG — extended pump
    if (
        (price_24h is not None and price_24h > 100)
        or (price_from_breakout_pct is not None and price_from_breakout_pct > 30)
        or breakout_state == "EXTENDED_BREAKOUT"
    ):
        trade_state = "LATE_LONG"
    # 2. EXIT_WARNING — big move + OI bleeding
    elif (price_24h is not None and price_24h > 30
          and oi_1h is not None and oi_1h < -10):
        trade_state = "EXIT_WARNING"
    # 3. DISTRIBUTION_RISK — stalled price after move, OI still rising
    elif (price_1h is not None and price_1h <= 0
          and oi_1h is not None and oi_1h > 10
          and price_24h is not None and price_24h > 30):
        trade_state = "DISTRIBUTION_RISK"
    # 4. LATE_SHORT — already dumped
    elif price_24h is not None and price_24h < -20:
        trade_state = "LATE_SHORT"
    # 5. TRIGGERED_SHORT
    elif (breakdown_confirmed and failed_reclaim
          and oi_1h is not None and oi_1h > 10):
        trade_state = "TRIGGERED_SHORT"
    # 6. READY_SHORT
    elif (oi_1h is not None and oi_1h > 10
          and price_1h is not None and price_1h < -2
          and current_funding >= 0):
        trade_state = "READY_SHORT"
    # 7. ACTIVE_TREND
    elif (price_3h is not None and price_3h > 20
          and oi_3h is not None and oi_3h > 30
          and current_funding < 0):
        trade_state = "ACTIVE_TREND"
    # 8. TRIGGERED_LONG
    elif (breakout_confirmed
          and oi_1h is not None and oi_1h >= 15
          and volume_confirmed
          and not is_extended):
        trade_state = "TRIGGERED_LONG"
    # 9. READY_LONG
    elif (oi_1h is not None and oi_1h >= 15
          and price_1h is not None and price_1h > 0
          and near_resistance
          and not is_extended):
        trade_state = "READY_LONG"
    # 10. SHORT_COVERING_ONLY
    elif (price_1h is not None and price_1h > 0
          and oi_1h is not None and oi_1h < 0):
        trade_state = "SHORT_COVERING_ONLY"
    # 11. EARLY_UNDERFLOW
    elif (oi_1h is not None and 3 <= oi_1h < 15
          and safe_abs(price_1h) < 3):
        trade_state = "EARLY_UNDERFLOW"
    # 12. NO_CONFIRMATION (fallback)
    elif (oi_1h is not None and oi_1h <= 0
          and safe_abs(price_1h) < 3):
        trade_state = "NO_CONFIRMATION"

    # ---- transition chain ----
    prior_3h_state = (prior_3h or {}).get("trade_state") or "UNKNOWN"
    prior_1h_state = (prior_1h or {}).get("trade_state") or "UNKNOWN"
    if prior_1h is None and prior_3h is None:
        transition = f"UNKNOWN → {trade_state}"
    elif prior_3h_state == prior_1h_state and prior_3h_state != "UNKNOWN":
        transition = f"{prior_1h_state} → {trade_state}"
    else:
        transition = f"{prior_3h_state} → {prior_1h_state} → {trade_state}"

    return {
        "trade_state": trade_state,
        "action": ACTION_MAP.get(trade_state, ""),
        "risk_notes": RISK_NOTES.get(trade_state, []),
        "transition": transition,
        "price_1h_change_pct": price_1h,
        "oi_1h_change_pct": oi_1h,
        "funding_1h_change": fund_1h,
        "price_3h_change_pct": price_3h,
        "oi_3h_change_pct": oi_3h,
        "price_from_breakout_pct": price_from_breakout_pct,
        "is_extended": is_extended,
        "near_resistance": near_resistance,
        "volume_confirmed": volume_confirmed,
        "breakout_confirmed": breakout_confirmed,
        "breakdown_confirmed": breakdown_confirmed,
        "failed_reclaim": failed_reclaim,
        "origin_pool_setup_state": pool_setup_state,
    }


# v2: bucket mapping for hourly output
LIFECYCLE_BUCKETS = {
    "ACTIONABLE": {"READY_LONG", "TRIGGERED_LONG", "READY_SHORT", "TRIGGERED_SHORT"},
    "ALERT":      {"EARLY_UNDERFLOW"},
    "ACTIVE_LATE": {"ACTIVE_TREND", "LATE_LONG", "LATE_SHORT"},
    "AVOID":      {"NO_CONFIRMATION", "SHORT_COVERING_ONLY", "DISTRIBUTION_RISK",
                   "EXIT_WARNING", "INVALIDATED"},
}


# v2.1: compact display maps for 2-line Telegram output
POOL_STATE_VISUAL = {
    # state → (status_emoji, action_emoji, short_hint)
    "PRICE_BREAKOUT_CONFIRMED": ("🟢", "🚀", "retest watch"),
    "ARMED_INSIDE_RANGE":       ("🟠", "💪", "wait BO + OI accel"),
    "WAKING_UP":                ("🟡", "🌅", "watch OI + resistance"),
    "EXTENDED_BREAKOUT":        ("⚠️", "🔥", "no chase, retest only"),
    "SLEEPING_ACCUMULATION":    ("💤", "😴", "still monitoring"),
    "INVALID_RANGE":            ("⚪", "❓", "invalid range data"),
}

TRADE_STATE_EMOJI = {
    "READY_LONG": "🟢", "TRIGGERED_LONG": "🟢",
    "ACTIVE_TREND": "🔥", "LATE_LONG": "🔥",
    "READY_SHORT": "🔻", "TRIGGERED_SHORT": "🔻", "LATE_SHORT": "🔻",
    "EARLY_UNDERFLOW": "🟠",
    "NO_CONFIRMATION": "⚪",
    "SHORT_COVERING_ONLY": "⚠️", "DISTRIBUTION_RISK": "⚠️", "EXIT_WARNING": "⚠️",
    "INVALIDATED": "⛔",
}

# Short forms untuk transition chain (line 2) — pakai `-` bukan `_` agar tidak break Markdown italic
TRADE_STATE_ABBREV = {
    "READY_LONG": "READY", "TRIGGERED_LONG": "TRIGGER",
    "ACTIVE_TREND": "ACTIVE", "LATE_LONG": "LATE",
    "READY_SHORT": "RDY-SH", "TRIGGERED_SHORT": "TRIG-SH", "LATE_SHORT": "LATE-SH",
    "EARLY_UNDERFLOW": "UNDERFLOW",
    "NO_CONFIRMATION": "NO-CONF",
    "SHORT_COVERING_ONLY": "COVER", "DISTRIBUTION_RISK": "DIST-RISK",
    "EXIT_WARNING": "EXIT", "INVALIDATED": "INVALID",
    # pool states yang muncul di chain (sebagai origin_pool_setup_state pertama)
    "ARMED_INSIDE_RANGE": "ARMED", "WAKING_UP": "WAKING",
    "PRICE_BREAKOUT_CONFIRMED": "BREAKOUT",
    "EXTENDED_BREAKOUT": "EXT-BO", "SLEEPING_ACCUMULATION": "SLEEP",
    "UNKNOWN": "UNK",
}

ACTION_COMPACT = {
    "READY_LONG":          "🎯 Wait BO/retest",
    "TRIGGERED_LONG":      "🎯 Entry, stop below retest",
    "ACTIVE_TREND":        "🎯 Hold, retest only",
    "LATE_LONG":           "⛔ NO CHASE, retest only",
    "READY_SHORT":         "🎯 Wait lower low",
    "TRIGGERED_SHORT":     "🎯 Short, stop above reclaim",
    "LATE_SHORT":          "⛔ NO chase, wait bounce",
    "EARLY_UNDERFLOW":     "🎯 Wait OI accel + BO",
    "NO_CONFIRMATION":     "Watch only",
    "SHORT_COVERING_ONLY": "⛔ No long, wait OI flip",
    "DISTRIBUTION_RISK":   "⛔ Avoid long, watch reversal",
    "EXIT_WARNING":        "⛔ TP / tighten stop",
    "INVALIDATED":         "⛔ Skip",
}

ORIGIN_SHORT = {
    "momentum_chase": "momentum",
    "combined":       "combined",
    "ambush":         "ambush",
    "reversal":       "reversal",
    "heat":           "heat",
}


def _abbrev_transition(transition_str):
    """Convert 'ARMED_INSIDE_RANGE → READY_LONG → TRIGGERED_LONG'
    to 'ARMED→READY→TRIGGER' using TRADE_STATE_ABBREV."""
    if not transition_str:
        return "UNK"
    parts = [p.strip() for p in transition_str.split("→")]
    return "→".join(TRADE_STATE_ABBREV.get(p, p) for p in parts)


def _price_arrow(pct):
    if pct is None:
        return "➡️"
    if pct >= 0.5:
        return "📈"
    if pct <= -0.5:
        return "📉"
    return "➡️"


def _funding_icon(funding_rate_pct):
    """🧊 jika negative, 💸 jika positive/zero."""
    return "🧊" if (funding_rate_pct or 0) < 0 else "💸"


def _delta_arrow(price_delta, oi_delta):
    """Arrow ringkas untuk pasangan delta 1h."""
    if price_delta is None or oi_delta is None:
        return "↔"
    if price_delta >= 0 and oi_delta >= 0:
        return "↗"
    if price_delta <= 0 and oi_delta <= 0:
        return "↘"
    return "→"


def _fmt_delta_pair(p, o):
    """'+5/+18' atau 'N/A' jika prior snapshot belum ada."""
    if p is None or o is None:
        return "N/A"
    return f"{p:+.0f}/{o:+.0f}"


def _short_origins(origins):
    """List → 'momentum+combined+heat' atau '—' jika empty."""
    if not origins:
        return "—"
    return "+".join(ORIGIN_SHORT.get(o, o) for o in origins)


def _pretty_state(state):
    """Replace underscores with spaces for display (Markdown italic-safe)."""
    if not state:
        return "UNKNOWN"
    return state.replace("_", " ")


def bucket_of(trade_state):
    for bucket, states in LIFECYCLE_BUCKETS.items():
        if trade_state in states:
            return bucket
    return "AVOID"


def get_all_perp_symbols():
    """Fetch all USDT perpetual symbols."""
    info = api_get("/fapi/v1/exchangeInfo")
    if not info:
        return []
    return [s["symbol"] for s in info["symbols"]
            if s["quoteAsset"] == "USDT" 
            and s["contractType"] == "PERPETUAL"
            and s["status"] == "TRADING"]


def compute_breakout_state(current_price, low_price, high_price):
    """Compute (range_position_pct, distance_to_high_pct, breakout_state) from price + range.
    Returns (None, None, 'INVALID_RANGE') for degenerate ranges."""
    if high_price <= low_price or low_price <= 0 or current_price <= 0:
        return None, None, "INVALID_RANGE"

    range_position_pct = ((current_price - low_price) / (high_price - low_price)) * 100
    distance_to_high_pct = ((high_price - current_price) / current_price) * 100

    if range_position_pct < 25:
        state = "INSIDE_RANGE_LOW"
    elif range_position_pct < 60:
        state = "INSIDE_RANGE_MID"
    elif range_position_pct < 90:
        state = "INSIDE_RANGE_HIGH"
    elif range_position_pct <= 110:
        state = "BREAKOUT_ZONE"
    elif range_position_pct <= 125:
        state = "BREAKOUT_CONFIRMED"
    else:
        state = "EXTENDED_BREAKOUT"

    return range_position_pct, distance_to_high_pct, state


def compute_pool_setup_state(vol_breakout, breakout_state):
    """Derive pool_setup_state from volume expansion + price location in range."""
    if breakout_state == "INVALID_RANGE":
        return "SLEEPING_ACCUMULATION"
    if breakout_state == "EXTENDED_BREAKOUT":
        return "EXTENDED_BREAKOUT"
    if breakout_state in ("BREAKOUT_ZONE", "BREAKOUT_CONFIRMED"):
        return "PRICE_BREAKOUT_CONFIRMED"
    # Inside range cases: drive by volume expansion
    if vol_breakout < 1.5:
        return "SLEEPING_ACCUMULATION"
    if vol_breakout < 3:
        return "WAKING_UP"
    return "ARMED_INSIDE_RANGE"


def calculate_late_penalty(price_24h_change_pct, price_from_breakout_pct):
    """Negative score adjustment for tokens already extended.
    Returns 0 or a negative integer to subtract from entry_readiness_score."""
    penalty = 0
    if price_24h_change_pct is not None:
        if price_24h_change_pct > 30:
            penalty -= 10
        if price_24h_change_pct > 60:
            penalty -= 20
        if price_24h_change_pct > 100:
            penalty -= 40
    if price_from_breakout_pct is not None:
        if price_from_breakout_pct > 15:
            penalty -= 10
        if price_from_breakout_pct > 30:
            penalty -= 25
    return penalty


def compute_entry_readiness_score(breakout_state, range_position_pct, distance_to_high_pct,
                                   vol_breakout, price_24h_change_pct=None,
                                   price_from_breakout_pct=None, oi_1h_change_pct=None):
    """Composite 0-100 score gauging how close to a real entry trigger.
    Pool-mode callers pass only price/range/volume fields; OI mode tops it up with
    oi_1h_change_pct. Late penalty is always applied."""
    if breakout_state == "INVALID_RANGE":
        return 0

    # 1) Breakout progression — peaks in BREAKOUT_ZONE, drops back for EXTENDED
    progression_map = {
        "INSIDE_RANGE_LOW":     5,
        "INSIDE_RANGE_MID":     15,
        "INSIDE_RANGE_HIGH":    30,
        "BREAKOUT_ZONE":        45,
        "BREAKOUT_CONFIRMED":   35,
        "EXTENDED_BREAKOUT":    10,
    }
    score = progression_map.get(breakout_state, 0)

    # 2) Distance to high bonus (closer = better, only when still inside range)
    if breakout_state.startswith("INSIDE_RANGE") and distance_to_high_pct is not None:
        if distance_to_high_pct < 10:
            score += 15
        elif distance_to_high_pct < 30:
            score += 8
        elif distance_to_high_pct < 60:
            score += 3

    # 3) Volume breakout signal (max 25 pts at vol_x >= 3)
    if vol_breakout:
        score += min(vol_breakout / 3.0, 1.0) * 25

    # 4) OI confirmation (optional — only fed by OI mode)
    if oi_1h_change_pct is not None:
        if oi_1h_change_pct >= 15:
            score += 15
        elif oi_1h_change_pct >= 5:
            score += 8
        elif oi_1h_change_pct >= 2:
            score += 3

    # 5) Late penalty (always applied)
    score += calculate_late_penalty(price_24h_change_pct, price_from_breakout_pct)

    return max(0, min(100, round(score)))


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

    # === v2: Pool quality score (fundamentals only) ===
    pool_quality_score = round(total_score)

    # === v2: Breakout state + pool setup state ===
    current_price = data[-1]["close"]
    range_position_pct, distance_to_high_pct, breakout_state = compute_breakout_state(
        current_price, best_low, best_high
    )
    pool_setup_state = compute_pool_setup_state(vol_breakout, breakout_state)

    # === v2: Entry readiness score (parsial — tanpa OI delta saat pool scan) ===
    price_from_breakout_pct = None
    if best_high > 0 and current_price > best_high:
        price_from_breakout_pct = ((current_price - best_high) / best_high) * 100

    # price_24h_change_pct tidak tersedia di pool scan (perlu /fapi/v1/ticker/24hr)
    entry_readiness_score = compute_entry_readiness_score(
        breakout_state=breakout_state,
        range_position_pct=range_position_pct,
        distance_to_high_pct=distance_to_high_pct,
        vol_breakout=vol_breakout,
        price_24h_change_pct=None,
        price_from_breakout_pct=price_from_breakout_pct,
        oi_1h_change_pct=None,
    )

    return {
        "symbol": symbol,
        "coin": coin,
        "sideways_days": best_sideways,
        "range_pct": best_range,
        "slope_pct": best_slope_pct,
        "low_price": best_low,
        "high_price": best_high,
        "avg_vol": best_avg_vol,
        "current_price": current_price,
        "recent_vol": recent_vol,
        "vol_breakout": vol_breakout,
        "score": total_score,
        "status": status,
        "data_days": len(data),
        # v2 fields
        "range_position_pct": range_position_pct,
        "distance_to_high_pct": distance_to_high_pct,
        "breakout_state": breakout_state,
        "pool_setup_state": pool_setup_state,
        "pool_quality_score": pool_quality_score,
        "entry_readiness_score": entry_readiness_score,
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


def _pool_watch_hint(setup_state, breakout_state, distance_to_high_pct):
    """Per-bucket actionable hint for pool report rows."""
    if setup_state == "WAKING_UP":
        return "Watch: OI confirmation + price pressing resistance"
    if setup_state == "ARMED_INSIDE_RANGE":
        if distance_to_high_pct is not None and distance_to_high_pct < 15:
            return "Watch: very close to range high — wait for breakout + OI"
        return "Watch: wait for price breakout + OI acceleration"
    if setup_state == "PRICE_BREAKOUT_CONFIRMED":
        return "Watch: retest range high, avoid chase if extended"
    if setup_state == "EXTENDED_BREAKOUT":
        return "Watch: no new long. Retest only."
    return "Watch: still sleeping — keep monitoring"


def _format_pool_row(r):
    """Render one token row as exactly 2 lines (compact v2.1, Opsi A)."""
    pq = int(r.get("pool_quality_score") or round(r.get("score", 0)))
    er = int(r.get("entry_readiness_score") or 0)
    rp = r.get("range_position_pct")
    dh = r.get("distance_to_high_pct")
    ss = r.get("pool_setup_state") or "SLEEPING_ACCUMULATION"
    sw = r.get("sideways_days", 0) or 0
    rng = r.get("range_pct", 0) or 0
    vol_x = r.get("vol_breakout", 0) or 0

    rp_str = f"{rp:.0f}%" if rp is not None else "N/A"
    dh_str = f"{dh:+.0f}%" if dh is not None else "N/A"

    status_em, action_em, hint = POOL_STATE_VISUAL.get(ss, ("⚪", "•", ""))

    return [
        f"{status_em} **{r['coin']}** ▸ Q{pq} ER{er} ▸ "
        f"⏱{sw}d 📏{rng:.0f}% 📊{vol_x:.1f}× ▸ 📐{rp_str} 🎯{dh_str}",
        f"   {action_em} {_pretty_state(ss)} — {hint}",
    ]


def build_pool_report(results, top_n=25):
    """Build the accumulation-pool report grouped by pool_setup_state."""
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

    buckets = [
        ("PRICE_BREAKOUT_CONFIRMED", "🟢", "PRICE BREAKOUT CONFIRMED", "Retest watch"),
        ("ARMED_INSIDE_RANGE",       "🟠", "ARMED INSIDE RANGE",       "Volume anomaly but no price breakout yet"),
        ("WAKING_UP",                "🟡", "WAKING UP",                "Early volume wake-up"),
        ("EXTENDED_BREAKOUT",        "⚠️", "EXTENDED BREAKOUT",       "No new long, retest only"),
        ("SLEEPING_ACCUMULATION",    "💤", "SLEEPING ACCUMULATION",    "Keep monitoring"),
    ]

    # Group and sort each bucket by entry_readiness_score desc, then pool_quality_score desc
    grouped = {}
    for r in results:
        ss = r.get("pool_setup_state") or "SLEEPING_ACCUMULATION"
        grouped.setdefault(ss, []).append(r)
    for arr in grouped.values():
        arr.sort(key=lambda x: (x.get("entry_readiness_score", 0),
                                x.get("pool_quality_score", x.get("score", 0))), reverse=True)

    per_bucket_limit = {
        "PRICE_BREAKOUT_CONFIRMED": 10,
        "ARMED_INSIDE_RANGE":       10,
        "WAKING_UP":                10,
        "EXTENDED_BREAKOUT":        5,
        "SLEEPING_ACCUMULATION":    8,
    }

    any_shown = False
    for key, emoji, label, tagline in buckets:
        arr = grouped.get(key, [])
        if not arr:
            continue
        any_shown = True
        lines.append(f"{emoji} **{label}** ({len(arr)}) — {tagline}")
        limit = per_bucket_limit.get(key, 10)
        for r in arr[:limit]:
            lines.extend(_format_pool_row(r))
        if len(arr) > limit:
            lines.append(f"  ... +{len(arr) - limit} more in {label}")
        lines.append("")  # blank line antar bucket only

    if not any_shown:
        lines.append("(No candidates matched any setup state.)")

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
             low_price, high_price, current_price, score, status,
             range_position_pct, distance_to_high_pct, breakout_state,
             pool_setup_state, pool_quality_score, entry_readiness_score, vol_breakout)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (r["symbol"], r["coin"], now, r["sideways_days"], r["range_pct"],
             r["avg_vol"], r["low_price"], r["high_price"], r["current_price"],
             r["score"], r["status"],
             r.get("range_position_pct"), r.get("distance_to_high_pct"),
             r.get("breakout_state"), r.get("pool_setup_state"),
             r.get("pool_quality_score"), r.get("entry_readiness_score"),
             r.get("vol_breakout")))

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


def save_signals(conn, chase, combined, ambush, reversal, coin_data, pool_map, now_str,
                 trade_state_map=None, action_map=None, origin_pool_state_map=None):
    """Save top signals from each strategy to signal_tracker for performance tracking.

    v2: optional trade_state_map / action_map / origin_pool_state_map enrich
    each signal with lifecycle context.
    """
    c = conn.cursor()
    to_save = []
    seen = set()

    cutoff = (datetime.now(timezone(timedelta(hours=8))) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    c.execute("SELECT coin, signal_type FROM signal_tracker WHERE signal_time > ?", (cutoff,))
    for row in c.fetchall():
        seen.add((row[0], row[1]))

    trade_state_map = trade_state_map or {}
    action_map = action_map or {}
    origin_pool_state_map = origin_pool_state_map or {}

    def add_sig(coin, symbol, sig_type, price, score_val, rh=0, rl=0, n=""):
        key = (coin, sig_type)
        if key not in seen:
            ts = trade_state_map.get(symbol)
            act = action_map.get(symbol)
            origin = origin_pool_state_map.get(symbol)
            to_save.append((symbol, coin, sig_type, now_str, price, rh, rl, score_val, n,
                            ts, origin, act))
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
            (symbol, coin, signal_type, signal_time, signal_price, range_high, range_low,
             score, notes, trade_state, origin_pool_setup_state, action_label)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", item)

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
            display_name = sig_type.replace("_", " ")  # avoid Markdown italic break
            lines.append(f"  {emoji} {display_name}: {wins}/{total} ({rate:.0f}%)")

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

    # v2: breakdown by trade_state
    c.execute("""SELECT trade_state, COUNT(*),
                 SUM(CASE WHEN status='entered' THEN 1 ELSE 0 END),
                 SUM(CASE WHEN status='entered' AND entry_price > signal_price THEN 1 ELSE 0 END),
                 AVG(CASE WHEN status='entered' THEN (entry_price - signal_price) / signal_price * 100 END)
                 FROM signal_tracker
                 WHERE trade_state IS NOT NULL
                 GROUP BY trade_state
                 ORDER BY COUNT(*) DESC""")
    rows = c.fetchall()
    if rows:
        lines.append("")
        lines.append("By Trade State (v2):")
        for ts, total, entered, wins, avg_pnl in rows:
            wr = (wins / entered * 100) if entered else 0
            pnl_str = f"avg {avg_pnl:+.1f}%" if avg_pnl is not None else "N/A"
            lines.append(f"  {ts}: {total} total | {entered} entered | {wr:.0f}% WR | {pnl_str}")

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

            if text == "/btc":
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
            elif text == "/oi":
                if not review_sent:
                    print("[TG] /oi received, triggering OI scan...")
                    send_telegram_plain("⏳ Menjalankan OI scan... (tunggu ~60 detik)")
                    try:
                        subprocess.run(
                            [sys.executable, __file__, "oi"],
                            timeout=300,
                        )
                        print("[TG] /oi scan complete")
                    except subprocess.TimeoutExpired:
                        send_telegram_plain("⚠️ OI scan timeout setelah 5 menit.")
                    except Exception as e:
                        print(f"[TG] /oi scan failed: {e}")
                        send_telegram_plain(f"❌ OI scan gagal: {e}")
                    review_sent = True
                LAST_TG_UPDATE_ID = update["update_id"]
            elif text == "/help":
                send_telegram_plain(
                    "Commands:\n"
                    "/oi - Force run OI scan sekarang\n"
                    "/btc - Today's BTC bias brief\n"
                    "/review - Signal tracker performance report\n"
                    "/help - Show this help message"
                )
                LAST_TG_UPDATE_ID = update["update_id"]
            else:
                LAST_TG_UPDATE_ID = update["update_id"]

        if LAST_TG_UPDATE_ID > stored_id:
            set_app_state(conn, "last_tg_update_id", str(LAST_TG_UPDATE_ID))
    except Exception as e:
        print(f"[TG] Command check error: {e}")


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
        else:
            existing = load_watchlist_symbols(conn)
            if existing:
                send_telegram(
                    f"⚠️ *Pool scan gagal* (SSL/network error)\n"
                    f"Menggunakan watchlist lama: {len(existing)} simbol.\n"
                    f"OI scan tetap berjalan."
                )
                print(f"[pool] scan failed, keeping existing {len(existing)} watchlist entries")
            else:
                send_telegram("❌ Pool scan gagal & watchlist kosong. OI scan dilewati.")
    
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

        # ═══════════════════════════════════════
        # v2: Origin strategy tagging per coin
        # ═══════════════════════════════════════
        chase_set = {s["coin"] for s in chase}
        combined_set = {s["coin"] for s in combined}
        ambush_set = {s["coin"] for s in ambush}
        reversal_set = {s["coin"] for s in reversal}
        origin_map = {}
        for sym, d in coin_data.items():
            coin = d["coin"]
            origins = []
            if coin in chase_set:    origins.append("momentum_chase")
            if coin in combined_set: origins.append("combined")
            if coin in ambush_set:   origins.append("ambush")
            if coin in reversal_set: origins.append("reversal")
            if d.get("in_cg") or d.get("vol_surge"):
                origins.append("heat")
            origin_map[sym] = origins

        # ═══════════════════════════════════════
        # v2: Load full pool data (with v2 fields) for lifecycle classifier
        # ═══════════════════════════════════════
        c2.execute("""SELECT symbol, low_price, high_price, current_price,
                             sideways_days, range_pct, avg_vol, vol_breakout,
                             breakout_state, pool_setup_state,
                             distance_to_high_pct, range_position_pct,
                             pool_quality_score, entry_readiness_score
                      FROM watchlist""")
        pool_v2_map = {}
        for row in c2.fetchall():
            pool_v2_map[row[0]] = {
                "symbol": row[0],
                "low_price": row[1], "high_price": row[2], "current_price": row[3],
                "sideways_days": row[4], "range_pct": row[5], "avg_vol": row[6],
                "vol_breakout": row[7], "breakout_state": row[8],
                "pool_setup_state": row[9], "distance_to_high_pct": row[10],
                "range_position_pct": row[11], "pool_quality_score": row[12],
                "entry_readiness_score": row[13],
            }

        # ═══════════════════════════════════════
        # v2: Lifecycle classification + snapshot persistence per coin
        # ═══════════════════════════════════════
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        now_wib = datetime.now(timezone(timedelta(hours=8)))
        # Limit lifecycle classification to interesting coins (in pool or matched a strategy)
        interesting_syms = set()
        for sym, d in coin_data.items():
            if d.get("in_pool") or d.get("heat", 0) > 0 or origin_map.get(sym):
                interesting_syms.add(sym)
            # Also include any coin with notable OI change
            if abs(d.get("d6h", 0)) >= 5:
                interesting_syms.add(sym)

        lifecycle_results = []  # list of (coin_data_dict, classification_dict)
        for sym in interesting_syms:
            d = coin_data[sym]
            pool_row = pool_v2_map.get(sym)
            prior_1h = get_prior_snapshot(conn, sym, 1)
            prior_3h = get_prior_snapshot(conn, sym, 3)
            cls = classify_trade_state(d, prior_1h, prior_3h, pool_row)
            cls["origin_strategies"] = origin_map.get(sym, [])
            lifecycle_results.append((d, cls))

            # Persist current snapshot
            save_hourly_snapshot(conn, {
                "symbol": sym,
                "timestamp": now_iso,
                "price": d.get("price"),
                "price_24h_change_pct": d.get("px_chg"),
                "open_interest": d.get("oi_usd"),
                "oi_change_pct_from_baseline": d.get("d6h"),
                "funding_rate": d.get("fr_pct", 0) / 100.0 if d.get("fr_pct") is not None else None,
                "volume_24h": None,
                "quote_volume_24h": d.get("vol"),
                "pool_setup_state": (pool_row or {}).get("pool_setup_state"),
                "breakout_state": (pool_row or {}).get("breakout_state"),
                "trade_state": cls["trade_state"],
                "action": cls["action"],
                "origin_strategies": cls["origin_strategies"],
            })

        # Prune old snapshots (>7 days)
        try:
            prune_old_snapshots(conn, days_to_keep=7)
        except Exception as e:
            print(f"[snapshot] prune failed: {e}")

        # ═══════════════════════════════════════
        # 5.5 Save signals to tracker and check pending breakouts (v2-aware)
        # ═══════════════════════════════════════
        now_str = now_wib.strftime("%Y-%m-%d %H:%M")
        trade_state_map = {}
        action_map = {}
        origin_pool_state_map = {}
        for d, cls in lifecycle_results:
            trade_state_map[d["sym"]] = cls["trade_state"]
            action_map[d["sym"]] = cls["action"]
            origin_pool_state_map[d["sym"]] = cls.get("origin_pool_setup_state")

        save_signals(conn, chase, combined, ambush, reversal, coin_data, pool_map, now_str,
                     trade_state_map=trade_state_map, action_map=action_map,
                     origin_pool_state_map=origin_pool_state_map)
        check_breakouts(conn, ticker_map)

        # ═══════════════════════════════════════
        # v2: New lifecycle-bucketed output
        # ═══════════════════════════════════════
        def mcap_str(v):
            if v >= 1e6: return f"${v/1e6:.0f}M"
            if v >= 1e3: return f"${v/1e3:.0f}K"
            return f"${v:.0f}"

        def fmt_delta(x, suffix="%"):
            return f"{x:+.1f}{suffix}" if x is not None else "N/A"

        # Bucket the lifecycle results
        buckets = {"ACTIONABLE": [], "ALERT": [], "ACTIVE_LATE": [], "AVOID": []}
        for d, cls in lifecycle_results:
            buckets[bucket_of(cls["trade_state"])].append((d, cls))

        # Sort: actionable/alert by entry_readiness_score desc, late by price_24h desc
        def sort_actionable(item):
            d, cls = item
            pool = pool_v2_map.get(d["sym"], {})
            return pool.get("entry_readiness_score") or 0
        def sort_late(item):
            d, _cls = item
            return d.get("px_chg") or 0
        buckets["ACTIONABLE"].sort(key=sort_actionable, reverse=True)
        buckets["ALERT"].sort(key=sort_actionable, reverse=True)
        buckets["ACTIVE_LATE"].sort(key=sort_late, reverse=True)
        buckets["AVOID"].sort(key=sort_late, reverse=True)

        def render_token_block(d, cls, pool_row):
            """Render one OI token as exactly 2 lines (compact v2.1, Opsi B)."""
            state = cls["trade_state"]
            status_em = TRADE_STATE_EMOJI.get(state, "•")

            # Line 1 metrics
            px24 = d.get("px_chg", 0) or 0
            oi6h = d.get("d6h", 0) or 0
            fr_pct = d.get("fr_pct", 0) or 0
            p_arrow = _price_arrow(px24)
            f_icon = _funding_icon(fr_pct)

            p_1h = cls.get("price_1h_change_pct")
            o_1h = cls.get("oi_1h_change_pct")
            one_h_arrow = _delta_arrow(p_1h, o_1h)
            one_h_str = _fmt_delta_pair(p_1h, o_1h)

            line1 = (
                f"{status_em} **{d['coin']}** {_pretty_state(state)} ▸ "
                f"{p_arrow}{px24:+.0f}% 💰{oi6h:+.0f}% {f_icon}{fr_pct:+.2f}% ▸ "
                f"1h{one_h_arrow}{one_h_str}"
            )

            # Line 2: transition + via + action
            trans = _abbrev_transition(cls.get("transition") or "UNK")
            via = _short_origins(cls.get("origin_strategies") or [])
            action = ACTION_COMPACT.get(state, cls.get("action", ""))

            line2 = f"   {trans} via {via} ▸ {action}"

            return [line1, line2]

        lines = [
            f"🏦 **Smart Money Radar** - Hourly Progression",
            f"⏰ {now_wib.strftime('%Y-%m-%d %H:%M')} WIB",
            f"━━━━━━━━━━━━━━━━━━",
        ]

        section_meta = [
            ("ACTIONABLE",   "📍", "ACTIONABLE NOW",       "READY / TRIGGERED",                       10),
            ("ALERT",        "🟠", "ALERT / NEXT SETUP",   "Early underflow / building",              8),
            ("ACTIVE_LATE",  "🔥", "ACTIVE BUT LATE",      "Trending or extended — no fresh chase",   8),
            ("AVOID",        "⚠️", "AVOID / DEPRIORITIZE", "No confirmation / covering / exit",       6),
        ]

        rendered_any = False
        for bucket_key, emoji, title, tagline, limit in section_meta:
            items = buckets[bucket_key]
            if not items:
                continue
            rendered_any = True
            lines.append("")
            lines.append(f"{emoji} **{title}** ({len(items)}) — {tagline}")
            for d, cls in items[:limit]:
                pool_row = pool_v2_map.get(d["sym"])
                lines.extend(render_token_block(d, cls, pool_row))
            if len(items) > limit:
                lines.append(f"   ... +{len(items) - limit} more in {title}")

        if not rendered_any:
            lines.append("(No tokens classified this hour.)")

        # PREV WATCHED: tokens that were EARLY_UNDERFLOW in last 6h but have since changed state
        cutoff_6h = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S")

        # Find tokens whose latest snapshot is NOT EARLY_UNDERFLOW, but had EARLY_UNDERFLOW in last 6h
        _prev_cur = conn.cursor()
        _prev_cur.row_factory = sqlite3.Row
        prev_rows = _prev_cur.execute("""
            SELECT h1.symbol, h1.trade_state AS latest_state
            FROM hourly_token_snapshots h1
            WHERE h1.timestamp = (
                SELECT MAX(h2.timestamp) FROM hourly_token_snapshots h2
                WHERE h2.symbol = h1.symbol
            )
            AND h1.trade_state != 'EARLY_UNDERFLOW'
            AND h1.symbol IN (
                SELECT DISTINCT symbol FROM hourly_token_snapshots
                WHERE trade_state = 'EARLY_UNDERFLOW' AND timestamp >= ?
            )
            ORDER BY h1.timestamp DESC
        """, (cutoff_6h,)).fetchall()

        if prev_rows:
            prev_lines = []
            for row in prev_rows:
                sym = row["symbol"]
                latest_state = row["latest_state"]

                if latest_state in ("ACTIVE_TREND", "LATE_LONG", "LATE_SHORT"):
                    label = "sudah lari ❌ missed"
                elif latest_state in ("NO_CONFIRMATION", "INVALIDATED", "SHORT_COVERING_ONLY", "DISTRIBUTION_RISK", "EXIT_WARNING"):
                    label = "OI turun, skip"
                elif latest_state in ("READY_LONG", "READY_SHORT", "TRIGGERED_LONG", "TRIGGERED_SHORT"):
                    label = "lihat ACTIONABLE ✅"
                else:
                    label = latest_state.lower().replace("_", " ")

                prev_lines.append(f"   {sym:<10} UNDER→{latest_state:<20} — {label}")

            lines.append("")
            lines.append(f"━━━━━━━━━━━━━━━━━━")
            lines.append(f"📋 **PREV WATCHED** (EARLY_UNDERFLOW 6h lalu → sekarang)")
            lines.extend(prev_lines)

        report = "\n".join(lines)

        # Append signal tracking recap if there are tracked signals
        tracking_recap = build_tracking_recap(conn)
        if tracking_recap:
            report += "\n" + tracking_recap
        send_telegram(report)
        if TG_POLL_COMMANDS_IN_OI:
            check_telegram_commands(conn)
    
    if mode == "btc":
        conn.close()
        generate_btc_brief()
        print("\n✅ BTC brief complete")
        return

    if mode == "review":
        review_signals(conn)
        print("\n✅ Review complete")
        conn.close()
        return

    if mode == "listen":
        print("[listen] Telegram command listener started (polling every 10s)")
        while True:
            try:
                check_telegram_commands(conn)
            except Exception as e:
                print(f"[listen] Error: {e}")
            time.sleep(10)

    conn.close()
    print("\n✅ Done")


if __name__ == "__main__":
    main()
