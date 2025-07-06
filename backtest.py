#backtest.py

"""
CSV → MockExchange → 전략 Tick 을 연결하는 간단 러너

사용 예:
    export EXCHANGE_MODE=mock
    python backtest.py --csv data/BTCUSDT_1m_2024-01-01~2024-12-31.csv
"""

# ──────────────────────────────────────────────
#  백테스트 전용 환경 변수는 **가장 먼저**!
# ──────────────────────────────────────────────
import os
os.environ["EXCHANGE_MODE"] = "mock"     # ✅ Binance 호출 차단
# (선택) 디스코드 로그까지 끄고 싶다면 함께:
# os.environ["NO_DISCORD"] = "1"

from datetime import datetime, timezone
import os, argparse, pandas as pd
from decimal import Decimal
from exchange.mock_exchange import mark_price, set_last_price
from main import backtest_tick

# MockExchange 내부 상태 공유용
import types, sys
mock_state = types.ModuleType("exchange.mock_state")
mock_state.last_price = Decimal("0")
sys.modules["exchange.mock_state"] = mock_state

from exchange.mock_exchange import place_order, mark_price
from config.settings import TRADE_RISK_PCT, DEFAULT_LEVERAGE

# 🎯 당장 검증만을 위한 더미 전략 – HTF/LTF 구조는 추후 연결
def dummy_strategy(row):
    """
    SMA-20 크로스 기반 데모.
    LONG  →  종가 > SMA20
    SHORT →  종가 < SMA20
    """
    close = Decimal(str(row["close"]))
    sma   = Decimal(str(row["sma20"]))
    symbol = "BTCUSDT"

    if close > sma:
        place_order(symbol, side="BUY", order_type="MARKET",
                    quantity=Decimal("0.01"))
    elif close < sma:
        place_order(symbol, side="SELL", order_type="MARKET",
                    quantity=Decimal("0.01"))


def main(csv_path: str):
    # ────────────────────────────────────────────────
    #  ⬇️ ISO-8601 → epoch ms 변환
    #     • CSV 열 이름이 'timestamp'·'date'·'datetime' 중 무엇이든 자동 인식
    # ────────────────────────────────────────────────
    date_cols = {"timestamp", "date", "datetime", "time"}
    df = pd.read_csv(csv_path)

    ts_col = next((c for c in df.columns if c.lower() in date_cols), None)
    if ts_col is None:
        raise ValueError("❌ CSV에 timestamp/date 열이 없습니다.")

    df[ts_col] = (
        pd.to_datetime(df[ts_col], utc=True)
          .astype("int64")          # ns → int64 (미래 호환)
          // 1_000_000              # ns → ms
    ).astype(int)

    # 내부에서 한결같이 'timestamp' 키를 쓰도록 보정
    if ts_col != "timestamp":
        df.rename(columns={ts_col: "timestamp"}, inplace=True)
    for _, row in df.iterrows():
        # 캔들 dict 생성 (기존 구조와 동일)
        ts_ms = int(row["timestamp"])
        ts_dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        candle = {
            "timestamp": ts_ms,
            "time":      ts_dt,          #  ✅ datetime 으로 교체
            "open" : float(row["open"]),
            "high" : float(row["high"]),
            "low"  : float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        }

        # ① 최신가 캐시 + TP/SL 충돌 확인
        last_p = Decimal(str(candle["close"]))
        set_last_price(last_p)
        mark_price("BTCUSDT", last_p)

        # ② SMC 전략 한 틱 실행 (TF 자동 인식)
        backtest_tick("BTCUSDT", candle)

    from exchange.mock_exchange import _balance as balance  # pylint: disable=protected-access
    print(f"\n🧾  백테스트 종료 – 최종 잔고: {balance:.2f} USDT\n")

def preload_history(csv_path: str):
    print(f"[LOAD] 과거 히스토리 적재: {csv_path}")
    df = pd.read_csv(csv_path)
    df["timestamp"] = (
        pd.to_datetime(df.iloc[:, 0], utc=True)
          .astype("int64") // 1_000_000
    ).astype(int)

    for _, row in df.iterrows():
        ts_ms = int(row["timestamp"])
        ts_dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        candle = {
            "timestamp": ts_ms,
            "time":      ts_dt,
            "open":  float(row["open"]),
            "high":  float(row["high"]),
            "low":   float(row["low"]),
            "close": float(row["close"]),
            "volume":float(row["volume"]),
        }
        # ① 가격 캐시만 업데이트 (TP/SL 체크 X)
        set_last_price(Decimal(str(candle["close"])))
        # ② 집계 전용 – handle_pair() 호출 없이 **backtest_tick()** 으로 LTF·HTF deque만 채우기
        backtest_tick("BTCUSDT", candle, exec_strategy=False)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, help="백테스트 대상 CSV 경로")
    parser.add_argument("--hist", required=False, help="선로딩용 히스토리 CSV 경로")
    args = parser.parse_args()
    os.environ["EXCHANGE_MODE"] = "mock"   # 강제 mock
    # ① 히스토리 선로딩
    if args.hist:
        preload_history(args.hist)     # 아래 새 함수
    # ② 본 백테스트 실행
    main(args.csv)
