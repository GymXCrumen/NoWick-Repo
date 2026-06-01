"""
No-Wick Signal Bot — Multi-Timeframe Edition
Markets: GBP/JPY, EUR/JPY, XAU/USD, USD/JPY
Timeframes: 1H Swing | 4H Position | Daily Trade
Signals: BUY & SELL | Multi-TF Confluence Engine
"""

import os
import logging
import asyncio
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

# TP R-multiples per timeframe
TP_MULTIPLIERS = {
    "1H":   (1.0, 2.0, 3.0),
    "4H":   (1.5, 3.0, 5.0),
    "1day": (2.0, 4.0, 7.0),
}

MIN_CONFLUENCE = 5   # out of 8 — minimum score to fire a signal

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

alerted: dict = {}        # key: "SYMBOL_TF_direction" → candle datetime
alerts_enabled: bool = True


# ── DATA FETCHER ────────────────────────────────────────────────────
def fetch_candles(symbol: str, interval: str, outputsize: int = 60):
    url    = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": interval, "outputsize": outputsize,
              "apikey": TWELVEDATA_KEY, "format": "JSON"}
    try:
        r    = requests.get(url, params=params, timeout=15)
        data = r.json()
        if data.get("status") == "error":
            log.warning(f"[{symbol}/{interval}] API error: {data.get('message')}")
            return None
        return data.get("values", [])   # newest first
    except Exception as e:
        log.error(f"[{symbol}/{interval}] fetch error: {e}")
        return None


# ── INDICATORS ──────────────────────────────────────────────────────
def calc_ema(closes, period):
    if len(closes) < period:
        return []
    k   = 2 / (period + 1)
    ema = [sum(closes[:period]) / period]
    for p in closes[period:]:
        ema.append(p * k + ema[-1] * (1 - k))
    return ema


def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    return round(100 - (100 / (1 + ag / al)), 2)


def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = float(candles[i]["high"]), float(candles[i]["low"]), float(candles[i-1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return round(atr, 5)


def find_sr(candles, lookback=20):
    highs = [float(c["high"]) for c in candles[1: lookback + 1]]
    lows  = [float(c["low"])  for c in candles[1: lookback + 1]]
    return round(min(lows), 5), round(max(highs), 5)


def pip_unit(symbol):
    return 0.01 if "JPY" in symbol else (1.0 if "XAU" in symbol else 0.0001)


def decimals(symbol):
    return 3 if "JPY" in symbol or "XAU" in symbol else 5


# ── CANDLE DETECTOR — BUY & SELL ───────────────────────────────────
def analyse_candle(candle, symbol, tf_cfg):
    """
    Detects no-wick Marubozu candles in BOTH directions.
    BUY  = bullish body (close > open), tiny wicks
    SELL = bearish body (open > close), tiny wicks
    Returns (direction, stats) where direction is 'BUY', 'SELL', or None
    """
    o, h, l, c = float(candle["open"]), float(candle["high"]), float(candle["low"]), float(candle["close"])
    pu         = pip_unit(symbol)

    body        = abs(c - o)
    body_pips   = body / pu
    is_bull     = c > o
    upper_wick  = h - max(o, c)
    lower_wick  = min(o, c) - l

    stats = {
        "open": o, "high": h, "low": l, "close": c,
        "body": round(body, 5), "body_pips": round(body_pips, 1),
        "upper_wick": round(upper_wick, 5),
        "lower_wick": round(lower_wick, 5),
        "time": candle["datetime"],
        "is_bull": is_bull,
    }

    if body_pips < tf_cfg["min_body_pips"]:
        return None, stats                   # too small

    tol = tf_cfg["wick_tol"] * body
    if upper_wick > tol or lower_wick > tol:
        return None, stats                   # wicks too large

    direction = "BUY" if is_bull else "SELL"
    return direction, stats


# ── CONFLUENCE ENGINE ───────────────────────────────────────────────
def research_confluence(candles_newest_first, symbol, tf_cfg, direction):
    """
    Scores confluence for a given direction (BUY or SELL).
    8 points total — minimum MIN_CONFLUENCE required.
    """
    candles_old = list(reversed(candles_newest_first))
    closes      = [float(c["close"]) for c in candles_old]
    pu          = pip_unit(symbol)
    score, checks = 0, {}

    ef = calc_ema(closes, tf_cfg["ema_fast"])
    es = calc_ema(closes, tf_cfg["ema_slow"])

    # 1. EMA trend alignment
    if ef and es:
        trend_bull = ef[-1] > es[-1]
        aligned    = (direction == "BUY" and trend_bull) or (direction == "SELL" and not trend_bull)
        aligned_label = f"✅ Aligned with {direction}" if aligned else "❌ Against trend"
        checks["ema_trend"] = (
            f"EMA{tf_cfg['ema_fast']} {'>' if trend_bull else '<'} EMA{tf_cfg['ema_slow']} — {aligned_label}"
        )
        if aligned: score += 2

    # 2. Price vs slow EMA
    if es:
        price_bull = closes[-1] > es[-1]
        aligned_p  = (direction == "BUY" and price_bull) or (direction == "SELL" and not price_bull)
        checks["price_ema"] = (
            f"Price {'above' if price_bull else 'below'} EMA{tf_cfg['ema_slow']} — "
            f"{'✅' if aligned_p else '❌'}"
        )
        if aligned_p: score += 1

    # 3. RSI momentum
    rsi = calc_rsi(closes, tf_cfg["rsi_period"])
    if rsi is not None:
        if direction == "BUY":
            rsi_ok = 40 <= rsi <= 75
            rsi_label = "✅ Bullish zone" if rsi_ok else ("⚠️ Overbought" if rsi > 75 else "⚠️ Weak")
        else:
            rsi_ok = 25 <= rsi <= 60
            rsi_label = "✅ Bearish zone" if rsi_ok else ("⚠️ Oversold" if rsi < 25 else "⚠️ Weak")
        checks["rsi"] = f"RSI {rsi} — {rsi_label}"
        if rsi_ok:    score += 2
        elif (direction == "BUY" and rsi > 75) or (direction == "SELL" and rsi < 25):
            score -= 1   # extreme zone penalty

    # 4. ATR — active market
    atr     = calc_atr(candles_old, tf_cfg["atr_period"])
    avg_bod = sum(abs(float(c["close"]) - float(c["open"])) for c in candles_old[-20:]) / 20
    if atr:
        vol_ok = atr > avg_bod * 0.5
        checks["atr"] = f"ATR {round(atr / pu, 1)} pips — {'✅ Active' if vol_ok else '⚠️ Low volatility'}"
        if vol_ok: score += 1

    # 5. Candle momentum (last 3)
    last3      = candles_newest_first[:3]
    bull_count = sum(1 for c in last3 if float(c["close"]) > float(c["open"]))
    bear_count = 3 - bull_count
    if direction == "BUY":
        mom_ok = bull_count >= 2
        checks["momentum"] = f"{bull_count}/3 recent candles bullish — {'✅' if mom_ok else '⚠️'}"
    else:
        mom_ok = bear_count >= 2
        checks["momentum"] = f"{bear_count}/3 recent candles bearish — {'✅' if mom_ok else '⚠️'}"
    if mom_ok: score += 1

    # 6. Room to nearest S/R level
    support, resistance = find_sr(candles_newest_first)
    price = float(candles_newest_first[0]["close"])
    if direction == "BUY":
        room_pips = round((resistance - price) / pu, 1)
        room_ok   = room_pips > 15
        checks["sr_room"] = f"Room to resistance: {room_pips} pips — {'✅ Clear path' if room_ok else '⚠️ Near resistance'}"
    else:
        room_pips = round((price - support) / pu, 1)
        room_ok   = room_pips > 15
        checks["sr_room"] = f"Room to support: {room_pips} pips — {'✅ Clear path' if room_ok else '⚠️ Near support'}"
    if room_ok: score += 1

    return {
        "score": score, "max_score": 8, "checks": checks,
        "rsi": rsi, "atr": atr, "support": support, "resistance": resistance,
    }


# ── TRADE LEVELS ────────────────────────────────────────────────────
def calculate_trade_levels(stats, symbol, tf_key, direction, atr):
    """
    BUY:  Entry = close, SL below low, TPs above
    SELL: Entry = close, SL above high, TPs below
    """
    entry  = stats["close"]
    dec    = decimals(symbol)
    pu     = pip_unit(symbol)
    tp1_m, tp2_m, tp3_m = TP_MULTIPLIERS[tf_key]

    if direction == "BUY":
        sl   = stats["low"]
        risk = entry - sl
        if atr and atr > risk:
            sl   = round(entry - atr, dec)
            risk = atr
        return {
            "entry": round(entry, dec),
            "sl":    round(sl, dec),
            "tp1":   round(entry + tp1_m * risk, dec),
            "tp2":   round(entry + tp2_m * risk, dec),
            "tp3":   round(entry + tp3_m * risk, dec),
            "risk_pips": round(risk / pu, 1),
            "tp1_r": tp1_m, "tp2_r": tp2_m, "tp3_r": tp3_m,
        }
    else:  # SELL
        sl   = stats["high"]
        risk = sl - entry
        if atr and atr > risk:
            sl   = round(entry + atr, dec)
            risk = atr
        return {
            "entry": round(entry, dec),
            "sl":    round(sl, dec),
            "tp1":   round(entry - tp1_m * risk, dec),
            "tp2":   round(entry - tp2_m * risk, dec),
            "tp3":   round(entry - tp3_m * risk, dec),
            "risk_pips": round(risk / pu, 1),
            "tp1_r": tp1_m, "tp2_r": tp2_m, "tp3_r": tp3_m,
        }


# ── SIGNAL FORMATTER ────────────────────────────────────────────────
def format_signal(symbol, tf_key, direction, stats, levels, confluence):
    flag   = {"GBP/JPY": "🇬🇧🇯🇵", "EUR/JPY": "🇪🇺🇯🇵", "XAU/USD": "🥇💵", "USD/JPY": "🇺🇸🇯🇵"}.get(symbol, "📈")
    stars  = "⭐" * min(confluence["score"], 5)
    checks = "\n".join(f"  {v}" for v in confluence["checks"].values())

    if direction == "BUY":
        dir_line = "🟢 *BUY  (LONG)*"
    else:
        dir_line = "🔴 *SELL (SHORT)*"

    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕯 *NO-WICK SIGNAL*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{flag}  *{symbol}* — {TIMEFRAMES[tf_key]['label']}\n"
        f"🕐 `{stats['time']} UTC`\n"
        f"{dir_line}\n"
        f"📊 Confluence: {stars} `{confluence['score']}/{confluence['max_score']}`\n\n"
        f"*📍 Trade Levels*\n"
        f"  ▶️  Entry : `{levels['entry']}`\n"
        f"  🛑  SL    : `{levels['sl']}` ({levels['risk_pips']} pips)\n"
        f"  🎯  TP1   : `{levels['tp1']}`  _({levels['tp1_r']}R)_\n"
        f"  🎯  TP2   : `{levels['tp2']}`  _({levels['tp2_r']}R)_\n"
        f"  🎯  TP3   : `{levels['tp3']}`  _({levels['tp3_r']}R)_\n\n"
        f"*🔬 Analysis*\n{checks}\n\n"
        f"*📐 Candle*\n"
        f"  Body: `{stats['body_pips']} pips`\n"
        f"  Upper wick: `{stats['upper_wick']}` | Lower wick: `{stats['lower_wick']}`\n\n"
        f"⚠️ _Manage risk. Max 1-2% per trade._\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )


# ── SCANNER ─────────────────────────────────────────────────────────
async def scan_timeframe(bot: Bot, tf_key: str, notify_no_signal: bool = False):
    tf_cfg        = TIMEFRAMES[tf_key]
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

        log.info(f"[{symbol}/{tf_key}] ✅ {direction} candle — running deep analysis...")
        confluence = research_confluence(candles, symbol, tf_cfg, direction)
        score      = confluence["score"]
        log.info(f"[{symbol}/{tf_key}] {direction} score: {score}/{confluence['max_score']}")

        if score < MIN_CONFLUENCE:
            log.info(f"[{symbol}/{tf_key}] ⚠️ Score too low ({score}), skipping")
            continue

        levels = calculate_trade_levels(stats, symbol, tf_key, direction, confluence["atr"])
        msg    = format_signal(symbol, tf_key, direction, stats, levels, confluence)

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Taken",   callback_data=f"taken_{symbol}_{tf_key}_{direction}"),
            InlineKeyboardButton("❌ Skipped", callback_data=f"skip_{symbol}_{tf_key}_{direction}"),
        ]])

        if alerts_enabled:
            await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown", reply_markup=keyboard)
            log.info(f"[{symbol}/{tf_key}] 🚀 {direction} signal sent!")
        else:
            log.info(f"[{symbol}/{tf_key}] 🔕 {direction} signal found but alerts OFF")

        alerted[alert_key] = candle_time
        signals_found += 1
        await asyncio.sleep(1)

    if notify_no_signal and signals_found == 0:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=f"🔍 *{TIMEFRAMES[tf_key]['label']} scan complete* — no qualifying setups right now.",
            parse_mode="Markdown",
        )


async def scan_all(bot: Bot, notify_no_signal: bool = False):
    for tf_key in TIMEFRAMES:
        await scan_timeframe(bot, tf_key, notify_no_signal)
        await asyncio.sleep(2)


# ── COMMAND HANDLERS ────────────────────────────────────────────────
async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *No-Wick Signal Bot* is live!\n\n"
        "*Timeframes:*\n"
        "  📊 1H  — Swing (scans every 5 min)\n"
        "  📊 4H  — Position (scans every 15 min)\n"
        "  📊 Day — Daily (scans every 1 hr)\n\n"
        "*Signals:* 🟢 BUY & 🔴 SELL\n\n"
        "*Commands:*\n"
        "  /scan    — scan all timeframes now\n"
        "  /scan1h  — scan 1H only\n"
        "  /scan4h  — scan 4H only\n"
        "  /scanday — scan Daily only\n"
        "  /status  — last signals per pair/TF\n"
        "  /pairs   — active pairs\n"
        "  /alert   — toggle alerts ON/OFF\n\n"
        "🔔 Alerts: *ON* | Min confluence: 5/8",
        parse_mode="Markdown",
    )


async def cmd_scan(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔭 Running full multi-timeframe research scan...")
    await scan_all(context.application.bot, notify_no_signal=True)


async def cmd_scan_1h(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔭 Scanning 1H for BUY & SELL setups...")
    await scan_timeframe(context.application.bot, "1H", notify_no_signal=True)


async def cmd_scan_4h(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔭 Scanning 4H for BUY & SELL setups...")
    await scan_timeframe(context.application.bot, "4H", notify_no_signal=True)


async def cmd_scan_day(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔭 Scanning Daily for BUY & SELL setups...")
    await scan_timeframe(context.application.bot, "1day", notify_no_signal=True)


async def cmd_alert(update, context: ContextTypes.DEFAULT_TYPE):
    global alerts_enabled
    alerts_enabled = not alerts_enabled
    state = "🔔 ON" if alerts_enabled else "🔕 OFF"
    await update.message.reply_text(
        f"*Alerts: {state}*\n{'Signals will push to this chat.' if alerts_enabled else 'Signals logged only.'}",
        parse_mode="Markdown",
    )


async def cmd_status(update, context: ContextTypes.DEFAULT_TYPE):
    state = "🔔 ON" if alerts_enabled else "🔕 OFF"
    lines = [f"*Bot Status* | Alerts: {state}\n"]
    for tf_key, tf_cfg in TIMEFRAMES.items():
        lines.append(f"*— {tf_cfg['label']} —*")
        for sym in SYMBOLS:
            buy_last  = alerted.get(f"{sym}_{tf_key}_BUY",  "—")
            sell_last = alerted.get(f"{sym}_{tf_key}_SELL", "—")
            lines.append(f"  • {sym}  🟢 {buy_last}  |  🔴 {sell_last}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_pairs(update, context: ContextTypes.DEFAULT_TYPE):
    pairs = "\n".join(f"  • {s}" for s in SYMBOLS)
    tfs   = "\n".join(f"  • {k} — {v['label']}" for k, v in TIMEFRAMES.items())
    await update.message.reply_text(f"*Active Pairs*\n{pairs}\n\n*Timeframes*\n{tfs}", parse_mode="Markdown")


async def callback_handler(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data
    if data.startswith("taken_"):
        parts = data.replace("taken_", "").split("_")
        label = " ".join(parts)
        await query.edit_message_reply_markup(
            InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ {label} — Taken!", callback_data="done")]]))
    elif data.startswith("skip_"):
        parts = data.replace("skip_", "").split("_")
        label = " ".join(parts)
        await query.edit_message_reply_markup(
            InlineKeyboardMarkup([[InlineKeyboardButton(f"❌ {label} — Skipped", callback_data="done")]]))


# ── PERIODIC JOBS ───────────────────────────────────────────────────
async def job_1h(context: ContextTypes.DEFAULT_TYPE):
    log.info("⏰ Auto-scan: 1H")
    await scan_timeframe(context.bot, "1H")

async def job_4h(context: ContextTypes.DEFAULT_TYPE):
    log.info("⏰ Auto-scan: 4H")
    await scan_timeframe(context.bot, "4H")

async def job_day(context: ContextTypes.DEFAULT_TYPE):
    log.info("⏰ Auto-scan: Daily")
    await scan_timeframe(context.bot, "1day")


# ── MAIN ────────────────────────────────────────────────────────────
def main():
    log.info("🚀 Starting No-Wick Signal Bot (Multi-TF | BUY & SELL)...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("scan",    cmd_scan))
    app.add_handler(CommandHandler("scan1h",  cmd_scan_1h))
    app.add_handler(CommandHandler("scan4h",  cmd_scan_4h))
    app.add_handler(CommandHandler("scanday", cmd_scan_day))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("pairs",   cmd_pairs))
    app.add_handler(CommandHandler("alert",   cmd_alert))
    app.add_handler(CallbackQueryHandler(callback_handler))

    app.job_queue.run_repeating(job_1h,  interval=TIMEFRAMES["1H"]["scan_secs"],   first=10)
    app.job_queue.run_repeating(job_4h,  interval=TIMEFRAMES["4H"]["scan_secs"],   first=20)
    app.job_queue.run_repeating(job_day, interval=TIMEFRAMES["1day"]["scan_secs"], first=30)

    log.info("✅ Running — 1H/5min | 4H/15min | Daily/1hr | BUY & SELL active")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
