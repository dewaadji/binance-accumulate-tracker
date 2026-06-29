"""setup_engine.py — Supply/Demand zone detection + actionable trade setup generation.

Dependencies: requests (same as main project).
Integrasi: set_fetch_fn() dipanggil dari accumulation_radar.py sebelum generate_setups().
"""

import time
import json
import requests as _req
from datetime import datetime, timezone, timedelta

FAPI = "https://fapi.binance.com"
KLINE_CACHE_4H = {}
KLINE_CACHE_15M = {}
_fetch_fn = None


def set_fetch_fn(fn):
    global _fetch_fn
    _fetch_fn = fn


def clear_caches():
    KLINE_CACHE_4H.clear()
    KLINE_CACHE_15M.clear()


def _get_klines(symbol, interval, limit):
    cache = None
    if interval == "4h":
        cache = KLINE_CACHE_4H
    elif interval == "15m":
        cache = KLINE_CACHE_15M

    cache_key = f"{symbol}:{interval}:{limit}"
    if cache and cache_key in cache:
        return cache[cache_key]

    if _fetch_fn:
        result = _fetch_fn(symbol, interval, limit)
    else:
        url = f"{FAPI}/fapi/v1/klines"
        try:
            resp = _req.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10)
            result = resp.json()
        except Exception:
            return None

    if cache and isinstance(result, list):
        cache[cache_key] = result
    return result


def _avg_range(klines, period=14):
    if not klines or len(klines) < period:
        return 0
    ranges = [float(k[2]) - float(k[3]) for k in klines[-period:]]
    return sum(ranges) / len(ranges)


def get_atr(klines, period=14):
    if not klines or len(klines) < period + 1:
        return None
    closes = [float(k[4]) for k in klines]
    tr_values = []
    for i in range(1, period + 1):
        h, l = float(klines[i][2]), float(klines[i][3])
        pc = closes[i - 1]
        tr_values.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(tr_values) / period
    for i in range(period + 1, len(klines)):
        h, l = float(klines[i][2]), float(klines[i][3])
        pc = closes[i - 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        atr = (atr * (period - 1) + tr) / period
    return atr


def detect_swing_points(klines, lookback=5):
    if not klines or len(klines) < lookback * 2 + 1:
        return [], []

    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    swing_highs, swing_lows = [], []

    for i in range(lookback, len(klines) - lookback):
        if all(highs[i] > highs[i - j] for j in range(1, lookback + 1)) and \
           all(highs[i] >= highs[i + j] for j in range(1, lookback + 1)):
            swing_highs.append((i, highs[i], int(klines[i][0])))
        if all(lows[i] < lows[i - j] for j in range(1, lookback + 1)) and \
           all(lows[i] <= lows[i + j] for j in range(1, lookback + 1)):
            swing_lows.append((i, lows[i], int(klines[i][0])))

    return swing_highs, swing_lows


def _days_since(ts_ms):
    ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 86400


def detect_demand_zones(klines):
    if not klines or len(klines) < 20:
        return []

    avg_r = _avg_range(klines, 14)
    if avg_r <= 0:
        return []

    zones = []
    for i in range(1, len(klines)):
        impulse_range = float(klines[i][2]) - float(klines[i][3])
        is_bullish = float(klines[i][4]) > float(klines[i][1])
        if impulse_range > avg_r * 2.0 and is_bullish:
            base = klines[i - 1]
            base_vol = float(base[5])
            impulse_vol = float(klines[i][5])
            vol_ratio = impulse_vol / base_vol if base_vol > 0 else 1
            zones.append({
                "type": "demand",
                "bottom": float(base[3]),
                "top": float(base[2]),
                "impulse_idx": i,
                "impulse_range": impulse_range,
                "vol_ratio": vol_ratio,
                "days_since": _days_since(int(base[0])),
            })
    return zones


def detect_supply_zones(klines):
    if not klines or len(klines) < 20:
        return []

    avg_r = _avg_range(klines, 14)
    if avg_r <= 0:
        return []

    zones = []
    for i in range(1, len(klines)):
        impulse_range = float(klines[i][2]) - float(klines[i][3])
        is_bearish = float(klines[i][4]) < float(klines[i][1])
        if impulse_range > avg_r * 2.0 and is_bearish:
            base = klines[i - 1]
            base_vol = float(base[5])
            impulse_vol = float(klines[i][5])
            vol_ratio = impulse_vol / base_vol if base_vol > 0 else 1
            zones.append({
                "type": "supply",
                "bottom": float(base[3]),
                "top": float(base[2]),
                "impulse_idx": i,
                "impulse_range": impulse_range,
                "vol_ratio": vol_ratio,
                "days_since": _days_since(int(base[0])),
            })
    return zones


def score_zone(zone, current_price, avg_range=None):
    strength = 0
    vol_r = zone.get("vol_ratio", 1)
    if vol_r >= 5:
        strength += 4
    elif vol_r >= 3:
        strength += 3
    elif vol_r >= 2:
        strength += 2
    elif vol_r >= 1.5:
        strength += 1

    if avg_range and avg_range > 0:
        impulse_r = zone["impulse_range"] / avg_range
        if impulse_r >= 4:
            strength += 4
        elif impulse_r >= 3:
            strength += 3
        elif impulse_r >= 2.5:
            strength += 2
        else:
            strength += 1

    days = zone.get("days_since", 99)
    if days < 7:
        freshness = "fresh"
        strength += 2
    elif days < 21:
        freshness = "normal"
        strength += 1
    else:
        freshness = "stale"

    zone_width = zone["top"] - zone["bottom"]
    if zone_width > 0:
        dist_to_top = abs(current_price - zone["top"])
        if dist_to_top < zone_width * 2:
            strength += 1

    if strength >= 8:
        label = "strong"
    elif strength >= 4:
        label = "medium"
    else:
        label = "weak"

    return strength, freshness, label


def generate_entry_sl(symbol, direction, zone, atr_4h, coin_data, pool_row):
    zone_top = zone["top"]
    zone_bottom = zone["bottom"]
    zone_width = zone_top - zone_bottom

    if direction == "long":
        entry_low = zone_bottom
        entry_high = zone_bottom + zone_width * 0.5
        entry_mid = (entry_low + entry_high) / 2
        sl = zone_bottom - (atr_4h * 0.5) if atr_4h and atr_4h > 0 else zone_bottom * 0.98
    else:
        entry_low = zone_top - zone_width * 0.5
        entry_high = zone_top
        entry_mid = (entry_low + entry_high) / 2
        sl = zone_top + (atr_4h * 0.5) if atr_4h and atr_4h > 0 else zone_top * 1.02

    sl_pct = abs((sl - entry_mid) / entry_mid) * 100 if entry_mid > 0 else 0

    return {
        "entry_low": entry_low,
        "entry_high": entry_high,
        "entry_mid": entry_mid,
        "stop_loss": sl,
        "sl_pct": round(sl_pct, 2),
    }


def generate_fallback_setup(symbol, direction, coin_data, pool_row, atr_4h, swing_lows, swing_highs):
    current_price = coin_data.get("price", 0)
    if not current_price or current_price <= 0:
        return None

    if direction == "long":
        if pool_row:
            range_low = float(pool_row.get("low_price", 0))
            if range_low > 0 and range_low < current_price:
                entry_low = range_low
                entry_high = range_low * 1.05
            else:
                entry_low = current_price * 0.97
                entry_high = current_price * 0.995
        else:
            entry_low = current_price * 0.97
            entry_high = current_price * 0.995

        if swing_lows:
            below = [sl for sl in swing_lows if sl[1] < entry_low]
            nearest_sl = max(below, key=lambda x: x[1]) if below else None
            sl = nearest_sl[1] * 0.995 if nearest_sl else entry_low * 0.95
        else:
            sl = entry_low * 0.95
    else:
        if pool_row:
            range_high = float(pool_row.get("high_price", 0))
            if range_high > 0 and range_high > current_price:
                entry_low = range_high * 0.95
                entry_high = range_high
            else:
                entry_low = current_price * 1.005
                entry_high = current_price * 1.03
        else:
            entry_low = current_price * 1.005
            entry_high = current_price * 1.03

        if swing_highs:
            above = [sh for sh in swing_highs if sh[1] > entry_high]
            nearest_sh = min(above, key=lambda x: x[1]) if above else None
            sl = nearest_sh[1] * 1.005 if nearest_sh else entry_high * 1.05
        else:
            sl = entry_high * 1.05

    entry_mid = (entry_low + entry_high) / 2
    sl_pct = abs((sl - entry_mid) / entry_mid) * 100 if entry_mid > 0 else 0

    return {
        "entry_low": round(entry_low, 6),
        "entry_high": round(entry_high, 6),
        "entry_mid": round(entry_mid, 6),
        "stop_loss": round(sl, 6),
        "sl_pct": round(sl_pct, 2),
    }


def validate_15m_confirmation(symbol, zone, direction):
    klines = _get_klines(symbol, "15m", 24)
    if not klines or len(klines) < 6:
        return "no data"

    zone_top = zone["top"]
    zone_bottom = zone["bottom"]
    last_6 = klines[-6:]
    closes = [float(k[4]) for k in last_6]
    opens = [float(k[1]) for k in last_6]
    lows = [float(k[3]) for k in last_6]
    highs = [float(k[2]) for k in last_6]
    vols = [float(k[5]) for k in last_6]
    avg_vol = sum(vols) / len(vols) if vols else 0

    if direction == "long":
        for i in range(len(last_6)):
            if zone_bottom * 0.98 <= lows[i] <= zone_top * 1.02:
                if i < len(last_6) - 1:
                    if closes[i + 1] > opens[i + 1] and closes[i + 1] > highs[i]:
                        if avg_vol > 0 and vols[i] > avg_vol * 1.3:
                            return "confirmed"
                        return "retest+bounce"
        latest_low = lows[-1]
        if zone_bottom * 0.98 <= latest_low <= zone_top * 1.02:
            return "retesting" if closes[-1] < opens[-1] else "retesting+bounce"
        if closes[-1] > zone_top:
            return "above zone"
        return "no confirmation"
    else:
        for i in range(len(last_6)):
            if zone_bottom * 0.98 <= highs[i] <= zone_top * 1.02:
                if i < len(last_6) - 1:
                    if closes[i + 1] < opens[i + 1] and closes[i + 1] < lows[i]:
                        if avg_vol > 0 and vols[i] > avg_vol * 1.3:
                            return "confirmed"
                        return "retest+reject"
        latest_high = highs[-1]
        if zone_bottom * 0.98 <= latest_high <= zone_top * 1.02:
            return "retesting" if closes[-1] > opens[-1] else "retesting+reject"
        if closes[-1] < zone_bottom:
            return "below zone"
        return "no confirmation"


def _build_reasoning(best_zone, direction, state, cls, d, pool_row):
    parts = []
    if best_zone:
        str_label = best_zone.get("_strength_label", "medium")
        freshness = best_zone.get("_freshness", "normal")
        ztype = "Demand" if direction == "long" else "Supply"
        parts.append(f"{ztype} zone {freshness} ({str_label})")
    else:
        parts.append("No clear zone — accumulation range fallback")

    oi_1h = cls.get("oi_1h_change_pct")
    if oi_1h is not None:
        parts.append(f"OI 1h {oi_1h:+.0f}%")
    px24 = d.get("px_chg")
    if px24 is not None:
        parts.append(f"24h {px24:+.0f}%")
    return " | ".join(parts)


def _fmt_price(p):
    if p >= 1000:
        return f"${p:,.0f}"
    if p >= 1:
        return f"${p:.2f}"
    if p >= 0.01:
        return f"${p:.4f}"
    return f"${p:.8f}"


_FRESH_ICON = {"fresh": "⭐", "normal": "•", "stale": "☁️"}
_CONFIRM_ICON = {
    "confirmed": "✅", "retest+bounce": "🟢", "retest+reject": "🔻",
    "retesting": "⏳", "retesting+bounce": "🟢", "retesting+reject": "🔻",
    "above zone": "⬆️", "below zone": "⬇️", "no confirmation": "❌",
    "no data": "⚠️",
}


def format_setup_report(setups):
    if not setups:
        return ""

    lines = ["", "━━━ SETUP SIGNALS ━━━"]
    for s in setups:
        emoji_d = "🟢" if s["direction"] == "long" else "🔻"
        st = s["state"].replace("_", " ")
        lines.append(f"{emoji_d} **{s['coin']}** — {s['direction'].upper()} {st}")

        z = s.get("zone")
        if z and s["zone_type"] == "zone":
            fi = _FRESH_ICON.get(z.get("_freshness", ""), "")
            lines.append(f"   {'DZ' if s['direction']=='long' else 'SZ'} {fi} {z.get('_freshness','')} ({z.get('_strength_label','')})")

        ep = _fmt_price
        lines.append(f"   Entry: {ep(s['entry_low'])}-{ep(s['entry_high'])} | SL: {ep(s['stop_loss'])} ({s['sl_pct']:.1f}%)")

        cf = s.get("fifteen_m", "N/A")
        ci = _CONFIRM_ICON.get(cf, "")
        lines.append(f"   15m: {ci}{cf} | OI 1h: {s.get('oi_1h',0):+.0f}%")

        reason = s.get("reasoning", "")
        if reason:
            lines.append(f"   {reason}")
        lines.append("━━━━━━━━━━━━")

    return "\n".join(lines)


def generate_setups(lifecycle_results, coin_data, pool_v2_map):
    setups = []

    for d, cls in lifecycle_results:
        state = cls["trade_state"]
        if state not in ("READY_LONG", "TRIGGERED_LONG", "READY_SHORT", "TRIGGERED_SHORT", "EARLY_UNDERFLOW"):
            continue

        sym = d["sym"]
        direction = "long" if state in ("READY_LONG", "TRIGGERED_LONG", "EARLY_UNDERFLOW") else "short"
        pool_row = pool_v2_map.get(sym)
        current_price = d.get("price", 0)
        if not current_price or current_price <= 0:
            continue

        klines_4h = _get_klines(sym, "4h", 180)
        if not klines_4h or len(klines_4h) < 30:
            continue

        atr_4h = get_atr(klines_4h, 14)
        swing_highs, swing_lows = detect_swing_points(klines_4h, 5)
        avg_r = _avg_range(klines_4h, 14)

        demand_zones = detect_demand_zones(klines_4h)
        supply_zones = detect_supply_zones(klines_4h)

        best_zone = None
        if direction == "long":
            valid = [z for z in demand_zones if z["top"] < current_price and z["bottom"] > current_price * 0.80]
            for z in valid:
                sc, fr, lb = score_zone(z, current_price, avg_r)
                z["_strength"] = sc
                z["_freshness"] = fr
                z["_strength_label"] = lb
            valid.sort(key=lambda x: x["_strength"], reverse=True)
            best_zone = valid[0] if valid else None
        else:
            valid = [z for z in supply_zones if z["bottom"] > current_price and z["top"] < current_price * 1.20]
            for z in valid:
                sc, fr, lb = score_zone(z, current_price, avg_r)
                z["_strength"] = sc
                z["_freshness"] = fr
                z["_strength_label"] = lb
            valid.sort(key=lambda x: x["_strength"], reverse=True)
            best_zone = valid[0] if valid else None

        if best_zone:
            es = generate_entry_sl(sym, direction, best_zone, atr_4h, d, pool_row)
            fm = validate_15m_confirmation(sym, best_zone, direction)
            zt = "zone"
        else:
            es = generate_fallback_setup(sym, direction, d, pool_row, atr_4h, swing_lows, swing_highs)
            if not es:
                continue
            fm = "N/A (fallback)"
            zt = "fallback"

        setups.append({
            "symbol": sym,
            "coin": d["coin"],
            "direction": direction,
            "state": state,
            "zone_type": zt,
            "zone": best_zone,
            "entry_low": es["entry_low"],
            "entry_high": es["entry_high"],
            "entry_mid": es["entry_mid"],
            "stop_loss": es["stop_loss"],
            "sl_pct": es["sl_pct"],
            "fifteen_m": fm,
            "oi_1h": cls.get("oi_1h_change_pct"),
            "price_24h": d.get("px_chg"),
            "reasoning": _build_reasoning(best_zone, direction, state, cls, d, pool_row),
        })

    return setups
