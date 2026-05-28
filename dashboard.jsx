import { useState, useEffect, useCallback } from "react";

// ── Simulated candle data (replace with real API in production) ──
const SYMBOLS = ["GBP/JPY", "EUR/JPY", "XAU/USD", "USD/JPY"];

const MOCK_PRICES = {
  "GBP/JPY": 196.42,
  "EUR/JPY": 162.18,
  "XAU/USD": 2341.5,
  "USD/JPY": 149.85,
};

function generateCandles(basePrice, count = 20) {
  const candles = [];
  let price = basePrice;
  for (let i = count; i >= 0; i--) {
    const isBullish = Math.random() > 0.45;
    const bodySize = (Math.random() * 0.3 + 0.05) * (basePrice * 0.002);
    const open = price;
    const close = isBullish ? open + bodySize : open - bodySize;
    // Occasionally generate a no-wick candle
    const noWick = Math.random() > 0.75 && isBullish;
    const upper = noWick ? 0 : Math.random() * bodySize * 0.4;
    const lower = noWick ? 0 : Math.random() * bodySize * 0.4;
    const high = Math.max(open, close) + upper;
    const low = Math.min(open, close) - lower;
    const now = new Date(Date.now() - i * 15 * 60 * 1000);
    candles.push({
      open: +open.toFixed(3),
      high: +high.toFixed(3),
      low: +low.toFixed(3),
      close: +close.toFixed(3),
      time: now.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }),
      isNoWick: noWick,
      isBullish,
    });
    price = close + (Math.random() - 0.48) * bodySize * 0.5;
  }
  return candles;
}

function isNoWickBullish(c) {
  const body = c.close - c.open;
  if (body <= 0) return false;
  const upperWick = c.high - c.close;
  const lowerWick = c.open - c.low;
  const tol = 0.0002 * body;
  return upperWick <= Math.max(tol, 0.001) && lowerWick <= Math.max(tol, 0.001);
}

function CandleChart({ candles, symbol }) {
  const visible = candles.slice(-18);
  const highs = visible.map((c) => c.high);
  const lows = visible.map((c) => c.low);
  const maxP = Math.max(...highs);
  const minP = Math.min(...lows);
  const range = maxP - minP || 1;
  const W = 480, H = 160, PAD = 16;
  const chartW = W - PAD * 2;
  const chartH = H - PAD * 2;
  const slotW = chartW / visible.length;
  const bodyW = slotW * 0.55;

  function pxY(price) {
    return PAD + chartH - ((price - minP) / range) * chartH;
  }

  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ overflow: "visible" }}>
      {/* Grid lines */}
      {[0, 0.25, 0.5, 0.75, 1].map((t) => (
        <line
          key={t}
          x1={PAD} x2={W - PAD}
          y1={PAD + chartH * (1 - t)} y2={PAD + chartH * (1 - t)}
          stroke="#1e293b" strokeWidth="1"
        />
      ))}
      {/* Candles */}
      {visible.map((c, i) => {
        const x = PAD + i * slotW + slotW / 2;
        const isSignal = isNoWickBullish(c);
        const color = isSignal ? "#00ff9d" : c.isBullish ? "#22c55e" : "#ef4444";
        const dimColor = isSignal ? "#00ff9d44" : c.isBullish ? "#22c55e44" : "#ef444444";
        const openY = pxY(c.open);
        const closeY = pxY(c.close);
        const highY = pxY(c.high);
        const lowY = pxY(c.low);
        const bodyTop = Math.min(openY, closeY);
        const bodyH = Math.max(Math.abs(closeY - openY), 1.5);

        return (
          <g key={i}>
            {/* Wick */}
            <line x1={x} x2={x} y1={highY} y2={lowY} stroke={color} strokeWidth="1.5" />
            {/* Body */}
            <rect
              x={x - bodyW / 2}
              y={bodyTop}
              width={bodyW}
              height={bodyH}
              fill={isSignal ? "#00ff9d" : c.isBullish ? "#22c55e" : "#ef4444"}
              rx="1"
            />
            {/* Signal glow */}
            {isSignal && (
              <rect
                x={x - bodyW / 2 - 3}
                y={bodyTop - 3}
                width={bodyW + 6}
                height={bodyH + 6}
                fill="none"
                stroke="#00ff9d"
                strokeWidth="1"
                rx="2"
                opacity="0.5"
              />
            )}
            {/* Signal label */}
            {isSignal && (
              <text x={x} y={bodyTop - 8} textAnchor="middle" fill="#00ff9d" fontSize="8" fontFamily="monospace">
                ▲ LONG
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

function SignalCard({ signal, index }) {
  const [expanded, setExpanded] = useState(false);
  const risk = signal.entry - signal.sl;

  return (
    <div
      onClick={() => setExpanded(!expanded)}
      style={{
        background: "linear-gradient(135deg, #0f172a 0%, #1e293b 100%)",
        border: "1px solid #00ff9d44",
        borderLeft: "3px solid #00ff9d",
        borderRadius: "8px",
        padding: "14px 16px",
        cursor: "pointer",
        transition: "all 0.2s",
        animation: `slideIn 0.4s ease ${index * 0.1}s both`,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
          <span style={{
            background: "#00ff9d22",
            border: "1px solid #00ff9d55",
            borderRadius: "4px",
            padding: "3px 8px",
            color: "#00ff9d",
            fontFamily: "'JetBrains Mono', monospace",
            fontSize: "13px",
            fontWeight: "700",
            letterSpacing: "1px",
          }}>
            {signal.symbol}
          </span>
          <span style={{ color: "#64748b", fontFamily: "monospace", fontSize: "11px" }}>
            {signal.time} · M15
          </span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <span style={{
            background: "#00ff9d",
            color: "#0a0f1e",
            borderRadius: "4px",
            padding: "2px 8px",
            fontSize: "11px",
            fontWeight: "800",
            letterSpacing: "1px",
          }}>▲ LONG</span>
          <span style={{ color: "#475569", fontSize: "12px" }}>{expanded ? "▲" : "▼"}</span>
        </div>
      </div>

      {expanded && (
        <div style={{ marginTop: "14px", display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px" }}>
          {[
            { label: "ENTRY", value: signal.entry, color: "#e2e8f0" },
            { label: "STOP LOSS", value: signal.sl, color: "#ef4444" },
            { label: "TP1  (1R)", value: +(signal.entry + risk).toFixed(3), color: "#22c55e" },
            { label: "TP2  (2R)", value: +(signal.entry + 2 * risk).toFixed(3), color: "#4ade80" },
            { label: "TP3  (3R)", value: +(signal.entry + 3 * risk).toFixed(3), color: "#86efac" },
            { label: "RISK PIPS", value: +(risk / 0.01).toFixed(1) + " pips", color: "#f59e0b" },
          ].map((row) => (
            <div key={row.label} style={{
              background: "#0f172a",
              borderRadius: "6px",
              padding: "8px 10px",
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
            }}>
              <span style={{ color: "#475569", fontSize: "10px", fontFamily: "monospace", letterSpacing: "0.5px" }}>
                {row.label}
              </span>
              <span style={{ color: row.color, fontFamily: "monospace", fontSize: "13px", fontWeight: "700" }}>
                {row.value}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function App() {
  const [candles, setCandles] = useState({});
  const [signals, setSignals] = useState([]);
  const [scanning, setScanning] = useState(false);
  const [lastScan, setLastScan] = useState(null);
  const [activeTab, setActiveTab] = useState("GBP/JPY");
  const [tick, setTick] = useState(0);

  const runScan = useCallback(() => {
    setScanning(true);
    const newCandles = {};
    const newSignals = [];
    SYMBOLS.forEach((sym) => {
      const data = generateCandles(MOCK_PRICES[sym], 20);
      newCandles[sym] = data;
      const latest = data[data.length - 1];
      if (isNoWickBullish(latest)) {
        const entry = latest.close;
        const sl = latest.low;
        newSignals.push({
          id: `${sym}-${Date.now()}`,
          symbol: sym,
          entry: +entry.toFixed(3),
          sl: +sl.toFixed(3),
          time: latest.time,
        });
      }
    });
    setTimeout(() => {
      setCandles(newCandles);
      if (newSignals.length > 0) {
        setSignals((prev) => [...newSignals, ...prev].slice(0, 20));
      }
      setLastScan(new Date().toLocaleTimeString());
      setScanning(false);
    }, 800);
  }, []);

  useEffect(() => {
    runScan();
    const interval = setInterval(() => {
      setTick((t) => t + 1);
      runScan();
    }, 30000);
    return () => clearInterval(interval);
  }, [runScan]);

  // Countdown
  const [countdown, setCountdown] = useState(30);
  useEffect(() => {
    setCountdown(30);
    const t = setInterval(() => setCountdown((c) => Math.max(0, c - 1)), 1000);
    return () => clearInterval(t);
  }, [tick]);

  return (
    <div style={{
      minHeight: "100vh",
      background: "#060d1a",
      color: "#e2e8f0",
      fontFamily: "'JetBrains Mono', 'Courier New', monospace",
      padding: "0",
    }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700;800&family=Syne:wght@700;800&display=swap');
        @keyframes slideIn { from { opacity:0; transform:translateY(12px) } to { opacity:1; transform:none } }
        @keyframes pulse { 0%,100% { opacity:1 } 50% { opacity:0.4 } }
        @keyframes scanLine { 0% { transform:translateY(-100%) } 100% { transform:translateY(100vh) } }
        * { box-sizing: border-box; margin:0; padding:0 }
        ::-webkit-scrollbar { width:4px } ::-webkit-scrollbar-track { background:#0a0f1e }
        ::-webkit-scrollbar-thumb { background:#1e293b; border-radius:2px }
      `}</style>

      {/* Scan line animation */}
      {scanning && (
        <div style={{
          position: "fixed", top: 0, left: 0, right: 0, bottom: 0,
          pointerEvents: "none", zIndex: 999,
          background: "linear-gradient(transparent 40%, #00ff9d08 50%, transparent 60%)",
          animation: "scanLine 0.8s linear",
        }} />
      )}

      {/* Header */}
      <div style={{
        borderBottom: "1px solid #1e293b",
        background: "#0a0f1e",
        padding: "16px 24px",
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        position: "sticky", top: 0, zIndex: 100,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
          <div style={{
            width: "36px", height: "36px",
            background: "linear-gradient(135deg, #00ff9d, #00c97a)",
            borderRadius: "8px",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: "18px",
          }}>🕯</div>
          <div>
            <div style={{ fontFamily: "'Syne', sans-serif", fontWeight: "800", fontSize: "16px", letterSpacing: "1px", color: "#f8fafc" }}>
              NO-WICK SCANNER
            </div>
            <div style={{ color: "#475569", fontSize: "10px", letterSpacing: "2px" }}>
              EQUITY EDGE · M15 · LONG ONLY
            </div>
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "16px" }}>
          <div style={{ textAlign: "right" }}>
            <div style={{ color: "#475569", fontSize: "10px", letterSpacing: "1px" }}>NEXT SCAN</div>
            <div style={{ color: countdown <= 5 ? "#ef4444" : "#00ff9d", fontWeight: "700", fontSize: "14px" }}>
              {countdown}s
            </div>
          </div>
          <div style={{
            display: "flex", alignItems: "center", gap: "6px",
            background: scanning ? "#00ff9d22" : "#0f172a",
            border: `1px solid ${scanning ? "#00ff9d" : "#1e293b"}`,
            borderRadius: "20px",
            padding: "6px 14px",
            fontSize: "11px",
            color: scanning ? "#00ff9d" : "#475569",
            transition: "all 0.3s",
          }}>
            <span style={{ animation: scanning ? "pulse 0.8s infinite" : "none" }}>●</span>
            {scanning ? "SCANNING" : "LIVE"}
          </div>
        </div>
      </div>

      <div style={{ padding: "20px", maxWidth: "900px", margin: "0 auto" }}>
        {/* Stats row */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "12px", marginBottom: "20px" }}>
          {[
            { label: "PAIRS", value: "4", sub: "Active" },
            { label: "SIGNALS", value: signals.length, sub: "Today" },
            { label: "TIMEFRAME", value: "M15", sub: "Interval" },
            { label: "STRATEGY", value: "NW", sub: "No-Wick Long" },
          ].map((s) => (
            <div key={s.label} style={{
              background: "#0a0f1e",
              border: "1px solid #1e293b",
              borderRadius: "8px",
              padding: "14px",
              animation: "slideIn 0.5s ease both",
            }}>
              <div style={{ color: "#475569", fontSize: "9px", letterSpacing: "2px", marginBottom: "6px" }}>{s.label}</div>
              <div style={{ color: "#00ff9d", fontWeight: "800", fontSize: "22px", fontFamily: "'Syne', sans-serif" }}>{s.value}</div>
              <div style={{ color: "#334155", fontSize: "10px", marginTop: "2px" }}>{s.sub}</div>
            </div>
          ))}
        </div>

        {/* Chart section */}
        <div style={{
          background: "#0a0f1e",
          border: "1px solid #1e293b",
          borderRadius: "10px",
          padding: "16px",
          marginBottom: "20px",
        }}>
          <div style={{ display: "flex", gap: "8px", marginBottom: "16px", flexWrap: "wrap" }}>
            {SYMBOLS.map((sym) => (
              <button
                key={sym}
                onClick={() => setActiveTab(sym)}
                style={{
                  background: activeTab === sym ? "#00ff9d" : "#0f172a",
                  color: activeTab === sym ? "#060d1a" : "#475569",
                  border: `1px solid ${activeTab === sym ? "#00ff9d" : "#1e293b"}`,
                  borderRadius: "6px",
                  padding: "5px 12px",
                  fontSize: "11px",
                  fontFamily: "'JetBrains Mono', monospace",
                  fontWeight: "700",
                  cursor: "pointer",
                  letterSpacing: "0.5px",
                  transition: "all 0.2s",
                }}
              >{sym}</button>
            ))}
          </div>
          <div style={{ color: "#334155", fontSize: "10px", letterSpacing: "1px", marginBottom: "8px" }}>
            {activeTab} · M15 · <span style={{ color: "#00ff9d55" }}>GREEN BORDER = NO-WICK SIGNAL</span>
          </div>
          {candles[activeTab] && <CandleChart candles={candles[activeTab]} symbol={activeTab} />}
          <div style={{ display: "flex", gap: "16px", marginTop: "10px" }}>
            {[
              { color: "#00ff9d", label: "No-Wick (Signal)" },
              { color: "#22c55e", label: "Bullish" },
              { color: "#ef4444", label: "Bearish" },
            ].map((l) => (
              <div key={l.label} style={{ display: "flex", alignItems: "center", gap: "5px" }}>
                <div style={{ width: "10px", height: "10px", background: l.color, borderRadius: "2px" }} />
                <span style={{ color: "#334155", fontSize: "10px" }}>{l.label}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Signals */}
        <div style={{ marginBottom: "16px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ fontFamily: "'Syne', sans-serif", fontWeight: "800", fontSize: "14px", letterSpacing: "1px", color: "#94a3b8" }}>
            SIGNALS LOG
          </div>
          {lastScan && (
            <span style={{ color: "#334155", fontSize: "10px" }}>
              Last scan: {lastScan}
            </span>
          )}
        </div>

        {signals.length === 0 ? (
          <div style={{
            background: "#0a0f1e",
            border: "1px dashed #1e293b",
            borderRadius: "10px",
            padding: "40px",
            textAlign: "center",
            color: "#334155",
          }}>
            <div style={{ fontSize: "28px", marginBottom: "10px" }}>🕯</div>
            <div style={{ fontSize: "13px" }}>No no-wick signals detected yet.</div>
            <div style={{ fontSize: "11px", marginTop: "4px", color: "#1e293b" }}>Watching M15 candles across all pairs…</div>
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
            {signals.map((sig, i) => (
              <SignalCard key={sig.id} signal={sig} index={i} />
            ))}
          </div>
        )}

        {/* Strategy guide */}
        <div style={{
          marginTop: "24px",
          background: "#0a0f1e",
          border: "1px solid #1e293b",
          borderRadius: "10px",
          padding: "16px",
        }}>
          <div style={{ color: "#475569", fontSize: "10px", letterSpacing: "2px", marginBottom: "12px" }}>
            📖 NO-WICK STRATEGY RULES
          </div>
          {[
            ["🕯", "Candle", "Bullish body, zero (or near-zero) upper & lower wicks — full Marubozu"],
            ["📍", "Entry", "Market order at the close of the no-wick candle"],
            ["🛑", "Stop Loss", "Just below the candle low (no wick = low IS the open bottom)"],
            ["🎯", "Targets", "TP1 = 1R · TP2 = 2R · TP3 = 3R (partial close at each)"],
            ["📊", "Trend", "Only trade in direction of higher lows (uptrend confirmation)"],
            ["⚠️", "Risk", "Max 1% of $5,000 account = $50 per trade for challenge phase"],
          ].map(([icon, title, desc]) => (
            <div key={title} style={{
              display: "flex", gap: "12px", padding: "10px 0",
              borderBottom: "1px solid #0f172a",
            }}>
              <span style={{ fontSize: "14px", minWidth: "20px" }}>{icon}</span>
              <div>
                <div style={{ color: "#94a3b8", fontSize: "11px", fontWeight: "700", letterSpacing: "0.5px" }}>{title}</div>
                <div style={{ color: "#475569", fontSize: "11px", marginTop: "2px", lineHeight: "1.5" }}>{desc}</div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
