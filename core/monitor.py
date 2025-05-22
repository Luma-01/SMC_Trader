# core/monitor.py
import matplotlib
matplotlib.use("Agg")              # GUI 없는 서버에서도 렌더
import matplotlib.pyplot as plt
from mplfinance.original_flavor import candlestick_ohlc
import matplotlib.dates as mdates
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import gettempdir

from notify.discord import send_discord_file, send_discord_message
from core.data_feed import candles

# 내부 메모리용 간단 로그
TRADE_LOG: list[dict] = []

# ────────────────────── 진입 / 청산 이벤트 헬퍼 ──────────────────────
def on_entry(symbol: str, direction: str, entry: float, sl: float, tp: float):
    TRADE_LOG.append({
        "symbol": symbol,
        "direction": direction,
        "open": entry,
        "sl": sl,
        "tp": tp,
        "entry_time": datetime.utcnow(),
        "exit": None,
        "pnl": 0.0,
    })
    _capture_chart(TRADE_LOG[-1])   # ★ 진입 즉시 스냅샷

def on_exit(symbol: str, exit_price: float):
    for trade in reversed(TRADE_LOG):
        if trade["symbol"] == symbol and trade["exit"] is None:
            trade["exit"] = exit_price
            trade["exit_time"] = datetime.utcnow()
            mult = 1 if trade["direction"] == "long" else -1
            trade["pnl"] = (exit_price - trade["open"]) * mult
            _capture_chart(trade)      # PNG 생성 & 전송
            break

# ────────────────────────── 차트 캡쳐 & 전송 ─────────────────────────
def _capture_chart(trade: dict):
    sym = trade["symbol"]
    df = pd.DataFrame(candles.get(sym, {}).get("5m", []))
    if df.empty:
        import requests, time
        end = int(time.time() * 1000)
        start = end - 60 * 5 * 60 * 1000     # 60개(5분) = 300분
        url = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=5m&startTime={start}&endTime={end}"
        raw = requests.get(url, timeout=3).json()
        if raw and isinstance(raw, list):
            df = pd.DataFrame(raw, columns=['time','open','high','low','close','vol','c1','c2','c3','c4','c5','c6'])
            df['time'] = pd.to_datetime(df['time'], unit='ms')
            df = df[['time','open','high','low','close']].astype(float)
        if df.empty:
            return

    df = df.tail(60).copy()
    df["date"] = mdates.date2num(df["time"])
    ohlc = df[["date", "open", "high", "low", "close"]].values

    fig, ax = plt.subplots(figsize=(10, 4))
    candlestick_ohlc(ax, ohlc, width=0.0008, colorup="g", colordown="r", alpha=0.9)
    ax.axhline(trade["open"], color="blue", linestyle="--")
    ax.axhline(trade["tp"],   color="green", linestyle=":")
    ax.axhline(trade["sl"],   color="red",   linestyle=":")

    ax.set_title(f"{sym} Entry/Exit")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.grid(alpha=.3)

    path = Path(gettempdir()) / f"{sym}_{int(trade['entry_time'].timestamp())}.png"
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)

    send_discord_file(str(path), "aggregated")
    path.unlink(missing_ok=True)

# ───────────────────────────── 주간 리포트 ─────────────────────────────
_last_report_week = None

def maybe_send_weekly_report(now: datetime):
    global _last_report_week
    if _last_report_week == now.isocalendar().week:
        return
    # 일요일 23:59-00:05(UTC) 사이에만 실행
    if now.weekday() != 6 or now.minute > 5:
        return

    _last_report_week = now.isocalendar().week
    week_ago = now - timedelta(days=7)
    week_trades = [t for t in TRADE_LOG if t.get("exit_time") and t["exit_time"] >= week_ago]
    if not week_trades:
        return

    pnl = sum(t["pnl"] for t in week_trades)
    win = sum(1 for t in week_trades if t["pnl"] > 0)
    winrate = win / len(week_trades) * 100
    expectancy = pnl / len(week_trades)

    msg = (
        f"📊 **Weekly P&L**\n"
        f"• Trades : {len(week_trades)}\n"
        f"• WinRate: {winrate:.1f} %\n"
        f"• Expect : {expectancy:.2f} USDT\n"
        f"• P&L    : {pnl:.2f} USDT"
    )
    send_discord_message(msg, "aggregated")