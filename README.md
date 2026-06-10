# 🕯 No-Wick Signal Bot — Setup Guide
**| GBP/JPY · EUR/JPY · XAU/USD · USD/JPY**

---

## What You're Getting
| File | Purpose |
|------|---------|
| `bot.py` | Telegram bot — scans M15 candles every 60s, sends LONG signals |
| `.env.example` | Template for your API keys |
| `requirements.txt` | Python dependencies |
| `dashboard.jsx` | Visual React dashboard (preview in browser) |

---

## Step 1 — Create Your Telegram Bot
1. Open Telegram → search **@BotFather**
2. Send `/newbot` → give it a name (e.g. "Equity Edge Scanner")
3. Copy the **token** it gives you → paste into `.env` as `TELEGRAM_TOKEN`
4. To get your **Chat ID**: message **@userinfobot** on Telegram → copy the ID

---

## Step 2 — Get Twelve Data API Key (Free)
1. Go to [https://twelvedata.com](https://twelvedata.com) → sign up free
2. Dashboard → API Keys → copy your key
3. Free tier: **800 requests/day** (plenty for 4 pairs every 60s)

---

## Step 3 — Install & Run
```bash
# Clone / move bot files to a folder
cd nowick_bot

# Install dependencies
pip install -r requirements.txt

# Copy and fill in your keys
cp .env.example .env
nano .env   # or open in any text editor

# Run the bot
python bot.py
```

---

## Step 4 — Telegram Commands
| Command | Action |
|---------|--------|
| `/start` | Welcome message + command list |
| `/scan`  | Force immediate scan of all pairs |
| `/status` | Show last signal time per pair |
| `/pairs`  | List active pairs |

---

## Signal Message Example
```
━━━━━━━━━━━━━━━━━━━━━━━
🕯 NO-WICK LONG SIGNAL
━━━━━━━━━━━━━━━━━━━━━━━
🇬🇧🇯🇵  GBP/JPY — M15
🕐 2024-11-14 09:15 UTC

📍 Trade Levels
  ▶️  Entry : 196.420
  🛑  SL    : 196.280  (14 pips)
  🎯  TP1   : 196.560  (1R)
  🎯  TP2   : 196.700  (2R)
  🎯  TP3   : 196.840  (3R)

📊 Candle Stats
  Body       : 0.14000
  Upper wick : 0.00100
  Lower wick : 0.00000
━━━━━━━━━━━━━━━━━━━━━━━
```

---

## No-Wick Strategy Rules (Quick Reference)

### What is a No-Wick Candle?
A **Marubozu** — a candle where price moves entirely in one direction with no rejection:
- ✅ Bullish body (close > open)
- ✅ Upper wick ≈ 0  (buyers are in full control, no rejection at top)
- ✅ Lower wick ≈ 0  (no pullback at all during the candle)

### Trade Management for $5K Equity Edge Challenge
| Rule | Value |
|------|-------|
| Max risk per trade | 1% = **$50** |
| Daily drawdown limit | Stay under Equity Edge's 5% daily DD |
| Partial close at TP1 | Close 50% → move SL to breakeven |
| Let rest run to TP2/TP3 | Trail SL as price moves up |

### Best Trading Sessions for These Pairs
| Pair | Best Session |
|------|-------------|
| GBP/JPY | London open (08:00–12:00 GMT) |
| EUR/JPY | London + NY overlap (13:00–16:00 GMT) |
| XAU/USD | NY open (13:00–17:00 GMT) |
| USD/JPY | Asia open (00:00–04:00 GMT) |

---

## Run 24/7 on a Server (Optional)
```bash
# Using screen (Linux/Mac)
screen -S nowick
python bot.py
# Ctrl+A then D to detach

# OR using PM2
npm install -g pm2
pm2 start bot.py --interpreter python3 --name nowick
pm2 save
```

---

## Troubleshooting
| Issue | Fix |
|-------|-----|
| "Unauthorized" error | Check `TELEGRAM_TOKEN` in `.env` |
| No candle data returned | Check `TWELVEDATA_API_KEY` + free tier limit |
| Bot not scanning | Make sure `python-telegram-bot[job-queue]` is installed |
| Getting old signals | The bot deduplicates — each candle only alerts once |

---

*Good luck with your Equity Edge challenge! Stick to the rules, protect your drawdown.* 🚀
