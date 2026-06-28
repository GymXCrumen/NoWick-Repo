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
def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise SystemExit(f"❌ Missing required environment variable: {key}\n"
                         f"   Add it to Railway → Variables and redeploy.")
    return val

TELEGRAM_TOKEN = _require("TELEGRAM_BOT_TOKEN")
CHAT_ID        = _require("TELEGRAM_CHAT_ID")
TWELVEDATA_KEY = _require("TWELVEDATA_API_KEY")
CHANNEL_ID     = os.environ.get("CHANNEL_ID", "-1003379266288")

SYMBOLS = [
    "GBP/JPY", "EUR/JPY", "USD/JPY", "XAU/USD",  # current
    "GBP/USD", "EUR/USD",                           # cleanest majors
    "USD/CHF", "AUD/USD",                           # solid trends
]

TIMEFRAMES = {
    "1H":   {"interval": "1h",   "label": "1H Swing",    "scan_secs": 300,  "candles": 60, "min_body_pips": 5,  "wick_tol": 0.15, "ema_fast": 9, "ema_slow": 21, "rsi_period": 14, "atr_period": 14},
    "4H":   {"interval": "4h",   "label": "4H Position", "scan_secs": 900,  "candles": 60, "min_body_pips": 8,  "wick_tol": 0.15, "ema_fast": 9, "ema_slow": 21, "rsi_period": 14, "atr_period": 14},
    "1day": {"interval": "1day", "label": "Daily Trade",  "scan_secs": 3600, "candles": 60, "min_body_pips": 15, "wick_tol": 0.20, "ema_fast": 9, "ema_slow": 21, "rsi_period": 14, "atr_period": 14},
}

TP_MULTIPLIERS = {
    "1H":   (1.0, 2.0, 3.0),
    "4H":   (1.5, 3.0, 5.0),
    "1day": (2.0, 4.0, 7.0),
}

BASE_MIN_CONFLUENCE = 4   # adjusted dynamically by memory engine
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
                strategy    TEXT DEFAULT 'NOWICK',
                entry       REAL,
                sl          REAL,
                current_sl  REAL,
                tp1         REAL,
                tp2         REAL,
                tp3         REAL,
                risk_pips   REAL,
                confluence  INTEGER,
                candle_time TEXT,
                signal_time TEXT,
                stage       INTEGER DEFAULT 0,
                outcome     TEXT DEFAULT 'OPEN',
                close_price REAL,
                close_time  TEXT,
                pnl_r       REAL
            )
        """)
        # Migrate existing DB — add new columns if missing
        for col, definition in [
            ("current_sl", "REAL"),
            ("stage",      "INTEGER DEFAULT 0"),
            ("strategy",   "TEXT DEFAULT 'NOWICK'"),
            ("factor_hits","TEXT DEFAULT '{}'"),
        ]:
            try:
                con.execute(f"ALTER TABLE trades ADD COLUMN {col} {definition}")
            except Exception:
                pass  # column already exists
        con.execute("""
            CREATE TABLE IF NOT EXISTS pending_levels (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol        TEXT NOT NULL,
                tf_key        TEXT NOT NULL,
                direction     TEXT NOT NULL,
                level         REAL NOT NULL,
                candle_time   TEXT NOT NULL,
                last_candle   TEXT NOT NULL,
                age_candles   INTEGER DEFAULT 0,
                active        INTEGER DEFAULT 1
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS factor_weights (
                symbol    TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                direction TEXT NOT NULL,
                strategy  TEXT NOT NULL DEFAULT 'NOWICK',
                factor    TEXT NOT NULL,
                wins      INTEGER DEFAULT 0,
                losses    INTEGER DEFAULT 0,
                PRIMARY KEY (symbol, timeframe, direction, strategy, factor)
            )
        """)
        # Migrate factor_weights too
        try:
            con.execute("ALTER TABLE factor_weights ADD COLUMN strategy TEXT NOT NULL DEFAULT 'NOWICK'")
        except Exception:
            pass
        con.commit()

def db_log_signal(symbol, tf, direction, entry, sl, tp1, tp2, tp3,
                  risk_pips, confluence, candle_time, strategy="NOWICK", factor_hits=None) -> int:
    """Save a new signal to memory. Returns the trade ID."""
    with db_connect() as con:
        cur = con.execute("""
            INSERT INTO trades
              (symbol, timeframe, direction, strategy, entry, sl, current_sl, tp1, tp2, tp3,
               risk_pips, confluence, candle_time, signal_time, stage, factor_hits)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (symbol, tf, direction, strategy, entry, sl, sl, tp1, tp2, tp3,
              risk_pips, confluence, candle_time, now_wat(), 0, json.dumps(factor_hits or {})))
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
            "SELECT id, symbol, timeframe, direction, entry, sl, current_sl, tp1, tp2, tp3, stage, strategy "
            "FROM trades WHERE outcome='OPEN'"
        ).fetchall()
    return rows

def db_symbol_has_open_trade(symbol: str, strategy: str = None) -> bool:
    """Returns True if there is already an open trade for this symbol (optionally per-strategy)."""
    with db_connect() as con:
        if strategy:
            row = con.execute(
                "SELECT COUNT(*) FROM trades WHERE symbol=? AND outcome='OPEN' AND strategy=?",
                (symbol, strategy)
            ).fetchone()
        else:
            row = con.execute(
                "SELECT COUNT(*) FROM trades WHERE symbol=? AND outcome='OPEN'", (symbol,)
            ).fetchone()
    return row[0] > 0

def db_update_trade_stage(trade_id: int, stage: int, current_sl: float):
    """Update trade stage and move SL (e.g. to breakeven after TP1)."""
    with db_connect() as con:
        con.execute(
            "UPDATE trades SET stage=?, current_sl=? WHERE id=?",
            (stage, current_sl, trade_id)
        )
        con.commit()

def db_stats(symbol=None, tf=None, direction=None, strategy=None, limit=50):
    """Return win/loss/open counts and win rate for a given filter."""
    with db_connect() as con:
        filters, params = ["outcome != 'OPEN'"], []
        if symbol:    filters.append("symbol=?");    params.append(symbol)
        if tf:        filters.append("timeframe=?"); params.append(tf)
        if direction: filters.append("direction=?"); params.append(direction)
        if strategy:  filters.append("strategy=?");  params.append(strategy)
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

def db_update_factor_weight(symbol, tf, direction, factor, won: bool, strategy="NOWICK"):
    with db_connect() as con:
        con.execute("""
            INSERT INTO factor_weights (symbol, timeframe, direction, strategy, factor, wins, losses)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(symbol, timeframe, direction, strategy, factor)
            DO UPDATE SET
                wins   = wins   + ?,
                losses = losses + ?
        """, (symbol, tf, direction, strategy, factor,
              1 if won else 0, 0 if won else 1,
              1 if won else 0, 0 if won else 1))
        con.commit()

def db_get_factor_weights(symbol, tf, direction, strategy="NOWICK") -> dict:
    """Return {factor: weight_multiplier} based on historical accuracy."""
    with db_connect() as con:
        rows = con.execute("""
            SELECT factor, wins, losses FROM factor_weights
            WHERE symbol=? AND timeframe=? AND direction=? AND strategy=?
        """, (symbol, tf, direction, strategy)).fetchall()
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

def memory_adjust_threshold(symbol, tf, direction, strategy="NOWICK") -> int:
    """
    Dynamically raise/lower the minimum confluence threshold
    based on recent performance for this symbol/tf/direction/strategy.
    Win rate > 65% → lower threshold (trust it more)
    Win rate < 40% → raise threshold (be more selective)
    """
    stats = db_stats(symbol, tf, direction, strategy)
    if stats["total"] < 5:
        return BASE_MIN_CONFLUENCE   # not enough data yet
    rate = stats["win_rate"]
    if rate >= 65:
        return max(BASE_MIN_CONFLUENCE - 1, 3)
    elif rate < 40:
        return min(BASE_MIN_CONFLUENCE + 1, 7)
    return BASE_MIN_CONFLUENCE

def db_get_trade_meta(trade_id: int):
    """Fetch symbol/tf/direction/strategy/factor_hits for a trade — used for learning on close."""
    with db_connect() as con:
        row = con.execute(
            "SELECT symbol, timeframe, direction, strategy, factor_hits FROM trades WHERE id=?",
            (trade_id,)
        ).fetchone()
    if not row:
        return None
    symbol, tf, direction, strategy, factor_hits_json = row
    try:
        factor_hits = json.loads(factor_hits_json or "{}")
    except Exception:
        factor_hits = {}
    return {"symbol": symbol, "tf": tf, "direction": direction,
            "strategy": strategy or "NOWICK", "factor_hits": factor_hits}

def learn_from_trade(trade_id: int, won: bool):
    """
    Update factor_weights for every factor that contributed to this trade,
    based on whether it ultimately won or lost. Called whenever a trade closes.
    """
    meta = db_get_trade_meta(trade_id)
    if not meta or not meta["factor_hits"]:
        return
    for factor, hit in meta["factor_hits"].items():
        if hit:  # only learn from factors that were actually satisfied (✅) on this trade
            db_update_factor_weight(
                meta["symbol"], meta["tf"], meta["direction"], factor, won,
                strategy=meta["strategy"]
            )


# ══════════════════════════════════════════════════════════════════════
#  OUTCOME TRACKER — checks open trades against live price
# ══════════════════════════════════════════════════════════════════════
async def check_open_trades(bot: Bot):
    """
    Scale-out outcome tracker:
      Stage 0 → watching for TP1 or SL
      Stage 1 → TP1 hit: partial close (+1R), SL moved to breakeven, watching TP2
      Stage 2 → TP2 hit: partial close (+2R), SL moved to TP1, watching TP3
      Stage 3 → TP3 hit: full close (+3R)
    If SL hit at any stage, close remainder at current_sl pnl.
    """
    open_trades = db_get_open_trades()
    if not open_trades:
        return

    for row in open_trades:
        trade_id, symbol, tf, direction, entry, sl, current_sl, tp1, tp2, tp3, stage, strategy = row
        # current_sl may be None for old trades — fall back to original sl
        if current_sl is None:
            current_sl = sl

        candles = fetch_candles(symbol, "15min", outputsize=5)
        if not candles:
            continue

        price    = float(candles[0]["close"])
        flag     = {"GBP/JPY":"🇬🇧🇯🇵","EUR/JPY":"🇪🇺🇯🇵","XAU/USD":"🥇💵","USD/JPY":"🇺🇸🇯🇵"}.get(symbol,"📈")
        tf_label = TIMEFRAMES.get(tf, {}).get("label", tf)
        dir_emoji = "🟢 BUY" if direction == "BUY" else "🔴 SELL"
        strat_tag = "🕯 No-Wick" if strategy == "NOWICK" else "📦 Range Box"

        def sl_hit():
            return (direction == "BUY"  and price <= current_sl) or \
                   (direction == "SELL" and price >= current_sl)

        def tp_hit(tp_level):
            return (direction == "BUY"  and price >= tp_level) or \
                   (direction == "SELL" and price <= tp_level)

        async def notify(msg):
            if alerts_enabled:
                await bot.send_message(chat_id=CHAT_ID,    text=msg, parse_mode="Markdown")
                await bot.send_message(chat_id=CHANNEL_ID, text=msg, parse_mode="Markdown")

        # ── Stage 0: waiting for TP1 or SL ──
        if stage == 0:
            if sl_hit():
                db_close_trade(trade_id, "LOSS", price, -1.0)
                learn_from_trade(trade_id, won=False)
                await notify(
                    f"🔴 *SL HIT* — Trade #{trade_id}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{flag} *{symbol}* — {tf_label}\n"
                    f"{strat_tag}\n"
                    f"Direction : {dir_emoji}\n"
                    f"Outcome   : *LOSS*  (-1.0R)\n"
                    f"Close     : `{price}`\n"
                    f"🕐 {now_wat()}"
                )
                log.info(f"[#{trade_id}] SL hit at stage 0")

            elif tp_hit(tp1):
                # Partial close at TP1, move SL to breakeven
                db_update_trade_stage(trade_id, 1, entry)
                await notify(
                    f"🟡 *TP1 HIT — Running to TP2* #{trade_id}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{flag} *{symbol}* — {tf_label}\n"
                    f"{strat_tag}\n"
                    f"Direction : {dir_emoji}\n"
                    f"✅ Partial close at TP1 : `{tp1}` *(+1.0R)*\n"
                    f"🛡 SL moved to breakeven : `{entry}`\n"
                    f"🎯 Now targeting TP2    : `{tp2}` *(+2.0R)*\n"
                    f"🕐 {now_wat()}"
                )
                log.info(f"[#{trade_id}] TP1 hit — SL moved to BE, watching TP2")

        # ── Stage 1: SL at BE, waiting for TP2 ──
        elif stage == 1:
            if sl_hit():
                db_close_trade(trade_id, "BE", price, 0.0)
                learn_from_trade(trade_id, won=False)
                await notify(
                    f"⚪ *BREAKEVEN* — Trade #{trade_id}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{flag} *{symbol}* — {tf_label}\n"
                    f"{strat_tag}\n"
                    f"Direction : {dir_emoji}\n"
                    f"Outcome   : *BREAKEVEN*  (0.0R)\n"
                    f"_TP1 was banked (+1R). Remainder closed at BE._\n"
                    f"Close     : `{price}`\n"
                    f"🕐 {now_wat()}"
                )
                log.info(f"[#{trade_id}] BE hit at stage 1")

            elif tp_hit(tp2):
                # Move SL to TP1, target TP3
                db_update_trade_stage(trade_id, 2, tp1)
                await notify(
                    f"🟢 *TP2 HIT — Running to TP3* #{trade_id}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{flag} *{symbol}* — {tf_label}\n"
                    f"{strat_tag}\n"
                    f"Direction : {dir_emoji}\n"
                    f"✅ Partial close at TP2 : `{tp2}` *(+2.0R)*\n"
                    f"🛡 SL moved to TP1      : `{tp1}`\n"
                    f"🎯 Now targeting TP3    : `{tp3}` *(+3.0R)*\n"
                    f"🕐 {now_wat()}"
                )
                log.info(f"[#{trade_id}] TP2 hit — SL moved to TP1, watching TP3")

        # ── Stage 2: SL at TP1, waiting for TP3 ──
        elif stage == 2:
            if sl_hit():
                db_close_trade(trade_id, "WIN", price, 1.0)
                learn_from_trade(trade_id, won=True)
                await notify(
                    f"🟡 *CLOSED AT TP1 PROTECTION* — Trade #{trade_id}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{flag} *{symbol}* — {tf_label}\n"
                    f"{strat_tag}\n"
                    f"Direction : {dir_emoji}\n"
                    f"Outcome   : *WIN*  (+1.0R on remainder)\n"
                    f"_TP1 & TP2 banked. Remainder closed at TP1 lock._\n"
                    f"Close     : `{price}`\n"
                    f"🕐 {now_wat()}"
                )
                log.info(f"[#{trade_id}] Closed at TP1 protection (stage 2)")

            elif tp_hit(tp3):
                db_close_trade(trade_id, "WIN", price, 3.0)
                learn_from_trade(trade_id, won=True)
                await notify(
                    f"🏆 *TP3 FULL TARGET HIT* — Trade #{trade_id}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{flag} *{symbol}* — {tf_label}\n"
                    f"{strat_tag}\n"
                    f"Direction : {dir_emoji}\n"
                    f"Outcome   : *WIN*  *(+3.0R)* 🔥\n"
                    f"Close     : `{price}`\n"
                    f"🕐 {now_wat()}"
                )
                log.info(f"[#{trade_id}] 🏆 TP3 full target hit!")

        await asyncio.sleep(0.5)


# ── DATA FETCHER ────────────────────────────────────────────────────
def fetch_candles(symbol: str, interval: str, outputsize: int = 60):
    url    = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": interval, "outputsize": outputsize,
              "apikey": TWELVEDATA_KEY, "format": "JSON", "timezone": "UTC"}
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


# ── TREND FILTER ─────────────────────────────────────────────────────
def detect_trend(candles_newest_first, period_fast=9, period_slow=21):
    """
    Simple, robust trend filter using EMA slope + alignment.
    Returns 'UP', 'DOWN', or 'FLAT'.
    """
    closes = [float(c["close"]) for c in reversed(candles_newest_first)]
    ef = calc_ema(closes, period_fast)
    es = calc_ema(closes, period_slow)
    if not ef or not es or len(ef) < 3:
        return "FLAT"
    fast_now, fast_prev = ef[-1], ef[-3]
    bull = ef[-1] > es[-1] and fast_now > fast_prev
    bear = ef[-1] < es[-1] and fast_now < fast_prev
    if bull: return "UP"
    if bear: return "DOWN"
    return "FLAT"


# ── CANDLE DETECTOR — single-sided no-wick, trend-aligned ────────────
def analyse_candle(candle, symbol, tf_cfg, trend: str):
    """
    His exact rule:
      UP trend   + bullish candle with NO bottom wick → marks a BUY level
      DOWN trend + bearish candle with NO top wick    → marks a SELL level
    A no-wick candle against the trend, or in a flat market, is ignored.
    This function only MARKS the level — entry happens later on retest.
    """
    o,h,l,c = float(candle["open"]),float(candle["high"]),float(candle["low"]),float(candle["close"])
    pu = pip_unit(symbol)
    body = abs(c-o); body_pips = body/pu
    is_bull = c > o
    upper_wick = h - max(o,c); lower_wick = min(o,c) - l
    stats = {"open":o,"high":h,"low":l,"close":c,
             "body":round(body,5),"body_pips":round(body_pips,1),
             "upper_wick":round(upper_wick,5),"lower_wick":round(lower_wick,5),
             "time":candle["datetime"],"is_bull":is_bull}

    if body_pips < tf_cfg["min_body_pips"]:
        return None, stats

    tol = tf_cfg["wick_tol"] * body

    # BUY setup: uptrend + bullish candle + no bottom wick
    if trend == "UP" and is_bull and lower_wick <= tol:
        stats["level"] = round(l, 5)   # the wickless edge — this is the retest trigger price
        return "BUY", stats

    # SELL setup: downtrend + bearish candle + no top wick
    if trend == "DOWN" and not is_bull and upper_wick <= tol:
        stats["level"] = round(h, 5)   # the wickless edge — this is the retest trigger price
        return "SELL", stats

    return None, stats


# ── PERSISTENT PENDING LEVELS (survives Railway restarts) ────────────
# No longer an in-memory dict — everything lives in SQLite.
# Age is tracked in CANDLE units (not scan units) so a 1H mark
# correctly ages 1 count per closed 1H candle, not per 5-min scan.

def db_mark_level(symbol, tf_key, direction, level, candle_time):
    """Persist a newly marked no-wick level to the DB."""
    with db_connect() as con:
        # Skip if this exact candle/direction is already marked
        exists = con.execute("""
            SELECT COUNT(*) FROM pending_levels
            WHERE symbol=? AND tf_key=? AND direction=? AND candle_time=? AND active=1
        """, (symbol, tf_key, direction, candle_time)).fetchone()[0]
        if exists:
            return
        con.execute("""
            INSERT INTO pending_levels
              (symbol, tf_key, direction, level, candle_time, last_candle, age_candles, active)
            VALUES (?,?,?,?,?,?,0,1)
        """, (symbol, tf_key, direction, level, candle_time, candle_time))
        con.commit()
    log.info(f"[{symbol}/{tf_key}] 📌 Saved {direction} level {level} to DB (candle {candle_time})")


def db_get_pending_levels(symbol, tf_key):
    """Return all active pending levels for a symbol/tf."""
    with db_connect() as con:
        rows = con.execute("""
            SELECT id, direction, level, candle_time, age_candles
            FROM pending_levels
            WHERE symbol=? AND tf_key=? AND active=1
            ORDER BY id ASC
        """, (symbol, tf_key)).fetchall()
    return [{"id": r[0], "direction": r[1], "level": r[2],
             "candle_time": r[3], "age_candles": r[4]} for r in rows]


def db_expire_level(level_id):
    with db_connect() as con:
        con.execute("UPDATE pending_levels SET active=0 WHERE id=?", (level_id,))
        con.commit()


def db_consume_level(level_id):
    """Mark a level as consumed (retested + triggered)."""
    with db_connect() as con:
        con.execute("UPDATE pending_levels SET active=0 WHERE id=?", (level_id,))
        con.commit()


def db_age_levels(symbol, tf_key, current_candle_time):
    """
    Increment age for all active levels for this symbol/tf,
    but ONLY if this is a new candle close (not a repeated scan of the same candle).
    This ensures age = real candle count, not scan count.
    """
    with db_connect() as con:
        con.execute("""
            UPDATE pending_levels
            SET age_candles = age_candles + 1,
                last_candle = ?
            WHERE symbol=? AND tf_key=? AND active=1
              AND last_candle != ?
        """, (current_candle_time, symbol, tf_key, current_candle_time))
        con.commit()
        # Expire levels that have aged beyond the max
        con.execute("""
            UPDATE pending_levels SET active=0
            WHERE symbol=? AND tf_key=? AND active=1
              AND age_candles > ?
        """, (symbol, tf_key, MAX_PENDING_AGE_CANDLES))
        con.commit()





RETEST_TOLERANCE_PCT    = 0.15   # how close price must come to level (% of candle body)
MAX_PENDING_AGE_CANDLES = 50     # expire a level after 50 CANDLE closes (not scans)


def check_retest(symbol, tf_key, latest_candle, tf_cfg) -> tuple:
    """
    DB-backed retest check. Ages levels by real candle closes, not scan count.
    Returns (direction, level_info) on first match, or (None, None).
    """
    candle_time = latest_candle["datetime"]

    # Age all levels for this symbol/tf — only ticks forward when candle actually advances
    db_age_levels(symbol, tf_key, candle_time)

    levels = db_get_pending_levels(symbol, tf_key)
    if not levels:
        return None, None

    h   = float(latest_candle["high"])
    l   = float(latest_candle["low"])
    pu  = pip_unit(symbol)
    tol = RETEST_TOLERANCE_PCT * max(tf_cfg["min_body_pips"], 1) * pu

    for p in levels:
        # Never trigger on the same candle that created the mark
        if p["candle_time"] == candle_time:
            continue
        lvl    = p["level"]
        tapped = (l - tol) <= lvl <= (h + tol)
        if tapped:
            db_consume_level(p["id"])
            log.info(f"[{symbol}/{tf_key}] ✅ Retest of {p['direction']} level {lvl} "
                     f"after {p['age_candles']} candles")
            return p["direction"], p

    return None, None


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


# ══════════════════════════════════════════════════════════════════════
#  RANGE BOX STRATEGY ("Magic Lines") — reversal at extremes
#  Daily high/low defines the box. 15-min pin-bar/rejection candle at
#  the extreme triggers entry. Complementary to the No-Wick trend engine.
# ══════════════════════════════════════════════════════════════════════
RANGE_CFG = {
    "daily_lookback":   20,     # how many daily candles to scan for box high/low
    "entry_interval":   "15min",
    "entry_candles":    50,
    "zone_pct":         0.20,   # price must be within 20% of box range from an edge to qualify
    "min_wick_ratio":   0.55,   # rejection wick must be >= 55% of the candle's total range
    "max_body_ratio":   0.40,   # body must be <= 40% of the candle's total range (small body)
}


def get_daily_box(symbol: str) -> dict | None:
    """
    Builds the 'magic line' box: highest high and lowest low over the
    daily lookback window. Returns None if data unavailable.
    """
    candles = fetch_candles(symbol, "1day", outputsize=RANGE_CFG["daily_lookback"])
    if not candles or len(candles) < 10:
        return None
    highs = [float(c["high"]) for c in candles]
    lows  = [float(c["low"])  for c in candles]
    box_high = max(highs)
    box_low  = min(lows)
    return {"high": box_high, "low": box_low, "range": box_high - box_low}


def classify_zone(price: float, box: dict) -> str:
    """Returns 'TOP', 'BOTTOM', or 'MIDDLE' depending on where price sits in the box."""
    rng = box["range"]
    if rng <= 0:
        return "MIDDLE"
    pct_from_low = (price - box["low"]) / rng
    if pct_from_low >= (1 - RANGE_CFG["zone_pct"]):
        return "TOP"
    if pct_from_low <= RANGE_CFG["zone_pct"]:
        return "BOTTOM"
    return "MIDDLE"


def detect_pitchfork_candle(candle: dict, symbol: str, zone: str):
    """
    Detects a rejection ('pitchfork'/pin-bar) candle:
      TOP zone    → bearish rejection: long upper wick, small body  → SELL
      BOTTOM zone → bullish rejection: long lower wick, small body  → BUY
    Returns (direction, stats) or (None, stats)
    """
    o, h, l, c = float(candle["open"]), float(candle["high"]), float(candle["low"]), float(candle["close"])
    pu = pip_unit(symbol)
    total_range = h - l
    if total_range <= 0:
        return None, {}

    body       = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    body_ratio  = body / total_range
    upper_ratio = upper_wick / total_range
    lower_ratio = lower_wick / total_range

    stats = {
        "open": o, "high": h, "low": l, "close": c,
        "body_pips": round(body / pu, 1),
        "range_pips": round(total_range / pu, 1),
        "upper_wick": round(upper_wick, 5), "lower_wick": round(lower_wick, 5),
        "upper_ratio": round(upper_ratio, 2), "lower_ratio": round(lower_ratio, 2),
        "body_ratio": round(body_ratio, 2),
        "time": candle["datetime"],
    }

    if body_ratio > RANGE_CFG["max_body_ratio"]:
        return None, stats   # body too large to be a clean rejection candle

    if zone == "TOP" and upper_ratio >= RANGE_CFG["min_wick_ratio"]:
        return "SELL", stats
    if zone == "BOTTOM" and lower_ratio >= RANGE_CFG["min_wick_ratio"]:
        return "BUY", stats
    return None, stats


def range_confluence(symbol: str, zone: str, direction: str, box: dict, recent_candles: list) -> dict:
    """
    Lightweight confluence scoring for range-reversal signals, weighted by memory.
    Factors:
      1. Zone strength — how close to the absolute extreme (closer = stronger)
      2. RSI extreme — oversold at bottom / overbought at top supports reversal
      3. Rejection wick quality — bigger wick = stronger rejection
      4. No prior break of the box on the last few candles (range still holding)
    """
    weights = db_get_factor_weights(symbol, "15min", direction, strategy="RANGEBOX")
    score, checks, factor_hits = 0, {}, {}
    pu = pip_unit(symbol)

    price = float(recent_candles[0]["close"])
    rng   = box["range"]
    pct_from_low = (price - box["low"]) / rng if rng > 0 else 0.5

    # 1. Zone strength
    extremity = pct_from_low if direction == "SELL" else (1 - pct_from_low)
    zone_ok = extremity >= (1 - RANGE_CFG["zone_pct"])
    w = weights.get("zone_strength", 1.0)
    hist = f" _(hist×{w})_" if w != 1.0 else ""
    checks["zone_strength"] = f"Price at {'top' if direction=='SELL' else 'bottom'} of box ({round(extremity*100,1)}% extreme) — {'✅' if zone_ok else '⚠️'}{hist}"
    if zone_ok: score += round(2 * w); factor_hits["zone_strength"] = True
    else: factor_hits["zone_strength"] = False

    # 2. RSI extreme
    closes = [float(c["close"]) for c in reversed(recent_candles)]
    rsi = calc_rsi(closes, 14)
    if rsi is not None:
        if direction == "SELL":
            rsi_ok = rsi >= 60
            label  = "✅ Overbought — supports reversal" if rsi_ok else "⚠️ Not overbought"
        else:
            rsi_ok = rsi <= 40
            label  = "✅ Oversold — supports reversal" if rsi_ok else "⚠️ Not oversold"
        w = weights.get("rsi_extreme", 1.0)
        hist = f" _(hist×{w})_" if w != 1.0 else ""
        checks["rsi_extreme"] = f"RSI {rsi} — {label}{hist}"
        if rsi_ok: score += round(2 * w); factor_hits["rsi_extreme"] = True
        else: factor_hits["rsi_extreme"] = False

    # 3. Rejection wick quality (re-derived from latest candle)
    _, candle_stats = detect_pitchfork_candle(recent_candles[0], symbol, zone)
    wick_ratio = candle_stats.get("upper_ratio" if direction == "SELL" else "lower_ratio", 0)
    wick_strong = wick_ratio >= 0.65
    w = weights.get("wick_quality", 1.0)
    hist = f" _(hist×{w})_" if w != 1.0 else ""
    checks["wick_quality"] = f"Rejection wick {round(wick_ratio*100,1)}% of candle — {'✅ Strong' if wick_strong else '⚠️ Moderate'}{hist}"
    if wick_strong: score += round(2 * w); factor_hits["wick_quality"] = True
    else: factor_hits["wick_quality"] = False

    # 4. Box still holding — no recent close beyond the box edge
    last5 = recent_candles[:5]
    if direction == "SELL":
        held = all(float(c["close"]) <= box["high"] + (rng * 0.02) for c in last5)
    else:
        held = all(float(c["close"]) >= box["low"] - (rng * 0.02) for c in last5)
    w = weights.get("box_intact", 1.0)
    hist = f" _(hist×{w})_" if w != 1.0 else ""
    checks["box_intact"] = f"Box {'holding' if held else 'broken'} on last 5 candles — {'✅' if held else '❌'}{hist}"
    if held: score += round(1 * w); factor_hits["box_intact"] = True
    else: factor_hits["box_intact"] = False

    stats = db_stats(symbol, "15min", direction, strategy="RANGEBOX")
    mem_line = ""
    if stats["total"] >= 3:
        mem_line = f"\n🧠 Memory: {stats['wins']}W/{stats['losses']}L ({stats['win_rate']}% WR on {stats['total']} trades)"

    return {"score": score, "max_score": 7, "checks": checks,
            "factor_hits": factor_hits, "mem_line": mem_line}


def calculate_range_levels(stats: dict, symbol: str, direction: str, box: dict) -> dict:
    """
    Range strategy trade levels (per the 'magic lines' rule):
      SELL: Entry = candle close, SL = above the rejection candle high, TP = bottom of box
      BUY : Entry = candle close, SL = below the rejection candle low,  TP = top of box
    """
    entry = stats["close"]
    dec   = decimals(symbol)
    pu    = pip_unit(symbol)

    if direction == "SELL":
        sl   = stats["high"]
        risk = sl - entry
        tp1  = entry - risk * 1.0
        tp2  = round((entry + box["low"]) / 2, dec)   # midpoint as a partial target
        tp3  = box["low"]
    else:
        sl   = stats["low"]
        risk = entry - sl
        tp1  = entry + risk * 1.0
        tp2  = round((entry + box["high"]) / 2, dec)
        tp3  = box["high"]

    risk = max(risk, pu)  # guard against zero-risk
    return {
        "entry": round(entry, dec), "sl": round(sl, dec),
        "tp1": round(tp1, dec), "tp2": round(tp2, dec), "tp3": round(tp3, dec),
        "risk_pips": round(risk / pu, 1),
        "tp1_r": round(abs(tp1 - entry) / risk, 1),
        "tp2_r": round(abs(tp2 - entry) / risk, 1),
        "tp3_r": round(abs(tp3 - entry) / risk, 1),
    }


def format_range_signal(symbol, direction, stats, levels, confluence, box, trade_id):
    flag  = {"GBP/JPY":"🇬🇧🇯🇵","EUR/JPY":"🇪🇺🇯🇵","XAU/USD":"🥇💵","USD/JPY":"🇺🇸🇯🇵"}.get(symbol,"📈")
    stars = "⭐" * min(confluence["score"], 5)
    checks_text = "\n".join(f"  {v}" for v in confluence["checks"].values())
    dir_line = "🟢 *BUY  (LONG)*" if direction == "BUY" else "🔴 *SELL (SHORT)*"
    wat_time = to_wat(stats["time"])
    mem_line = confluence.get("mem_line", "")
    dec = decimals(symbol)
    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 *MAGIC LINES — RANGE SIGNAL* #{trade_id}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{flag}  *{symbol}* — 15min entry (Daily box)\n"
        f"🕐 `{wat_time}`\n"
        f"{dir_line}\n"
        f"📊 Confluence: {stars} `{confluence['score']}/{confluence['max_score']}`\n"
        f"{mem_line}\n\n"
        f"*📐 Box*\n"
        f"  Top    : `{round(box['high'], dec)}`\n"
        f"  Bottom : `{round(box['low'], dec)}`\n\n"
        f"*📍 Trade Levels*\n"
        f"  ▶️  Entry : `{levels['entry']}`\n"
        f"  🛑  SL    : `{levels['sl']}` ({levels['risk_pips']} pips)\n"
        f"  🎯  TP1   : `{levels['tp1']}`  _({levels['tp1_r']}R)_\n"
        f"  🎯  TP2   : `{levels['tp2']}`  _({levels['tp2_r']}R, box mid)_\n"
        f"  🎯  TP3   : `{levels['tp3']}`  _({levels['tp3_r']}R, opposite line)_\n\n"
        f"*🔬 Analysis*\n{checks_text}\n\n"
        f"*📐 Rejection Candle*\n"
        f"  Body: `{stats['body_pips']} pips` | Range: `{stats['range_pips']} pips`\n"
        f"  Upper wick: `{stats['upper_ratio']*100:.0f}%` | Lower wick: `{stats['lower_ratio']*100:.0f}%`\n\n"
        f"⚠️ _Manage risk. Max 1-2% per trade._\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )


async def scan_range_box(bot: Bot, notify_no_signal: bool = False):
    """
    Scans all symbols for Magic-Lines range reversal setups:
      1. Build the daily box (high/low)
      2. Check where 15-min price sits (top/bottom/middle)
      3. Look for a pitchfork/rejection candle at the extreme
      4. Score confluence, apply memory-adjusted threshold, log + alert
    """
    signals_found = 0
    log.info("📦 [RANGE] Magic-Lines scan starting")

    for symbol in SYMBOLS:
        box = get_daily_box(symbol)
        if not box:
            continue

        candles = fetch_candles(symbol, RANGE_CFG["entry_interval"], outputsize=RANGE_CFG["entry_candles"])
        if not candles or len(candles) < 10:
            continue

        latest      = candles[0]
        candle_time = latest["datetime"]
        price       = float(latest["close"])
        zone        = classify_zone(price, box)

        if zone == "MIDDLE":
            log.info(f"[{symbol}/RANGE] Price in middle of box — staying out of the chop")
            continue

        direction, stats = detect_pitchfork_candle(latest, symbol, zone)
        if not direction or not stats:
            log.info(f"[{symbol}/RANGE] No pitchfork candle at {zone}")
            continue

        alert_key = f"{symbol}_RANGE_{direction}"
        if alerted.get(alert_key) == candle_time:
            continue

        if db_symbol_has_open_trade(symbol, strategy="RANGEBOX"):
            log.info(f"[{symbol}/RANGE] ⏸ Already have an open RANGEBOX trade")
            continue

        confluence = range_confluence(symbol, zone, direction, box, candles)
        score      = confluence["score"]
        min_conf   = memory_adjust_threshold(symbol, "15min", direction, strategy="RANGEBOX")
        # Range strategy max score is 7, scale base threshold down proportionally
        min_conf_scaled = round(min_conf * 7 / 8)

        log.info(f"[{symbol}/RANGE] {direction} score:{score} threshold:{min_conf_scaled}")
        if score < min_conf_scaled:
            log.info(f"[{symbol}/RANGE] ⚠️ Score too low, skipping")
            continue

        levels   = calculate_range_levels(stats, symbol, direction, box)
        trade_id = db_log_signal(
            symbol, "15min", direction,
            levels["entry"], levels["sl"], levels["tp1"], levels["tp2"], levels["tp3"],
            levels["risk_pips"], score, candle_time,
            strategy="RANGEBOX", factor_hits=confluence.get("factor_hits", {})
        )
        msg = format_range_signal(symbol, direction, stats, levels, confluence, box, trade_id)

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Taken",   callback_data=f"taken_{trade_id}"),
            InlineKeyboardButton("❌ Skipped", callback_data=f"skip_{trade_id}"),
        ]])

        if alerts_enabled:
            await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown", reply_markup=keyboard)
            log.info(f"[{symbol}/RANGE] 🚀 Signal #{trade_id} sent!")
        else:
            log.info(f"[{symbol}/RANGE] 🔕 Signal #{trade_id} found but alerts OFF")

        alerted[alert_key] = candle_time
        signals_found += 1
        await asyncio.sleep(1)

    if notify_no_signal and signals_found == 0:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=f"🔍 *Range Box scan complete* — no setups at the extremes right now.\n🕐 {now_wat()}",
            parse_mode="Markdown",
        )


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


# ── RETEST TRADE LEVELS & FORMATTER ─────────────────────────────────
def calculate_retest_trade_levels(retest_stats, symbol, tf_key, direction, atr):
    """
    Entry  = close of the retest candle (the touch-and-go)
    SL     = just beyond the ORIGINAL marked no-wick level (the structure being defended),
             with a small buffer; ATR used as a floor if the raw distance is too tight.
    TPs    = timeframe-scaled R-multiples off that risk, same as before.
    """
    entry = retest_stats["close"]
    dec   = decimals(symbol)
    pu    = pip_unit(symbol)
    marked_level = retest_stats["marked_level"]
    buffer = max(atr * 0.25, pu * 2) if atr else pu * 3

    tp1_m, tp2_m, tp3_m = TP_MULTIPLIERS[tf_key]

    if direction == "BUY":
        sl   = marked_level - buffer
        risk = entry - sl
        if atr and atr > risk:
            sl   = round(entry - atr, dec); risk = atr
        return {"entry": round(entry,dec), "sl": round(sl,dec),
                "tp1": round(entry+tp1_m*risk,dec), "tp2": round(entry+tp2_m*risk,dec),
                "tp3": round(entry+tp3_m*risk,dec), "risk_pips": round(risk/pu,1),
                "tp1_r": tp1_m, "tp2_r": tp2_m, "tp3_r": tp3_m}
    else:
        sl   = marked_level + buffer
        risk = sl - entry
        if atr and atr > risk:
            sl   = round(entry + atr, dec); risk = atr
        return {"entry": round(entry,dec), "sl": round(sl,dec),
                "tp1": round(entry-tp1_m*risk,dec), "tp2": round(entry-tp2_m*risk,dec),
                "tp3": round(entry-tp3_m*risk,dec), "risk_pips": round(risk/pu,1),
                "tp1_r": tp1_m, "tp2_r": tp2_m, "tp3_r": tp3_m}


def format_retest_signal(symbol, tf_key, direction, retest_stats, levels, confluence, trade_id, trend):
    flag  = {"GBP/JPY":"🇬🇧🇯🇵","EUR/JPY":"🇪🇺🇯🇵","XAU/USD":"🥇💵","USD/JPY":"🇺🇸🇯🇵"}.get(symbol,"📈")
    stars = "⭐" * min(confluence["score"], 5)
    checks_text = "\n".join(f"  {v}" for v in confluence["checks"].values())
    dir_line = "🟢 *BUY  (LONG)*" if direction == "BUY" else "🔴 *SELL (SHORT)*"
    trend_label = "📈 Uptrend" if trend == "UP" else ("📉 Downtrend" if trend == "DOWN" else "➡️ Flat")
    wat_time = to_wat(retest_stats["time"])
    marked_wat = to_wat(retest_stats["marked_candle_time"])
    mem_line = confluence.get("mem_line", "")
    dec = decimals(symbol)
    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕯 *NO-WICK RETEST SIGNAL* #{trade_id}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{flag}  *{symbol}* — {TIMEFRAMES[tf_key]['label']}\n"
        f"🕐 `{wat_time}`  |  Trend: {trend_label}\n"
        f"{dir_line}\n"
        f"📊 Confluence: {stars} `{confluence['score']}/{confluence['max_score']}`\n"
        f"{mem_line}\n\n"
        f"*📌 Setup*\n"
        f"  No-wick level marked: `{round(retest_stats['marked_level'], dec)}`\n"
        f"  Marked at: `{marked_wat}`\n"
        f"  ✅ Price retested — entering now\n\n"
        f"*📍 Trade Levels*\n"
        f"  ▶️  Entry : `{levels['entry']}`\n"
        f"  🛑  SL    : `{levels['sl']}` ({levels['risk_pips']} pips)\n"
        f"  🎯  TP1   : `{levels['tp1']}`  _({levels['tp1_r']}R)_\n"
        f"  🎯  TP2   : `{levels['tp2']}`  _({levels['tp2_r']}R)_\n"
        f"  🎯  TP3   : `{levels['tp3']}`  _({levels['tp3_r']}R)_\n\n"
        f"*🔬 Analysis*\n{checks_text}\n\n"
        f"⚠️ _Manage risk. Max 1-2% per trade._\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )


# ── CHANNEL SIGNAL FORMATTER (clean, subscriber-friendly) ───────────
def format_signal_channel(symbol, tf_key, direction, stats, levels, confluence, trade_id):
    """Clean format for public channel — no debug info, no trade ID, no buttons."""
    flag     = {"GBP/JPY":"🇬🇧🇯🇵","EUR/JPY":"🇪🇺🇯🇵","XAU/USD":"🥇💵","USD/JPY":"🇺🇸🇯🇵"}.get(symbol,"📈")
    stars    = "⭐" * min(confluence["score"], 5)
    dir_line = "🟢 *BUY  (LONG)*" if direction == "BUY" else "🔴 *SELL (SHORT)*"
    wat_time = to_wat(stats["time"])
    tf_label = TIMEFRAMES[tf_key]["label"]

    return (
        f"╔══════════════════════════╗\n"
        f"      📡 *NEW SIGNAL*\n"
        f"╚══════════════════════════╝\n\n"
        f"{flag}  *{symbol}*\n"
        f"⏱ {tf_label}   |   🕐 `{wat_time}`\n\n"
        f"{dir_line}\n"
        f"📊 Strength: {stars}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*📍 Trade Levels*\n"
        f"  ▶️  Entry : `{levels['entry']}`\n"
        f"  🛑  SL    : `{levels['sl']}` ({levels['risk_pips']} pips)\n"
        f"  🎯  TP1   : `{levels['tp1']}`  _({levels['tp1_r']}R)_\n"
        f"  🎯  TP2   : `{levels['tp2']}`  _({levels['tp2_r']}R)_\n"
        f"  🎯  TP3   : `{levels['tp3']}`  _({levels['tp3_r']}R)_\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚠️ _Always manage your risk. Max 1-2% per trade._\n"
        f"📲 _Powered by No-Wick AI Signal Bot_"
    )


# ── SCANNER ─────────────────────────────────────────────────────────
async def scan_timeframe(bot: Bot, tf_key: str, notify_no_signal: bool = False):
    """
    Two-stage pipeline (per the no-wick retest strategy):
      1. MARK — find a trend-aligned no-wick candle, store its wickless edge as a level
      2. RETEST — on later candles, check if price has come back and tapped that level
      3. ENTER — fire the signal only on the retest, not on the original candle
    """
    tf_cfg = TIMEFRAMES[tf_key]
    signals_found = 0
    log.info(f"🔭 [{tf_key}] Research scan starting")

    for symbol in SYMBOLS:
        candles = fetch_candles(symbol, tf_cfg["interval"], outputsize=tf_cfg["candles"])
        if not candles or len(candles) < 30:
            continue

        trend = detect_trend(candles)
        latest = candles[0]
        candle_time = latest["datetime"]

        # ── Stage 1: MARK — does the latest candle qualify as a new no-wick level? ──
        mark_direction, mark_stats = analyse_candle(latest, symbol, tf_cfg, trend)
        if mark_direction:
            mark_key = f"{symbol}_{tf_key}_{mark_direction}_marked"
            if alerted.get(mark_key) != candle_time:
                db_mark_level(symbol, tf_key, mark_direction, mark_stats["level"], candle_time)
                alerted[mark_key] = candle_time
                log.info(f"[{symbol}/{tf_key}] 📌 {mark_direction} level marked at {mark_stats['level']}")

        # ── Stage 2: RETEST — has price tapped any pending level? ──
        retest_direction, level_info = check_retest(symbol, tf_key, latest, tf_cfg)
        if not retest_direction:
            log.info(f"[{symbol}/{tf_key}] Trend:{trend} — no retest trigger this candle")
            continue

        direction = retest_direction
        alert_key = f"{symbol}_{tf_key}_{direction}_entry"
        if alerted.get(alert_key) == candle_time:
            continue

        # One open trade per symbol *per strategy* — don't stack positions within a strategy
        if db_symbol_has_open_trade(symbol, strategy="NOWICK"):
            log.info(f"[{symbol}/{tf_key}] ⏸ Skipping — already have an open NOWICK trade on {symbol}")
            continue

        # Build stats for the RETEST candle (this is the entry trigger, not the original mark)
        o,h,l,c = float(latest["open"]),float(latest["high"]),float(latest["low"]),float(latest["close"])
        pu = pip_unit(symbol)
        retest_stats = {
            "open": o, "high": h, "low": l, "close": c,
            "body_pips": round(abs(c-o)/pu, 1),
            "upper_wick": round(h - max(o,c), 5),
            "lower_wick": round(min(o,c) - l, 5),
            "time": candle_time,
            "marked_level": level_info["level"],
            "marked_candle_time": level_info["candle_time"],
        }

        log.info(f"[{symbol}/{tf_key}] ✅ RETEST of {direction} level {level_info['level']} — deep analysis + memory check...")
        confluence = research_confluence(candles, symbol, tf_cfg, direction)
        score      = confluence["score"]

        # Memory-adjusted threshold
        min_conf = memory_adjust_threshold(symbol, tf_cfg["interval"], direction)
        log.info(f"[{symbol}/{tf_key}] {direction} score:{score} threshold:{min_conf}")

        if score < min_conf:
            log.info(f"[{symbol}/{tf_key}] ⚠️ Score {score} < threshold {min_conf}, skipping")
            continue

        levels   = calculate_retest_trade_levels(retest_stats, symbol, tf_key, direction, confluence["atr"])
        trade_id = db_log_signal(
            symbol, tf_cfg["interval"], direction,
            levels["entry"], levels["sl"], levels["tp1"], levels["tp2"], levels["tp3"],
            levels["risk_pips"], score, candle_time,
            strategy="NOWICK", factor_hits=confluence.get("factor_hits", {})
        )
        msg = format_retest_signal(symbol, tf_key, direction, retest_stats, levels, confluence, trade_id, trend)

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Taken",   callback_data=f"taken_{trade_id}"),
            InlineKeyboardButton("❌ Skipped", callback_data=f"skip_{trade_id}"),
        ]])

        if alerts_enabled:
            # Private chat — full detail with admin buttons
            await bot.send_message(
                chat_id=CHAT_ID,
                text=msg,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            # Public channel — clean subscriber-friendly format, no buttons
            channel_msg = format_signal_channel(symbol, tf_key, direction, retest_stats, levels, confluence, trade_id)
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=channel_msg,
                parse_mode="Markdown",
            )
            log.info(f"[{symbol}/{tf_key}] 🚀 Signal #{trade_id} sent to private + channel!")
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
    await scan_range_box(bot, notify_no_signal)


# ── COMMAND HANDLERS ────────────────────────────────────────────────
async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *No-Wick Signal Bot* — Generative Memory Edition\n\n"
        "*Strategies:*\n"
        "  🕯 No-Wick — trend-following Marubozu (1H/4H/Day)\n"
        "  📦 Magic Lines — range reversal at box extremes (15m)\n\n"
        "*Timeframes:*\n"
        "  📊 1H  — Swing (every 5 min)\n"
        "  📊 4H  — Position (every 15 min)\n"
        "  📊 Day — Daily (every 1 hr)\n"
        "  📦 Range — every 10 min\n\n"
        "*Signals:* 🟢 BUY & 🔴 SELL\n"
        "*Memory:* Bot learns from past trades, per strategy ✅\n"
        "*Timezone:* WAT (Abuja) 🇳🇬\n"
        "*Channel:* Signals auto-posted to subscribers 📡\n\n"
        "*Commands:*\n"
        "  /scan /scan1h /scan4h /scanday /scanrange\n"
        "  /status — live bot status\n"
        "  /performance — full win/loss stats\n"
        "  /history — last 10 closed trades\n"
        "  /open — currently open trades\n"
        "  /close — manually close a trade\n"
        "  /backup — download trade memory DB\n"
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

async def cmd_scan_range(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📦 Scanning Magic Lines range boxes...")
    await scan_range_box(context.application.bot, notify_no_signal=True)

async def cmd_alert(update, context: ContextTypes.DEFAULT_TYPE):
    global alerts_enabled
    alerts_enabled = not alerts_enabled
    state = "🔔 ON" if alerts_enabled else "🔕 OFF"
    await update.message.reply_text(f"*Alerts: {state}*", parse_mode="Markdown")

async def cmd_status(update, context: ContextTypes.DEFAULT_TYPE):
    state = "🔔 ON" if alerts_enabled else "🔕 OFF"
    lines = [f"*Bot Status* | Alerts: {state} | {now_wat()}\n"]
    lines.append("*🕯 No-Wick Strategy*")
    for tf_key, tf_cfg in TIMEFRAMES.items():
        lines.append(f"*— {tf_cfg['label']} —*")
        for sym in SYMBOLS:
            b = alerted.get(f"{sym}_{tf_key}_BUY",  "—")
            s = alerted.get(f"{sym}_{tf_key}_SELL", "—")
            lines.append(f"  • {sym}  🟢`{b}` 🔴`{s}`")
    lines.append("\n*📦 Magic Lines (Range Box)*")
    for sym in SYMBOLS:
        b = alerted.get(f"{sym}_RANGE_BUY",  "—")
        s = alerted.get(f"{sym}_RANGE_SELL", "—")
        lines.append(f"  • {sym}  🟢`{b}` 🔴`{s}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_performance(update, context: ContextTypes.DEFAULT_TYPE):
    lines = [f"*📊 Performance Report*\n🕐 {now_wat()}\n"]
    lines.append("*🕯 NO-WICK STRATEGY*")
    nowick_start = len(lines)
    for sym in SYMBOLS:
        for tf_key, tf_cfg in TIMEFRAMES.items():
            for d in ["BUY", "SELL"]:
                st = db_stats(sym, tf_cfg["interval"], d, strategy="NOWICK")
                if st["total"] == 0: continue
                bar = "🟩" * st["wins"] + "🟥" * st["losses"]
                lines.append(
                    f"*{sym} {tf_cfg['label']} {d}*\n"
                    f"  {bar}\n"
                    f"  {st['wins']}W / {st['losses']}L / {st['be']}BE  —  Win rate: *{st['win_rate']}%*\n"
                )
    if len(lines) == nowick_start:
        lines.append("No closed No-Wick trades yet.\n")

    lines.append("*📦 MAGIC LINES (RANGE BOX)*")
    range_start = len(lines)
    for sym in SYMBOLS:
        for d in ["BUY", "SELL"]:
            st = db_stats(sym, "15min", d, strategy="RANGEBOX")
            if st["total"] == 0: continue
            bar = "🟩" * st["wins"] + "🟥" * st["losses"]
            lines.append(
                f"*{sym} Range {d}*\n"
                f"  {bar}\n"
                f"  {st['wins']}W / {st['losses']}L / {st['be']}BE  —  Win rate: *{st['win_rate']}%*\n"
            )
    if len(lines) == range_start:
        lines.append("No closed Range Box trades yet.")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_history(update, context: ContextTypes.DEFAULT_TYPE):
    with db_connect() as con:
        rows = con.execute(
            "SELECT id, symbol, timeframe, direction, entry, outcome, pnl_r, close_time, strategy "
            "FROM trades WHERE outcome != 'OPEN' ORDER BY id DESC LIMIT 10"
        ).fetchall()
    if not rows:
        await update.message.reply_text("No closed trades yet.")
        return
    lines = [f"*📜 Last 10 Closed Trades*\n"]
    for r in rows:
        tid, sym, tf, d, entry, outcome, pnl, ctime, strategy = r
        emoji = "🟢" if outcome=="WIN" else ("🔴" if outcome=="LOSS" else "⚪")
        strat_icon = "🕯" if strategy == "NOWICK" else "📦"
        pnl_str = f"{'+' if pnl and pnl > 0 else ''}{pnl}R" if pnl else "—"
        lines.append(f"{emoji}{strat_icon} #{tid} *{sym}* {d} | {outcome} {pnl_str} | `{ctime or '—'}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_open(update, context: ContextTypes.DEFAULT_TYPE):
    open_trades = db_get_open_trades()
    if not open_trades:
        await update.message.reply_text("✅ No open trades right now.")
        return
    lines = [f"*📂 Open Trades ({len(open_trades)})*\n"]
    for r in open_trades:
        tid, sym, tf, d, entry, sl, current_sl, tp1, tp2, tp3, stage, strategy = r
        emoji = "🟢" if d=="BUY" else "🔴"
        strat_icon = "🕯" if strategy == "NOWICK" else "📦"
        lines.append(f"{emoji}{strat_icon} #{tid} *{sym}* {d} | Entry:`{entry}` SL:`{sl}` TP1:`{tp1}`")
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
    learn_from_trade(tid, won=(outcome == "WIN"))
    emoji = "🟢" if outcome=="WIN" else ("🔴" if outcome=="LOSS" else "⚪")
    await update.message.reply_text(f"{emoji} Trade #{tid} closed as *{outcome}*.", parse_mode="Markdown")

async def cmd_levels(update, context: ContextTypes.DEFAULT_TYPE):
    """Show all pending no-wick levels currently waiting for a retest."""
    lines = [f"*📌 Pending No-Wick Levels*\n🕐 {now_wat()}\n"]
    found = False
    for tf_key, tf_cfg in TIMEFRAMES.items():
        for sym in SYMBOLS:
            levels = db_get_pending_levels(sym, tf_key)
            if not levels:
                continue
            found = True
            lines.append(f"*{sym} — {tf_cfg['label']}*")
            for p in levels:
                dir_emoji = "🟢" if p["direction"] == "BUY" else "🔴"
                wat_time  = to_wat(p["candle_time"])
                lines.append(
                    f"  {dir_emoji} {p['direction']} @ `{p['level']}` "
                    f"| Marked: `{wat_time}` "
                    f"| Age: {p['age_candles']} candles"
                )
    if not found:
        lines.append("No levels currently marked. Levels appear when a trend-aligned\nno-wick candle closes and disappear once retested or expired.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


    pairs = "\n".join(f"  • {s}" for s in SYMBOLS)
    tfs   = "\n".join(f"  • {k} — {v['label']}" for k,v in TIMEFRAMES.items())
    await update.message.reply_text(f"*Active Pairs*\n{pairs}\n\n*Timeframes*\n{tfs}", parse_mode="Markdown")

async def cmd_pairs(update, context: ContextTypes.DEFAULT_TYPE):
    pairs = "\n".join(f"  • {s}" for s in SYMBOLS)
    tfs   = "\n".join(f"  • {k} — {v['label']}" for k, v in TIMEFRAMES.items())
    await update.message.reply_text(
        f"*Active Pairs*\n{pairs}\n\n*Timeframes*\n{tfs}\n\n"
        f"*Range Box:* Daily high/low box, 15min entry",
        parse_mode="Markdown"
    )


async def cmd_backup(update, context: ContextTypes.DEFAULT_TYPE):
    """Send the SQLite database file as a Telegram document."""
    if not os.path.exists(DB_PATH):
        await update.message.reply_text("❌ No database file found yet.")
        return
    await update.message.reply_text(f"📦 Preparing backup... `{now_wat()}`", parse_mode="Markdown")
    try:
        with open(DB_PATH, "rb") as f:
            await context.bot.send_document(
                chat_id=CHAT_ID,
                document=f,
                filename=f"trade_memory_{datetime.now(WAT).strftime('%Y%m%d_%H%M')}.db",
                caption=(
                    f"🧠 *Trade Memory Backup*\n"
                    f"🕐 {now_wat()}\n"
                    f"_Keep this file safe — it contains all your trade history._"
                ),
                parse_mode="Markdown",
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Backup failed: {e}")


async def cmd_testchannel(update, context: ContextTypes.DEFAULT_TYPE):
    """Send a test message to the channel to verify the bot has access."""
    try:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=(
                f"📡 *Channel Test*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ Bot has successfully connected to this channel!\n"
                f"🕐 {now_wat()}\n"
                f"Signals will appear here automatically."
            ),
            parse_mode="Markdown",
        )
        await update.message.reply_text(f"✅ Test message sent to channel! Check your channel now.\nChannel ID used: `{CHANNEL_ID}`", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(
            f"❌ Failed to post to channel.\nError: `{e}`\n\nChannel ID used: `{CHANNEL_ID}`\n\nCheck:\n1. Bot is admin in channel\n2. Post Messages is enabled\n3. Channel ID is correct",
            parse_mode="Markdown"
        )


    """Send the SQLite database file as a Telegram document."""
    if not os.path.exists(DB_PATH):
        await update.message.reply_text("❌ No database file found yet.")
        return
    await update.message.reply_text(f"📦 Preparing backup... `{now_wat()}`", parse_mode="Markdown")
    try:
        with open(DB_PATH, "rb") as f:
            await context.bot.send_document(
                chat_id=CHAT_ID,
                document=f,
                filename=f"trade_memory_{datetime.now(WAT).strftime('%Y%m%d_%H%M')}.db",
                caption=(
                    f"🧠 *Trade Memory Backup*\n"
                    f"🕐 {now_wat()}\n"
                    f"_Keep this file safe — it contains all your trade history._"
                ),
                parse_mode="Markdown",
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Backup failed: {e}")

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

async def job_range(context: ContextTypes.DEFAULT_TYPE):
    await scan_range_box(context.bot)


# ── MAIN ────────────────────────────────────────────────────────────
def main():
    db_init()
    log.info("🚀 Starting No-Wick Bot — Generative Memory Edition")
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    for cmd, fn in [
        ("start",   cmd_start),   ("scan",    cmd_scan),
        ("scan1h",  cmd_scan_1h), ("scan4h",  cmd_scan_4h),
        ("scanday", cmd_scan_day),("scanrange", cmd_scan_range),
        ("status",  cmd_status),  ("levels",  cmd_levels),
        ("performance", cmd_performance), ("history", cmd_history),
        ("open",    cmd_open),    ("close",   cmd_close),
        ("pairs",   cmd_pairs),   ("alert",   cmd_alert),
        ("backup",  cmd_backup),
        ("testchannel", cmd_testchannel),
    ]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(CallbackQueryHandler(callback_handler))

    app.job_queue.run_repeating(job_1h,    interval=TIMEFRAMES["1H"]["scan_secs"],   first=10)
    app.job_queue.run_repeating(job_4h,    interval=TIMEFRAMES["4H"]["scan_secs"],   first=20)
    app.job_queue.run_repeating(job_day,   interval=TIMEFRAMES["1day"]["scan_secs"], first=30)
    app.job_queue.run_repeating(job_range, interval=600,                             first=40)  # every 10 min

    log.info("✅ Running — Memory active | WAT timezone | BUY & SELL | No-Wick + Range Box")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
