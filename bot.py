"""
No-Wick Signal Bot — Generative Memory Edition
Markets : GBP/JPY · EUR/JPY · XAU/USD · USD/JPY
Timeframes: 1H Swing | 4H Position | Daily Trade
Signals  : BUY & SELL | Multi-TF Confluence + Memory Engine
Timezone : WAT (UTC+1) — Abuja, Nigeria
"""

import os, json, logging, asyncio, sqlite3
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import requests
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()

# ── CONFIG ──────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID        = os.environ["TELEGRAM_CHAT_ID"]
TWELVEDATA_KEY = os.environ["TWELVEDATA_API_KEY"]

SYMBOLS = ["GBP/JPY", "EUR/JPY", "XAU/USD", "USD/JPY"]

TIMEFRAMES = {
    "1H":   {"interval": "1h",   "label": "1H Swing",    "scan_secs": 300,  "candles": 60, "min_body_pips": 5,  "wick_tol": 0.20, "ema_fast": 9, "ema_slow": 21, "rsi_period": 14, "atr_period": 14},
    "4H":   {"interval": "4h",   "label": "4H Position", "scan_secs": 900,  "candles": 60, "min_body_pips": 10, "wick_tol": 0.20, "ema_fast": 9, "ema_slow": 21, "rsi_period": 14, "atr_period": 14},
    "1day": {"interval": "1day", "label": "Daily Trade",  "scan_secs": 3600, "candles": 60, "min_body_pips": 20, "wick_tol": 0.25, "ema_fast": 9, "ema_slow": 21, "rsi_period": 14, "atr_period": 14},
}

TP_MULTIPLIERS = {
    "1H":   (1.0, 2.0, 3.0),
    "4H":   (1.5, 3.0, 5.0),
    "1day": (2.0, 4.0, 7.0),
}

BASE_MIN_CONFLUENCE = 5   # adjusted dynamically by memory engine
DB_PATH = "/app/data/trade_memory.db"   # Railway persistent volume path
WAT     = timezone(timedelta(hours=1))  # West Africa Time (Abuja)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

alerted: dict       = {}
alerts_enabled:bool = True


# ── TIMEZONE HELPER ─────────────────────────────────────────────────
def to_wat(dt_str: str) -> str:
    """Convert a datetime string (UTC assumed) to WAT (UTC+1) display string."""
    try:
        dt_utc = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        dt_wat = dt_utc.astimezone(WAT)
        return dt_wat.strftime("%Y-%m-%d %H:%M WAT")
    except Exception:
        return dt_str

def now_wat() -> str:
    return datetime.now(WAT).strftime("%Y-%m-%d %H:%M WAT")


# ══════════════════════════════════════════════════════════════════════
#  MEMORY ENGINE — SQLite trade history & adaptive scoring
# ══════════════════════════════════════════════════════════════════════
def db_connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return sqlite3.connect(DB_PATH)

def db_init():
    """Create tables if they don't exist."""
    with db_connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT NOT NULL,
                timeframe   TEXT NOT NULL,
                direction   TEXT NOT NULL,
                entry       REAL,
                sl          REAL,
                tp1         REAL,
                tp2         REAL,
                tp3         REAL,
                risk_pips   REAL,
                confluence  INTEGER,
                candle_time TEXT,
                signal_time TEXT,
                outcome     TEXT DEFAULT 'OPEN',
                close_price REAL,
                close_time  TEXT,
                pnl_r       REAL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS factor_weights (
                symbol    TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                direction TEXT NOT NULL,
                factor    TEXT NOT NULL,
                wins      INTEGER DEFAULT 0,
                losses    INTEGER DEFAULT 0,
                PRIMARY KEY (symbol, timeframe, direction, factor)
            )
        """)
        con.commit()

def db_log_signal(symbol, tf, direction, entry, sl, tp1, tp2, tp3,
                  risk_pips, confluence, candle_time) -> int:
    """Save a new signal to memory. Returns the trade ID."""
    with db_connect() as con:
        cur = con.execute("""
            INSERT INTO trades
              (symbol, timeframe, direction, entry, sl, tp1, tp2, tp3,
               risk_pips, confluence, candle_time, signal_time)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (symbol, tf, direction, entry, sl, tp1, tp2, tp3,
              risk_pips, confluence, candle_time, now_wat()))
        con.commit()
        return cur.lastrowid

def db_close_trade(trade_id, outcome, close_price, pnl_r):
    """Mark a trade as WIN/LOSS/BE with its close price and P&L in R."""
    with db_connect() as con:
        con.execute("""
            UPDATE trades SET outcome=?, close_price=?, close_time=?, pnl_r=?
            WHERE id=?
        """, (outcome, close_price, now_wat(), pnl_r, trade_id))
        con.commit()

def db_get_open_trades():
    with db_connect() as con:
        rows = con.execute(
            "SELECT id, symbol, timeframe, direction, entry, sl, tp1, tp2, tp3 "
            "FROM trades WHERE outcome='OPEN'"
        ).fetchall()
    return rows

def db_stats(symbol=None, tf=None, direction=None, limit=50):
    """Return win/loss/open counts and win rate for a given filter."""
    with db_connect() as con:
        filters, params = ["outcome != 'OPEN'"], []
        if symbol:    filters.append("symbol=?");    params.append(symbol)
        if tf:        filters.append("timeframe=?"); params.append(tf)
        if direction: filters.append("direction=?"); params.append(direction)
        where = " AND ".join(filters)
        rows = con.execute(
            f"SELECT outcome, COUNT(*) FROM trades WHERE {where} "
            f"GROUP BY outcome", params
        ).fetchall()
    counts = {r[0]: r[1] for r in rows}
    wins   = counts.get("WIN",  0)
    losses = counts.get("LOSS", 0)
    be     = counts.get("BE",   0)
    total  = wins + losses + be
    rate   = round(wins / total * 100, 1) if total else 0
    return {"wins": wins, "losses": losses, "be": be, "total": total, "win_rate": rate}

def db_update_factor_weight(symbol, tf, direction, factor, won: bool):
    with db_connect() as con:
        con.execute("""
            INSERT INTO factor_weights (symbol, timeframe, direction, factor, wins, losses)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(symbol, timeframe, direction, factor)
            DO UPDATE SET
                wins   = wins   + ?,
                losses = losses + ?
        """, (symbol, tf, direction, factor,
              1 if won else 0, 0 if won else 1,
              1 if won else 0, 0 if won else 1))
        con.commit()

def db_get_factor_weights(symbol, tf, direction) -> dict:
    """Return {factor: weight_multiplier} based on historical accuracy."""
    with db_connect() as con:
        rows = con.execute("""
            SELECT factor, wins, losses FROM factor_weights
            WHERE symbol=? AND timeframe=? AND direction=?
        """, (symbol, tf, direction)).fetchall()
    weights = {}
    for factor, wins, losses in rows:
        total = wins + losses
        if total >= 5:   # need at least 5 samples to trust the weight
            rate = wins / total
            # Scale: 0% win rate → 0.5x weight, 50% → 1.0x, 100% → 1.5x
            weights[factor] = round(0.5 + rate, 2)
        else:
            weights[factor] = 1.0   # neutral until enough data
    return weights

def memory_adjust_threshold(symbol, tf, direction) -> int:
    """
    Dynamically raise/lower the minimum confluence threshold
    based on recent performance for this symbol/tf/direction.
    Win rate > 65% → lower threshold (trust it more)
    Win rate < 40% → raise threshold (be more selective)
    """
    stats = db_stats(symbol, tf, direction)
    if stats["total"] < 5:
        return BASE_MIN_CONFLUENCE   # not enough data yet
    rate = stats["win_rate"]
    if rate >= 65:
        return max(BASE_MIN_CONFLUENCE - 1, 3)
    elif rate < 40:
        return min(BASE_MIN_CONFLUENCE + 1, 7)
    return BASE_MIN_CONFLUENCE


# ══════════════════════════════════════════════════════════════════════
#  OUTCOME TRACKER — checks open trades against live price
# ══════════════════════════════════════════════════════════════════════
async def check_open_trades(bot: Bot):
    """
    Called every scan. Fetches latest price for each open trade,
    checks if TP1/SL hit, updates memory, and notifies Telegram.
    """
    open_trades = db_get_open_trades()
    if not open_trades:
        return

    for trade_id, symbol, tf, direction, entry, sl, tp1, tp2, tp3 in open_trades:
        candles = fetch_candles(symbol, "15min", outputsize=5)
        if not candles:
            continue
        price = float(candles[0]["close"])
        outcome, pnl_r, emoji = None, None, ""

        if direction == "BUY":
            if price <= sl:
                outcome, pnl_r, emoji = "LOSS", -1.0, "🔴"
            elif price >= tp2:
                outcome, pnl_r, emoji = "WIN",   2.0, "🟢"
            elif price >= tp1:
                outcome, pnl_r, emoji = "WIN",   1.0, "🟡"
        else:  # SELL
            if price >= sl:
                outcome, pnl_r, emoji = "LOSS", -1.0, "🔴"
            elif price <= tp2:
                outcome, pnl_r, emoji = "WIN",   2.0, "🟢"
            elif price <= tp1:
                outcome, pnl_r, emoji = "WIN",   1.0, "🟡"

        if outcome:
            db_close_trade(trade_id, outcome, price, pnl_r)
            log.info(f"[MEMORY] Trade #{trade_id} {symbol}/{tf} {direction} → {outcome} @ {price}")
            if alerts_enabled:
                await bot.send_message(
                    chat_id=CHAT_ID,
                    text=(
                        f"{emoji} *TRADE CLOSED* #{trade_id}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"{'🇬🇧🇯🇵' if symbol=='GBP/JPY' else '🇪🇺🇯🇵' if symbol=='EUR/JPY' else '🥇💵' if symbol=='XAU/USD' else '🇺🇸🇯🇵'}  "
                        f"*{symbol}* — {TIMEFRAMES.get(tf, {}).get('label', tf)}\n"
                        f"Direction : {'🟢 BUY' if direction=='BUY' else '🔴 SELL'}\n"
                        f"Outcome   : *{outcome}*  ({'+' if pnl_r > 0 else ''}{pnl_r}R)\n"
                        f"Close     : `{price}`\n"
                        f"🕐 {now_wat()}"
                    ),
                    parse_mode="Markdown",
                )
            await asyncio.sleep(0.5)


# ── DATA FETCHER ────────────────────────────────────────────────────
def fetch_candles(symbol: str, interval: str, outputsize: int = 60):
    url    = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": interval, "outputsize": outputsize,
              "apikey": TWELVEDATA_KEY, "format": "JSON"}
    try:
        r    = requests.get(url, params=params, timeout=15)
        data = r.json()
        if data.get("status") == "error":
            log.warning(f"[{symbol}/{interval}] API: {data.get('message')}")
            return None
        return data.get("values", [])
    except Exception as e:
        log.error(f"[{symbol}/{interval}] fetch error: {e}")
        return None


# ── INDICATORS ──────────────────────────────────────────────────────
def calc_ema(closes, period):
    if len(closes) < period: return []
    k, ema = 2 / (period + 1), [sum(closes[:period]) / period]
    for p in closes[period:]: ema.append(p * k + ema[-1] * (1 - k))
    return ema

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period-1) + gains[i]) / period
        al = (al * (period-1) + losses[i]) / period
    return round(100 - (100 / (1 + ag/al)), 2) if al else 100.0

def calc_atr(candles, period=14):
    if len(candles) < period + 1: return None
    trs = [max(float(candles[i]["high"]) - float(candles[i]["low"]),
               abs(float(candles[i]["high"]) - float(candles[i-1]["close"])),
               abs(float(candles[i]["low"])  - float(candles[i-1]["close"])))
           for i in range(1, len(candles))]
    atr = sum(trs[:period]) / period
    for tr in trs[period:]: atr = (atr * (period-1) + tr) / period
    return round(atr, 5)

def find_sr(candles, lookback=20):
    highs = [float(c["high"]) for c in candles[1:lookback+1]]
    lows  = [float(c["low"])  for c in candles[1:lookback+1]]
    return round(min(lows), 5), round(max(highs), 5)

def pip_unit(symbol): return 0.01 if "JPY" in symbol else (1.0 if "XAU" in symbol else 0.0001)
def decimals(symbol): return 3 if "JPY" in symbol or "XAU" in symbol else 5


# ── CANDLE DETECTOR ─────────────────────────────────────────────────
def analyse_candle(candle, symbol, tf_cfg):
    o,h,l,c = float(candle["open"]),float(candle["high"]),float(candle["low"]),float(candle["close"])
    pu = pip_unit(symbol)
    body = abs(c-o); body_pips = body/pu
    is_bull = c > o
    upper_wick = h - max(o,c); lower_wick = min(o,c) - l
    stats = {"open":o,"high":h,"low":l,"close":c,
             "body":round(body,5),"body_pips":round(body_pips,1),
             "upper_wick":round(upper_wick,5),"lower_wick":round(lower_wick,5),
             "time":candle["datetime"],"is_bull":is_bull}
    if body_pips < tf_cfg["min_body_pips"]: return None, stats
    tol = tf_cfg["wick_tol"] * body
    if upper_wick > tol or lower_wick > tol: return None, stats
    return ("BUY" if is_bull else "SELL"), stats


# ── CONFLUENCE ENGINE (memory-weighted) ─────────────────────────────
def research_confluence(candles_newest_first, symbol, tf_cfg, direction):
    candles_old = list(reversed(candles_newest_first))
    closes = [float(c["close"]) for c in candles_old]
    pu = pip_unit(symbol)
    score, checks, factor_hits = 0, {}, {}

    # Load historical weights for this setup
    weights = db_get_factor_weights(symbol, tf_cfg["interval"], direction)

    ef = calc_ema(closes, tf_cfg["ema_fast"])
    es = calc_ema(closes, tf_cfg["ema_slow"])

    # 1. EMA trend
    if ef and es:
        trend_bull = ef[-1] > es[-1]
        aligned = (direction=="BUY" and trend_bull) or (direction=="SELL" and not trend_bull)
        label = f"✅ Aligned with {direction}" if aligned else "❌ Against trend"
        w = weights.get("ema_trend", 1.0)
        hist = f" _(hist×{w})_" if w != 1.0 else ""
        checks["ema_trend"] = f"EMA{tf_cfg['ema_fast']} {'>' if trend_bull else '<'} EMA{tf_cfg['ema_slow']} — {label}{hist}"
        if aligned: score += round(2 * w); factor_hits["ema_trend"] = True
        else: factor_hits["ema_trend"] = False

    # 2. Price vs slow EMA
    if es:
        price_bull = closes[-1] > es[-1]
        aligned_p = (direction=="BUY" and price_bull) or (direction=="SELL" and not price_bull)
        w = weights.get("price_ema", 1.0)
        hist = f" _(hist×{w})_" if w != 1.0 else ""
        checks["price_ema"] = f"Price {'above' if price_bull else 'below'} EMA{tf_cfg['ema_slow']} — {'✅' if aligned_p else '❌'}{hist}"
        if aligned_p: score += round(1 * w); factor_hits["price_ema"] = True
        else: factor_hits["price_ema"] = False

    # 3. RSI
    rsi = calc_rsi(closes, tf_cfg["rsi_period"])
    if rsi is not None:
        if direction == "BUY":
            rsi_ok = 40 <= rsi <= 75
            rsi_label = "✅ Bullish zone" if rsi_ok else ("⚠️ Overbought" if rsi > 75 else "⚠️ Weak")
        else:
            rsi_ok = 25 <= rsi <= 60
            rsi_label = "✅ Bearish zone" if rsi_ok else ("⚠️ Oversold" if rsi < 25 else "⚠️ Weak")
        w = weights.get("rsi", 1.0)
        hist = f" _(hist×{w})_" if w != 1.0 else ""
        checks["rsi"] = f"RSI {rsi} — {rsi_label}{hist}"
        if rsi_ok: score += round(2 * w); factor_hits["rsi"] = True
        else:
            factor_hits["rsi"] = False
            if (direction=="BUY" and rsi > 75) or (direction=="SELL" and rsi < 25): score -= 1

    # 4. ATR
    atr = calc_atr(candles_old, tf_cfg["atr_period"])
    avg_bod = sum(abs(float(c["close"])-float(c["open"])) for c in candles_old[-20:]) / 20
    if atr:
        vol_ok = atr > avg_bod * 0.5
        w = weights.get("atr", 1.0)
        hist = f" _(hist×{w})_" if w != 1.0 else ""
        checks["atr"] = f"ATR {round(atr/pu,1)} pips — {'✅ Active' if vol_ok else '⚠️ Low volatility'}{hist}"
        if vol_ok: score += round(1 * w); factor_hits["atr"] = True
        else: factor_hits["atr"] = False

    # 5. Candle momentum
    last3 = candles_newest_first[:3]
    bull_count = sum(1 for c in last3 if float(c["close"]) > float(c["open"]))
    bear_count = 3 - bull_count
    mom_ok = (bull_count >= 2) if direction=="BUY" else (bear_count >= 2)
    w = weights.get("momentum", 1.0)
    hist = f" _(hist×{w})_" if w != 1.0 else ""
    side_count = bull_count if direction=="BUY" else bear_count
    side_label = "bullish" if direction=="BUY" else "bearish"
    checks["momentum"] = f"{side_count}/3 recent candles {side_label} — {'✅' if mom_ok else '⚠️'}{hist}"
    if mom_ok: score += round(1 * w); factor_hits["momentum"] = True
    else: factor_hits["momentum"] = False

    # 6. S/R room
    support, resistance = find_sr(candles_newest_first)
    price = float(candles_newest_first[0]["close"])
    if direction == "BUY":
        room_pips = round((resistance - price) / pu, 1)
        room_ok = room_pips > 15
        checks["sr_room"] = f"Room to resistance: {room_pips} pips — {'✅ Clear path' if room_ok else '⚠️ Near resistance'}"
    else:
        room_pips = round((price - support) / pu, 1)
        room_ok = room_pips > 15
        checks["sr_room"] = f"Room to support: {room_pips} pips — {'✅ Clear path' if room_ok else '⚠️ Near support'}"
    w = weights.get("sr_room", 1.0)
    if room_ok: score += round(1 * w); factor_hits["sr_room"] = True
    else: factor_hits["sr_room"] = False

    # Memory insight line
    stats = db_stats(symbol, tf_cfg["interval"], direction)
    mem_line = ""
    if stats["total"] >= 3:
        mem_line = f"\n🧠 Memory: {stats['wins']}W/{stats['losses']}L ({stats['win_rate']}% WR on {stats['total']} trades)"

    return {
        "score": score, "max_score": 8, "checks": checks,
        "rsi": rsi, "atr": atr, "support": support, "resistance": resistance,
        "factor_hits": factor_hits, "mem_line": mem_line,
    }


# ── TRADE LEVELS ────────────────────────────────────────────────────
def calculate_trade_levels(stats, symbol, tf_key, direction, atr):
    entry = stats["close"]; dec = decimals(symbol); pu = pip_unit(symbol)
    tp1_m, tp2_m, tp3_m = TP_MULTIPLIERS[tf_key]
    if direction == "BUY":
        sl = stats["low"]; risk = entry - sl
        if atr and atr > risk: sl = round(entry - atr, dec); risk = atr
        return {"entry": round(entry,dec), "sl": round(sl,dec),
                "tp1": round(entry+tp1_m*risk,dec), "tp2": round(entry+tp2_m*risk,dec),
                "tp3": round(entry+tp3_m*risk,dec), "risk_pips": round(risk/pu,1),
                "tp1_r": tp1_m, "tp2_r": tp2_m, "tp3_r": tp3_m}
    else:
        sl = stats["high"]; risk = sl - entry
        if atr and atr > risk: sl = round(entry + atr, dec); risk = atr
        return {"entry": round(entry,dec), "sl": round(sl,dec),
                "tp1": round(entry-tp1_m*risk,dec), "tp2": round(entry-tp2_m*risk,dec),
                "tp3": round(entry-tp3_m*risk,dec), "risk_pips": round(risk/pu,1),
                "tp1_r": tp1_m, "tp2_r": tp2_m, "tp3_r": tp3_m}


# ── SIGNAL FORMATTER ────────────────────────────────────────────────
def format_signal(symbol, tf_key, direction, stats, levels, confluence, trade_id):
    flag  = {"GBP/JPY":"🇬🇧🇯🇵","EUR/JPY":"🇪🇺🇯🇵","XAU/USD":"🥇💵","USD/JPY":"🇺🇸🇯🇵"}.get(symbol,"📈")
    stars = "⭐" * min(confluence["score"], 5)
    checks_text = "\n".join(f"  {v}" for v in confluence["checks"].values())
    dir_line = "🟢 *BUY  (LONG)*" if direction == "BUY" else "🔴 *SELL (SHORT)*"
    wat_time = to_wat(stats["time"])
    mem_line = confluence.get("mem_line", "")
    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕯 *NO-WICK SIGNAL* #{trade_id}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{flag}  *{symbol}* — {TIMEFRAMES[tf_key]['label']}\n"
        f"🕐 `{wat_time}`\n"
        f"{dir_line}\n"
        f"📊 Confluence: {stars} `{confluence['score']}/{confluence['max_score']}`\n"
        f"{mem_line}\n\n"
        f"*📍 Trade Levels*\n"
        f"  ▶️  Entry : `{levels['entry']}`\n"
        f"  🛑  SL    : `{levels['sl']}` ({levels['risk_pips']} pips)\n"
        f"  🎯  TP1   : `{levels['tp1']}`  _({levels['tp1_r']}R)_\n"
        f"  🎯  TP2   : `{levels['tp2']}`  _({levels['tp2_r']}R)_\n"
        f"  🎯  TP3   : `{levels['tp3']}`  _({levels['tp3_r']}R)_\n\n"
        f"*🔬 Analysis*\n{checks_text}\n\n"
        f"*📐 Candle*\n"
        f"  Body: `{stats['body_pips']} pips` | "
        f"Upper: `{stats['upper_wick']}` | Lower: `{stats['lower_wick']}`\n\n"
        f"⚠️ _Manage risk. Max 1-2% per trade._\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )


# ── SCANNER ─────────────────────────────────────────────────────────
async def scan_timeframe(bot: Bot, tf_key: str, notify_no_signal: bool = False):
    tf_cfg = TIMEFRAMES[tf_key]
    signals_found = 0
    log.info(f"🔭 [{tf_key}] Research scan starting")

    for symbol in SYMBOLS:
        candles = fetch_candles(symbol, tf_cfg["interval"], outputsize=tf_cfg["candles"])
        if not candles or len(candles) < 30:
            continue

        latest      = candles[0]
        candle_time = latest["datetime"]
        direction, stats = analyse_candle(latest, symbol, tf_cfg)
        if not direction:
            log.info(f"[{symbol}/{tf_key}] ❌ No valid candle shape")
            continue

        alert_key = f"{symbol}_{tf_key}_{direction}"
        if alerted.get(alert_key) == candle_time:
            continue

        log.info(f"[{symbol}/{tf_key}] ✅ {direction} candle — deep analysis + memory check...")
        confluence = research_confluence(candles, symbol, tf_cfg, direction)
        score      = confluence["score"]

        # Memory-adjusted threshold
        min_conf = memory_adjust_threshold(symbol, tf_cfg["interval"], direction)
        log.info(f"[{symbol}/{tf_key}] {direction} score:{score} threshold:{min_conf}")

        if score < min_conf:
            log.info(f"[{symbol}/{tf_key}] ⚠️ Score {score} < threshold {min_conf}, skipping")
            continue

        levels   = calculate_trade_levels(stats, symbol, tf_key, direction, confluence["atr"])
        trade_id = db_log_signal(
            symbol, tf_cfg["interval"], direction,
            levels["entry"], levels["sl"], levels["tp1"], levels["tp2"], levels["tp3"],
            levels["risk_pips"], score, candle_time
        )
        msg = format_signal(symbol, tf_key, direction, stats, levels, confluence, trade_id)

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Taken",   callback_data=f"taken_{trade_id}"),
            InlineKeyboardButton("❌ Skipped", callback_data=f"skip_{trade_id}"),
        ]])

        if alerts_enabled:
            await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown", reply_markup=keyboard)
            log.info(f"[{symbol}/{tf_key}] 🚀 Signal #{trade_id} sent!")
        else:
            log.info(f"[{symbol}/{tf_key}] 🔕 Signal #{trade_id} found but alerts OFF")

        alerted[alert_key] = candle_time
        signals_found += 1
        await asyncio.sleep(1)

    if notify_no_signal and signals_found == 0:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=f"🔍 *{TIMEFRAMES[tf_key]['label']} scan complete* — no qualifying setups.\n🕐 {now_wat()}",
            parse_mode="Markdown",
        )


async def scan_all(bot: Bot, notify_no_signal: bool = False):
    await check_open_trades(bot)
    for tf_key in TIMEFRAMES:
        await scan_timeframe(bot, tf_key, notify_no_signal)
        await asyncio.sleep(2)


# ── COMMAND HANDLERS ────────────────────────────────────────────────
async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *No-Wick Signal Bot* — Generative Memory Edition\n\n"
        "*Timeframes:*\n"
        "  📊 1H  — Swing (every 5 min)\n"
        "  📊 4H  — Position (every 15 min)\n"
        "  📊 Day — Daily (every 1 hr)\n\n"
        "*Signals:* 🟢 BUY & 🔴 SELL\n"
        "*Memory:* Bot learns from past trades ✅\n"
        "*Timezone:* WAT (Abuja) 🇳🇬\n\n"
        "*Commands:*\n"
        "  /scan /scan1h /scan4h /scanday\n"
        "  /status — live bot status\n"
        "  /performance — full win/loss stats\n"
        "  /history — last 10 closed trades\n"
        "  /open — currently open trades\n"
        "  /close — manually close a trade\n"
        "  /alert — toggle alerts ON/OFF\n"
        "  /pairs — active pairs",
        parse_mode="Markdown",
    )

async def cmd_scan(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🔭 Full multi-TF scan + memory check...\n🕐 {now_wat()}")
    await scan_all(context.application.bot, notify_no_signal=True)

async def cmd_scan_1h(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔭 Scanning 1H...")
    await scan_timeframe(context.application.bot, "1H", notify_no_signal=True)

async def cmd_scan_4h(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔭 Scanning 4H...")
    await scan_timeframe(context.application.bot, "4H", notify_no_signal=True)

async def cmd_scan_day(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔭 Scanning Daily...")
    await scan_timeframe(context.application.bot, "1day", notify_no_signal=True)

async def cmd_alert(update, context: ContextTypes.DEFAULT_TYPE):
    global alerts_enabled
    alerts_enabled = not alerts_enabled
    state = "🔔 ON" if alerts_enabled else "🔕 OFF"
    await update.message.reply_text(f"*Alerts: {state}*", parse_mode="Markdown")

async def cmd_status(update, context: ContextTypes.DEFAULT_TYPE):
    state = "🔔 ON" if alerts_enabled else "🔕 OFF"
    lines = [f"*Bot Status* | Alerts: {state} | {now_wat()}\n"]
    for tf_key, tf_cfg in TIMEFRAMES.items():
        lines.append(f"*— {tf_cfg['label']} —*")
        for sym in SYMBOLS:
            b = alerted.get(f"{sym}_{tf_key}_BUY",  "—")
            s = alerted.get(f"{sym}_{tf_key}_SELL", "—")
            lines.append(f"  • {sym}  🟢`{b}` 🔴`{s}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_performance(update, context: ContextTypes.DEFAULT_TYPE):
    lines = [f"*📊 Performance Report*\n🕐 {now_wat()}\n"]
    for sym in SYMBOLS:
        for tf_key, tf_cfg in TIMEFRAMES.items():
            for d in ["BUY", "SELL"]:
                st = db_stats(sym, tf_cfg["interval"], d)
                if st["total"] == 0: continue
                bar = "🟩" * st["wins"] + "🟥" * st["losses"]
                lines.append(
                    f"*{sym} {tf_cfg['label']} {d}*\n"
                    f"  {bar}\n"
                    f"  {st['wins']}W / {st['losses']}L / {st['be']}BE  —  Win rate: *{st['win_rate']}%*\n"
                )
    if len(lines) == 2:
        lines.append("No closed trades yet. Keep trading! 🚀")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_history(update, context: ContextTypes.DEFAULT_TYPE):
    with db_connect() as con:
        rows = con.execute(
            "SELECT id, symbol, timeframe, direction, entry, outcome, pnl_r, close_time "
            "FROM trades WHERE outcome != 'OPEN' ORDER BY id DESC LIMIT 10"
        ).fetchall()
    if not rows:
        await update.message.reply_text("No closed trades yet.")
        return
    lines = [f"*📜 Last 10 Closed Trades*\n"]
    for r in rows:
        tid, sym, tf, d, entry, outcome, pnl, ctime = r
        emoji = "🟢" if outcome=="WIN" else ("🔴" if outcome=="LOSS" else "⚪")
        pnl_str = f"{'+' if pnl and pnl > 0 else ''}{pnl}R" if pnl else "—"
        lines.append(f"{emoji} #{tid} *{sym}* {d} | {outcome} {pnl_str} | `{ctime or '—'}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_open(update, context: ContextTypes.DEFAULT_TYPE):
    open_trades = db_get_open_trades()
    if not open_trades:
        await update.message.reply_text("✅ No open trades right now.")
        return
    lines = [f"*📂 Open Trades ({len(open_trades)})*\n"]
    for r in open_trades:
        tid, sym, tf, d, entry, sl, tp1, tp2, tp3 = r
        emoji = "🟢" if d=="BUY" else "🔴"
        lines.append(f"{emoji} #{tid} *{sym}* {d} | Entry:`{entry}` SL:`{sl}` TP1:`{tp1}`")
    lines.append("\nTo close manually: /close <trade_id> <WIN|LOSS|BE>")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_close(update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /close <id> <WIN|LOSS|BE>"""
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /close <trade_id> <WIN|LOSS|BE>")
        return
    try:
        tid     = int(args[0])
        outcome = args[1].upper()
        if outcome not in ("WIN","LOSS","BE"):
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text("Usage: /close <trade_id> <WIN|LOSS|BE>")
        return
    pnl_map = {"WIN": 1.0, "LOSS": -1.0, "BE": 0.0}
    db_close_trade(tid, outcome, None, pnl_map[outcome])
    emoji = "🟢" if outcome=="WIN" else ("🔴" if outcome=="LOSS" else "⚪")
    await update.message.reply_text(f"{emoji} Trade #{tid} closed as *{outcome}*.", parse_mode="Markdown")

async def cmd_pairs(update, context: ContextTypes.DEFAULT_TYPE):
    pairs = "\n".join(f"  • {s}" for s in SYMBOLS)
    tfs   = "\n".join(f"  • {k} — {v['label']}" for k,v in TIMEFRAMES.items())
    await update.message.reply_text(f"*Active Pairs*\n{pairs}\n\n*Timeframes*\n{tfs}", parse_mode="Markdown")

async def callback_handler(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data
    if data.startswith("taken_"):
        tid = data.replace("taken_", "")
        await query.edit_message_reply_markup(
            InlineKeyboardMarkup([[
                InlineKeyboardButton(f"✅ #{tid} Taken — close with /close {tid} WIN|LOSS|BE", callback_data="done")
            ]]))
    elif data.startswith("skip_"):
        tid = data.replace("skip_", "")
        db_close_trade(int(tid), "SKIPPED", None, 0)
        await query.edit_message_reply_markup(
            InlineKeyboardMarkup([[InlineKeyboardButton(f"❌ #{tid} Skipped", callback_data="done")]]))


# ── PERIODIC JOBS ───────────────────────────────────────────────────
async def job_1h(context: ContextTypes.DEFAULT_TYPE):
    await check_open_trades(context.bot)
    await scan_timeframe(context.bot, "1H")

async def job_4h(context: ContextTypes.DEFAULT_TYPE):
    await scan_timeframe(context.bot, "4H")

async def job_day(context: ContextTypes.DEFAULT_TYPE):
    await scan_timeframe(context.bot, "1day")


# ── MAIN ────────────────────────────────────────────────────────────
def main():
    db_init()
    log.info("🚀 Starting No-Wick Bot — Generative Memory Edition")
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    for cmd, fn in [
        ("start",   cmd_start),   ("scan",    cmd_scan),
        ("scan1h",  cmd_scan_1h), ("scan4h",  cmd_scan_4h),
        ("scanday", cmd_scan_day),("status",  cmd_status),
        ("performance", cmd_performance), ("history", cmd_history),
        ("open",    cmd_open),    ("close",   cmd_close),
        ("pairs",   cmd_pairs),   ("alert",   cmd_alert),
    ]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(CallbackQueryHandler(callback_handler))

    app.job_queue.run_repeating(job_1h,  interval=TIMEFRAMES["1H"]["scan_secs"],   first=10)
    app.job_queue.run_repeating(job_4h,  interval=TIMEFRAMES["4H"]["scan_secs"],   first=20)
    app.job_queue.run_repeating(job_day, interval=TIMEFRAMES["1day"]["scan_secs"], first=30)

    log.info("✅ Running — Memory active | WAT timezone | BUY & SELL")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
