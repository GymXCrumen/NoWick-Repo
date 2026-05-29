"""
╔══════════════════════════════════════════════════════════════╗
║       NO-WICK CANDLE STRATEGY BOT — Equity Edge Edition     ║
║       Markets: GBP/JPY, EUR/JPY, XAU/USD, USD/JPY           ║
║       Timeframe: M15  |  Signal: LONG only                   ║
╚══════════════════════════════════════════════════════════════╝

SETUP:
  pip install python-telegram-bot requests python-dotenv

USAGE:
  1. Copy .env.example → .env and fill in your keys
  2. python bot.py
"""

import os
import json
import logging
import asyncio
from datetime import datetime, timezone
from dotenv import load_dotenv
import requests
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

load_dotenv()

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "8711673831:AAHLoqgcZ3Fs9N-uxo8K6jqsFJmn6DKyMI4")
CHAT_ID          = os.getenv("TELEGRAM_CHAT_ID", "7027461661")
TWELVEDATA_KEY   = os.getenv("TWELVEDATA_API_KEY", "915d19cf66c24bfab3df227c297a4b9f")

SYMBOLS = ["GBP/JPY", "EUR/JPY", "XAU/USD", "USD/JPY"]
INTERVAL         = "15min"
SCAN_EVERY_SECS  = 60          # scan every 60 seconds
WICK_TOLERANCE   = 0.15        # max wick as fraction of candle body (15%)
MIN_BODY_PIPS    = 3           # minimum body size in pips to filter noise
LOOKBACK         = 2           # confirm trend: last N candles must be bullish

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# Track already-alerted signals to avoid spam
alerted: dict[str, str] = {}   # symbol → last alerted candle time


# ──────────────────────────────────────────────
# TWELVEDATA FETCHER
# ──────────────────────────────────────────────
def fetch_candles(symbol: str, outputsize: int = 10) -> list[dict] | None:
    """Fetch M15 OHLC candles from Twelve Data."""
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol":     symbol,
        "interval":   INTERVAL,
        "outputsize": outputsize,
        "apikey":     TWELVEDATA_KEY,
        "format":     "JSON",
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("status") == "error":
            log.warning(f"[{symbol}] API error: {data.get('message')}")
            return None
        return data.get("values", [])   # newest first
    except Exception as e:
        log.error(f"[{symbol}] fetch error: {e}")
        return None


# ──────────────────────────────────────────────
# NO-WICK CANDLE DETECTOR
# ──────────────────────────────────────────────
def is_no_wick_bullish(candle: dict) -> tuple[bool, dict]:
    """
    A 'no-wick' bullish candle (Marubozu Long) satisfies:
      • close > open  (bullish body)
      • upper wick ≤ WICK_TOLERANCE × body
      • lower wick ≤ WICK_TOLERANCE × body
      • body size ≥ MIN_BODY_PIPS (in price units, 1 pip ≈ 0.01 for JPY pairs)
    Returns (signal_bool, stats_dict)
    """
    o = float(candle["open"])
    h = float(candle["high"])
    l = float(candle["low"])
    c = float(candle["close"])

    body        = c - o
    upper_wick  = h - c
    lower_wick  = o - l
    candle_range = h - l

    stats = {
        "open": o, "high": h, "low": l, "close": c,
        "body": round(body, 5),
        "upper_wick": round(upper_wick, 5),
        "lower_wick": round(lower_wick, 5),
        "range": round(candle_range, 5),
        "time": candle["datetime"],
    }

    if body <= 0:
        return False, stats           # bearish or doji

    pip_unit = 0.01 if "JPY" in candle.get("_symbol", "") else 0.0001
    body_pips = body / pip_unit

    if body_pips < MIN_BODY_PIPS:
        return False, stats           # too small, ignore noise

    tolerance = WICK_TOLERANCE * body
    if upper_wick > tolerance or lower_wick > tolerance:
        return False, stats           # has wicks

    return True, stats


def confirm_uptrend(candles: list[dict], n: int = LOOKBACK) -> bool:
    """Last N closed candles should show higher lows (basic trend filter)."""
    if len(candles) < n + 1:
        return True   # not enough data, allow signal
    lows = [float(c["low"]) for c in candles[1: n + 1]]   # skip newest (index 0)
    return all(lows[i] >= lows[i + 1] for i in range(len(lows) - 1))


def calculate_trade_levels(stats: dict, symbol: str) -> dict:
    """
    For a long entry:
      Entry  = close of no-wick candle
      SL     = low of the candle (no wick = low IS the open for bullish)
      TP1    = entry + 1× risk  (1R)
      TP2    = entry + 2× risk  (2R)
      TP3    = entry + 3× risk  (3R)
    """
    entry = stats["close"]
    sl    = stats["low"]
    risk  = entry - sl
    tp1   = entry + risk
    tp2   = entry + 2 * risk
    tp3   = entry + 3 * risk

    decimals = 3 if "JPY" in symbol or "XAU" in symbol else 5

    return {
        "entry": round(entry, decimals),
        "sl":    round(sl,    decimals),
        "tp1":   round(tp1,   decimals),
        "tp2":   round(tp2,   decimals),
        "tp3":   round(tp3,   decimals),
        "risk_pips": round(risk / (0.01 if "JPY" in symbol else 0.0001), 1),
    }


# ──────────────────────────────────────────────
# SIGNAL MESSAGE FORMATTER
# ──────────────────────────────────────────────
def format_signal(symbol: str, stats: dict, levels: dict) -> str:
    flag = {"GBP/JPY": "🇬🇧🇯🇵", "EUR/JPY": "🇪🇺🇯🇵",
            "XAU/USD": "🥇💵",   "USD/JPY": "🇺🇸🇯🇵"}.get(symbol, "📈")

    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕯 *NO-WICK LONG SIGNAL*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{flag}  *{symbol}* — M15\n"
        f"🕐 `{stats['time']} UTC`\n\n"
        f"*📍 Trade Levels*\n"
        f"  ▶️  Entry : `{levels['entry']}`\n"
        f"  🛑  SL    : `{levels['sl']}` ({levels['risk_pips']} pips)\n"
        f"  🎯  TP1   : `{levels['tp1']}`  _(1R)_\n"
        f"  🎯  TP2   : `{levels['tp2']}`  _(2R)_\n"
        f"  🎯  TP3   : `{levels['tp3']}`  _(3R)_\n\n"
        f"*📊 Candle Stats*\n"
        f"  Body  : `{stats['body']}`\n"
        f"  Upper wick: `{stats['upper_wick']}`\n"
        f"  Lower wick: `{stats['lower_wick']}`\n\n"
        f"⚠️ _Manage risk. Max 1-2% per trade._\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━"
    )


# ──────────────────────────────────────────────
# CORE SCANNER
# ──────────────────────────────────────────────
async def scan_all(bot: Bot):
    """Scan all symbols and send signals for new no-wick setups."""
    for symbol in SYMBOLS:
        candles = fetch_candles(symbol, outputsize=10)
        if not candles:
            continue

        # Tag candles with symbol for pip calculation
        for c in candles:
            c["_symbol"] = symbol

        latest = candles[0]   # most recent closed candle
        candle_time = latest["datetime"]

        # Skip if we already alerted this candle
        if alerted.get(symbol) == candle_time:
            continue

        is_signal, stats = is_no_wick_bullish(latest)
        if not is_signal:
            log.info(f"[{symbol}] No signal at {candle_time}")
            continue

        if not confirm_uptrend(candles):
            log.info(f"[{symbol}] Signal found but trend not confirmed, skipping")
            continue

        levels = calculate_trade_levels(stats, symbol)
        msg    = format_signal(symbol, stats, levels)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Taken", callback_data=f"taken_{symbol}"),
             InlineKeyboardButton("❌ Skipped", callback_data=f"skip_{symbol}")]
        ])

        await bot.send_message(
            chat_id=CHAT_ID,
            text=msg,
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        alerted[symbol] = candle_time
        log.info(f"[{symbol}] 🚀 Signal sent for candle at {candle_time}")

        await asyncio.sleep(1)   # small delay between messages


# ──────────────────────────────────────────────
# TELEGRAM COMMAND HANDLERS
# ──────────────────────────────────────────────
async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *No-Wick Signal Bot* is live!\n\n"
        "Commands:\n"
        "  /scan  — manual scan now\n"
        "  /status — bot status\n"
        "  /pairs  — active pairs\n\n"
        "Auto-scanning every 60s on M15.",
        parse_mode="Markdown"
    )


async def cmd_scan(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Scanning markets now...")
    await scan_all(context.application.bot)
    await update.message.reply_text("✅ Scan complete.")


async def cmd_status(update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["*Bot Status*\n"]
    for sym in SYMBOLS:
        last = alerted.get(sym, "No signal yet")
        lines.append(f"• {sym}: `{last}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_pairs(update, context: ContextTypes.DEFAULT_TYPE):
    pairs_text = "\n".join(f"  • {s}" for s in SYMBOLS)
    await update.message.reply_text(
        f"*Active Pairs (M15)*\n{pairs_text}", parse_mode="Markdown"
    )


async def callback_handler(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("taken_"):
        sym = data.replace("taken_", "")
        await query.edit_message_reply_markup(
            InlineKeyboardMarkup([[
                InlineKeyboardButton(f"✅ {sym} — Trade Taken!", callback_data="done")
            ]])
        )
    elif data.startswith("skip_"):
        sym = data.replace("skip_", "")
        await query.edit_message_reply_markup(
            InlineKeyboardMarkup([[
                InlineKeyboardButton(f"❌ {sym} — Skipped", callback_data="done")
            ]])
        )


# ──────────────────────────────────────────────
# PERIODIC JOB
# ──────────────────────────────────────────────
async def periodic_scan(context: ContextTypes.DEFAULT_TYPE):
    log.info("⏰ Auto-scan triggered")
    await scan_all(context.bot)


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    log.info("🚀 Starting No-Wick Signal Bot...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("scan",   cmd_scan))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pairs",  cmd_pairs))
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Run auto-scan every SCAN_EVERY_SECS seconds
    app.job_queue.run_repeating(periodic_scan, interval=SCAN_EVERY_SECS, first=5)

    log.info(f"✅ Bot running. Scanning {SYMBOLS} every {SCAN_EVERY_SECS}s on M15.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
