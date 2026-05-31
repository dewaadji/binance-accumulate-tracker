# TASK: Upgrade Crypto Accumulation Pool + Hourly OI Monitoring Bot

Kamu akan memperbaiki bot monitoring teknikal token Binance Futures yang sudah berjalan. Bot ini memiliki dua workflow utama:

1. **Daily Pool Scan** — dijalankan setiap pagi untuk mencari universe token potensial berdasarkan sideways, range, dan volume anomaly.
2. **Hourly OI Monitor** — dijalankan tiap jam untuk memantau apakah token dari pool berkembang menjadi setup trade, gagal, atau sudah terlambat.

Tujuan upgrade ini adalah mengubah bot dari sekadar scanner/ranking menjadi **decision assistant** yang bisa memberi status tindakan:

- `WATCH`
- `ALERT`
- `READY`
- `TRIGGERED`
- `ACTIVE_TREND`
- `LATE`
- `NO_CONFIRMATION`
- `EXIT_WARNING`
- `INVALIDATED`

Bot tidak boleh memberi kesan bahwa skor tinggi otomatis berarti entry. Semua output harus membedakan antara **pool quality**, **entry readiness**, dan **latest trade action**.

---

# 1. Upgrade Daily Pool Scan

## Masalah saat ini

Kategori lama seperti:

- `Volume Breakout`
- `Volume Picking Up`
- `Accumulating`

terlalu ambigu. Contohnya, token yang volume-nya naik tetapi harga masih di dalam range tercampur dengan token yang harga-nya sudah breakout dari range.

## Perubahan yang harus dibuat

Tambahkan metrik berikut untuk setiap kandidat pool:

### 1.1 Range Position %

Formula:
range_position_pct = ((current_price - range_low) / (range_high - range_low)) * 100

Interpretasi:
0–25%     = INSIDE_RANGE_LOW
25–60%    = INSIDE_RANGE_MID
60–90%    = INSIDE_RANGE_HIGH
90–110%   = BREAKOUT_ZONE
>110%     = EXTENDED

Handle edge case:
if range_high <= range_low:
    range_position_pct = None
    breakout_state = "INVALID_RANGE"

### 1.2 Distance to Range High%

Formula:
distance_to_high_pct = ((range_high - current_price) / current_price) * 100

Interpretasi:
>30%    = masih jauh dari breakout
10–30%  = mulai layak dipantau
0–10%   = dekat trigger
<0%     = sudah di atas range high

### 1.3 Breakout State
Buat field baru:
breakout_state

Nilai yang digunakan:
INSIDE_RANGE_LOW
INSIDE_RANGE_MID
INSIDE_RANGE_HIGH
BREAKOUT_ZONE
BREAKOUT_CONFIRMED
EXTENDED_BREAKOUT
INVALID_RANGE

Rule:
if range_high <= range_low:
    breakout_state = "INVALID_RANGE"
elif range_position_pct < 25:
    breakout_state = "INSIDE_RANGE_LOW"
elif range_position_pct < 60:
    breakout_state = "INSIDE_RANGE_MID"
elif range_position_pct < 90:
    breakout_state = "INSIDE_RANGE_HIGH"
elif range_position_pct <= 110:
    breakout_state = "BREAKOUT_ZONE"
elif range_position_pct <= 125:
    breakout_state = "BREAKOUT_CONFIRMED"
else:
    breakout_state = "EXTENDED_BREAKOUT"

### 1.4 Pool Setup State
Buat field:
pool_setup_state

Nilai:
SLEEPING_ACCUMULATION
WAKING_UP
ARMED_INSIDE_RANGE
PRICE_BREAKOUT_CONFIRMED
EXTENDED_BREAKOUT

Rule dasar:
if volume_x < 1.5:
    pool_setup_state = "SLEEPING_ACCUMULATION"

elif 1.5 <= volume_x < 3 and breakout_state.startswith("INSIDE_RANGE"):
    pool_setup_state = "WAKING_UP"

elif volume_x >= 3 and breakout_state.startswith("INSIDE_RANGE"):
    pool_setup_state = "ARMED_INSIDE_RANGE"

elif breakout_state in ["BREAKOUT_ZONE", "BREAKOUT_CONFIRMED"]:
    pool_setup_state = "PRICE_BREAKOUT_CONFIRMED"

elif breakout_state == "EXTENDED_BREAKOUT":
    pool_setup_state = "EXTENDED_BREAKOUT"

Catatan penting:
- WAKING_UP tidak boleh dianggap lebih rendah kualitasnya daripada ARMED_INSIDE_RANGE.
- Token WAKING_UP sering menjadi kandidat lebih awal sebelum pump besar.
- ARMED_INSIDE_RANGE artinya token menarik, tetapi belum entry.
- PRICE_BREAKOUT_CONFIRMED artinya breakout sudah terjadi; cari retest, jangan chase.
- EXTENDED_BREAKOUT artinya jangan buka long baru tanpa pullback.

# 2. Pisahkan Pool Quality Score dan Entry Readiness Score
Jangan hanya pakai satu score.

### 2.1 Pool Quality Score
Tujuan:
Apakah token layak masuk universe monitoring?

Komponen yang boleh digunakan:
- sideways days
- range clarity
- average daily volume
- volume anomaly
- market cap / liquidity, jika tersedia

Output:
pool_quality_score

### 2.2 Entry Readiness Score
Tujuan:
Apakah token sudah dekat dengan trigger entry?

Komponen:
- range position
- distance to range high
- breakout state
- volume continuation
- OI confirmation dari hourly monitor
- funding condition
- taker buy/sell dominance, jika tersedia

Output:
entry_readiness_score

Untuk daily pool, jika data OI belum masuk, entry readiness boleh dihitung parsial.

# 3. Daily Pool Output Format Baru
Ubah output pool agar lebih actionable.

Contoh format:
🏦 Accumulation Radar - Pool Update
⏰ 2026-05-29 08:03 WIB
━━━━━━━━━━━━━━━━━━
Scanned 256 contracts. Candidates found:

🟡 WAKING UP - Early volume wake-up
  HEI | PoolQ:80 | EntryReady:42
     Sideways 111d | Range 75% | Vol 2.1x
     Range Pos: 58.2% | Distance High: 18.4%
     State: WAKING_UP
     Watch: OI +15% and price pressing resistance

🟠 ARMED INSIDE RANGE - Volume anomaly but no price breakout yet
  TRUST | PoolQ:81 | EntryReady:37
     Sideways 113d | Range 74% | Vol 3.6x
     Price: $0.065930 | Range: $0.059830~$0.103900
     Range Pos: 13.8% | Distance High: 57.6%
     State: ARMED_INSIDE_RANGE_LOW
     Watch: wait for price breakout + OI acceleration

🟢 PRICE BREAKOUT CONFIRMED - Retest watch
  ALLO | PoolQ:74 | EntryReady:68
     Sideways 91d | Range 80% | Vol 5.8x
     Price: $0.157690 | Range: $0.080660~$0.144890
     Range Pos: 108.8%
     State: PRICE_BREAKOUT_CONFIRMED
     Watch: retest range high, avoid chase if extended

# 4. Upgrade Hourly OI Monitor
Masalah saat ini
Output hourly OI hanya menampilkan snapshot terbaru, misalnya:
HEI Move +128% | OI +366% | Funding -0.23%

Ini bagus untuk mengetahui token aktif, tetapi tidak cukup untuk menentukan apakah token:
- baru mulai bergerak,
- sedang trigger,
- sudah active trend,
- sudah late,
- atau mulai exit warning.
- Perubahan wajib

Hourly OI Monitor harus mengambil data dari pool pagi dan menyimpan progression antar-jam.
Setiap token hourly harus memiliki:
origin_pool_setup_state
previous_hour_price
previous_hour_oi
previous_hour_funding
current_price
current_oi
current_funding
price_1h_change_pct
oi_1h_change_pct
funding_1h_change
price_3h_change_pct
oi_3h_change_pct
state_transition
trade_state
action

# 5. Hourly Trade State
Buat field:
trade_state

Nilai yang digunakan:
NO_CONFIRMATION
EARLY_UNDERFLOW
READY_LONG
TRIGGERED_LONG
ACTIVE_TREND
LATE_LONG
SHORT_COVERING_ONLY
DISTRIBUTION_RISK
READY_SHORT
TRIGGERED_SHORT
LATE_SHORT
EXIT_WARNING
INVALIDATED

# 6. Rule Trade State untuk Long
### 6.1 NO_CONFIRMATION
if oi_1h_change_pct <= 0 and abs(price_1h_change_pct) < 3:
    trade_state = "NO_CONFIRMATION"

Makna:
Volume pernah muncul di pool, tetapi futures belum mengonfirmasi.

Action:
Deprioritize / watch only

### 6.2 EARLY_UNDERFLOW
if oi_1h_change_pct > 3 and oi_1h_change_pct < 15 and abs(price_1h_change_pct) < 3:
    trade_state = "EARLY_UNDERFLOW"

Makna:
OI mulai naik, harga masih datar. Belum entry.

Action:
Alert only. Wait for OI acceleration + price breakout.

### 6.3 READY_LONG
if (
    oi_1h_change_pct >= 15
    and price_1h_change_pct > 0
    and current_price_near_resistance == True
    and not is_extended
):
    trade_state = "READY_LONG"

Makna:
Setup mulai siap, tetapi tunggu breakout/retest.

Action:
Prepare entry. Wait for close breakout or retest.

### 6.4 TRIGGERED_LONG
if (
    price_breakout_confirmed == True
    and oi_1h_change_pct >= 15
    and volume_confirmed == True
    and not is_extended
):
    trade_state = "TRIGGERED_LONG"

Makna:
Entry long valid.

Action:
Entry allowed with risk management.

### 6.5 ACTIVE_TREND
if (
    price_3h_change_pct > 20
    and oi_3h_change_pct > 30
    and current_funding < 0
):
    trade_state = "ACTIVE_TREND"

Makna:
Trend sudah berjalan. Jangan market chase. Cari retest atau manage posisi.

Action:
Hold/manage if already in position. New entry only on pullback/retest.

### 6.6 LATE_LONG
if (
    price_24h_change_pct > 50
    or price_from_breakout_pct > 30
    or breakout_state == "EXTENDED_BREAKOUT"
):
    trade_state = "LATE_LONG"

Untuk token sangat volatile, tambahkan hard rule:
if price_24h_change_pct > 100:
    trade_state = "LATE_LONG"

Action:
No chase. Retest only. Reduce priority for new entry.

### 6.7 SHORT_COVERING_ONLY
if price_1h_change_pct > 0 and oi_1h_change_pct < 0:
    trade_state = "SHORT_COVERING_ONLY"

Makna:
Harga naik tetapi OI turun. Kenaikan mungkin karena short covering, bukan fresh trend.

Action:
No fresh long unless OI turns positive again.

### 6.8 DISTRIBUTION_RISK
if (
    price_1h_change_pct <= 0
    and oi_1h_change_pct > 10
    and price_24h_change_pct > 30
):
    trade_state = "DISTRIBUTION_RISK"

Makna:
OI naik tetapi harga gagal lanjut setelah move besar. Risiko long trap.

Action:
Avoid new long. Watch for reversal or exit if already long.

### 6.9 EXIT_WARNING
if (
    price_24h_change_pct > 30
    and oi_1h_change_pct < -10
):
    trade_state = "EXIT_WARNING"

Makna:
Posisi mulai keluar setelah move besar.

Action:
Take profit / tighten stop if already in position. No new long.

# 7. Rule Trade State untuk Short
### 7.1 READY_SHORT
if (
    oi_1h_change_pct > 10
    and price_1h_change_pct < -2
    and current_funding >= 0
):
    trade_state = "READY_SHORT"

Makna:
Short build mulai terlihat, tetapi tunggu breakdown struktur.

Action:
Wait for lower low or failed reclaim.

### 7.2 TRIGGERED_SHORT
if (
    breakdown_confirmed == True
    and failed_reclaim == True
    and oi_1h_change_pct > 10
):
    trade_state = "TRIGGERED_SHORT"

Action:

Short allowed with stop above failed reclaim / swing high.

### 7.3 LATE_SHORT
if price_24h_change_pct < -20:
    trade_state = "LATE_SHORT"

Action:
Do not chase short. Wait for bounce/retest.

# 8. Tambahkan Late Penalty
Tambahkan fungsi:

def calculate_late_penalty(price_24h_change_pct, price_from_breakout_pct):
    penalty = 0

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

Gunakan penalty ini pada entry_readiness_score.
Token dengan move besar tetap boleh muncul di ranking, tetapi action-nya harus berubah menjadi:
LATE / NO CHASE / RETEST ONLY

# 9. Tambahkan Origin dari Pool ke Output OI
Hourly OI output wajib menampilkan asal token dari pool pagi:

origin_pool_setup_state
origin_pool_quality_score
origin_range_position_pct
origin_breakout_state

Contoh output:

HEI
Origin: WAKING_UP | PoolQ:80 | Sideways 111d | Vol 2.1x
Now: Move +128% | OI +366% | Funding -0.230%
1h: Price +18% | OI +74% | Funding -0.08% → -0.23%
Transition: WAKING_UP → ACTIVE_TREND → LATE_LONG
Action: NO CHASE. Retest only.

# 10. Hourly OI Output Format Baru

Ganti output hourly menjadi lebih decision-oriented.
Contoh:

🏦 Smart Money Radar - Hourly Progression
⏰ 2026-05-30 00:31 WIB

━━━━━━━━━━━━━━━━━━
🧬 Progression Radar

🔥 HEI — LATE SQUEEZE
Origin: WAKING_UP | Sideways 111d | Vol 2.1x
Now: Move +128% | OI +366% | Funding -0.230%
1h Δ: Price +18% | OI +74% | Funding deteriorating
Transition: WAKING_UP → TRIGGERED_LONG → LATE_LONG
Action: NO CHASE. Wait pullback/retest only.

🟠 TRUST — EARLY UNDERFLOW
Origin: ARMED_INSIDE_RANGE | Sideways 113d | Vol 3.6x
Now: Price flat | OI +4% | Funding neutral
1h Δ: Price +0.3% | OI +3%
Transition: ARMED → EARLY_UNDERFLOW
Action: ALERT ONLY. Long after breakout + OI acceleration.

🟡 GMT — NO CONFIRMATION
Origin: ARMED_INSIDE_RANGE | Sideways 119d | Vol 12.5x
Now: Price flat | OI -4%
Transition: ARMED → NO_CONFIRMATION
Action: Deprioritize until OI turns positive.

🟢 ID — ACTIVE TREND
Origin: ACCUMULATION_POOL
Now: Move +42% | OI +87% | Funding -0.190%
Transition: UNKNOWN → ACTIVE_TREND
Action: Wait retest/reclaim. Do not market chase.

⚠️ ALLO — EXTENDED / OI DIVERGENCE
Origin: PRICE_BREAKOUT_CONFIRMED
Now: Move +125% | OI -7%
Transition: BREAKOUT → SHORT_COVERING_ONLY / EXIT_WARNING
Action: No fresh long. Manage existing position only.

# 11. Ranking Baru untuk Hourly Output

Jangan hanya ranking berdasarkan Combined Score.

Buat beberapa section:

### 11.1 Actionable Now
Isi hanya token dengan:
TRIGGERED_LONG
TRIGGERED_SHORT
READY_LONG
READY_SHORT

### 11.2 Alert / Next Setup
Isi token dengan:
EARLY_UNDERFLOW
ARMED_INSIDE_RANGE
WAKING_UP

### 11.3 Active but Late
Isi token dengan:
ACTIVE_TREND
LATE_LONG
LATE_SHORT

### 11.4 Avoid / Deprioritize
Isi token dengan:
NO_CONFIRMATION
SHORT_COVERING_ONLY
DISTRIBUTION_RISK
EXIT_WARNING
INVALIDATED

# 12. Add Transition Memory
Simpan setiap hourly snapshot ke database/file agar bisa menghitung delta.
Minimal schema:

CREATE TABLE IF NOT EXISTS hourly_token_snapshots (
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

    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

Saat hourly monitor berjalan:

Ambil snapshot terbaru token.
Cari snapshot 1 jam sebelumnya.
Cari snapshot 3 jam sebelumnya.
Hitung delta.
Tentukan trade_state.
Simpan snapshot terbaru.
Output progression.

# 13. Risk Management Output
Setiap token yang READY atau TRIGGERED harus punya risk note.
Contoh:

Risk:
- Avoid market chase if candle already extended.
- Stop below retest low / range reclaim level.
- Reduce size if price_24h_change > 50%.

Untuk LATE_LONG:

Risk:
- No new long.
- Only retest scalp with smaller size.
- Watch for OI rising while price stalls = long trap risk.

Untuk TRIGGERED_SHORT:

Risk:
- Stop above failed reclaim / recent swing high.
- Avoid short if price already dumped >20% in 24h.

# 14. Acceptance Criteria
Implementasi dianggap selesai jika:
Daily Pool Scan menampilkan:
range_position_pct
distance_to_high_pct
breakout_state
pool_setup_state
pool_quality_score
entry_readiness_score
Hourly OI Monitor menampilkan:
origin dari pool pagi
delta 1h dan 3h untuk price, OI, dan funding
trade_state
state_transition
action

Token yang sudah extended seperti:

price 24h move > 50%
atau price from breakout > 30%

tidak boleh muncul sebagai fresh long signal. Harus diberi label:

LATE_LONG / NO CHASE / RETEST ONLY
Token dengan harga naik tetapi OI turun harus diberi label:
SHORT_COVERING_ONLY
Token dengan volume anomaly tetapi OI tidak berkembang harus diberi label:
NO_CONFIRMATION
Token dengan harga flat dan OI naik kecil harus diberi label:
EARLY_UNDERFLOW
Output akhir harus lebih menekankan action daripada ranking.

# 15. Important Trading Logic Philosophy
Jangan membuat bot seolah-olah memberi sinyal entry hanya karena skor tinggi.
Prinsip utama:
Pool score tinggi = layak dipantau
OI naik = ada aktivitas futures
Funding negatif = market crowded short
Harga breakout + OI naik + volume confirm = setup valid
Harga sudah terlalu jauh = late, jangan chase
Harga naik + OI turun = kemungkinan short covering, bukan fresh trend

Output bot harus selalu menjawab:

1. Token ini berasal dari fase apa di pool pagi?
2. Apa yang berubah sejak snapshot hourly sebelumnya?
3. Apakah setup ini baru mulai, siap entry, sudah aktif, atau sudah terlambat?
4. Apa tindakan yang disarankan sekarang?

Final goal:

Ubah bot dari “ranking token ramai” menjadi “decision assistant untuk scalping berdasarkan lifecycle setup”.
