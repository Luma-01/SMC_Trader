# main.py

import os
import requests
import sys
import asyncio
import builtins                     
from collections import deque       
from decimal import Decimal                
from datetime import datetime, timezone
from dotenv import load_dotenv
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import pandas as pd
from core.structure import detect_structure
# Decimal 변환용 유틸
from decimal import Decimal
from notify.discord import send_discord_debug, send_discord_message
from config.settings import (
    SYMBOLS,
    SYMBOLS_BINANCE,
    SYMBOLS_GATE,
    RR,
    SL_BUFFER,
    DEFAULT_LEVERAGE,
    ENABLE_GATE,
    ENABLE_BINANCE,
)
from core.data_feed import (
    candles, initialize_historical, start_data_feed,
    to_binance, is_gate_sym,
)
from core.iof import is_iof_entry
from core.position import PositionManager
from core.monitor import maybe_send_weekly_report
from core.ob import detect_ob
# ────────────── 모드별 import ──────────────
from exchange.router import get_open_position     # (Gate·Binance 공용)

if ENABLE_BINANCE:
    from exchange.binance_api import (
        place_order_with_tp_sl as binance_order_with_tp_sl,
        get_total_balance,
        get_tick_size, calculate_quantity,
        set_leverage, get_max_leverage,
        get_available_balance,
        get_open_position as binance_pos,
    )
# Gate.io 연동은 ENABLE_GATE 가 True 일 때만 임포트
if ENABLE_GATE:
    from exchange.gate_sdk import (
        place_order_with_tp_sl as gate_order_with_tp_sl,
        get_open_position as gate_pos,
        set_leverage as gate_set_leverage,
        get_available_balance as gate_get_balance,
        get_tick_size as get_tick_size_gate,
        calculate_quantity_gate as calculate_quantity_gate,
        to_gate_symbol as to_gate,        # ← 실제 함수명이 다르면 맞춰 주세요
    )

##########################################################################
#  콘솔 도배 방지용 dedup-print
#  ■ '[OB][' 또는 '[BB][' 로 시작하고 'NEW' 가 없는 “요약” 라인은
#    이미 한 번 찍혔으면 다시 출력하지 않는다
#  ■ 나머지 메시지(NEW, 구조, 진입/청산, 에러 등)는 그대로 출력
##########################################################################
_seen_log = deque(maxlen=5000)          # 최근 5 000줄만 기억

# 중복 메시지 필터 [ON("0", "false"), OFF("1", "true")]
DEDUP_OFF = os.getenv("NO_DEDUP", "").lower() in ("0", "false")

def _dedup_print(*args, **kwargs):
    if not args:                        # 빈 print()
        builtins.__orig_print__(*args, **kwargs)
        return

    first = str(args[0])

    # ───────── OB/BB 요약(NEW 없는) ─────────
    if (first.startswith("[OB][") or first.startswith("[BB][")) and "NEW" not in first:
        if first in _seen_log:
            return
        _seen_log.append(first)

    # ───────── 반복되는 BIAS / IOF 라인 ─────────
    elif first.startswith("[WARN] price-update failed"):
        tag = first.split(":")[0] + first.rsplit("→",1)[0]   # 심볼 기준
        if tag in _seen_log:
            return
        _seen_log.append(tag)

    builtins.__orig_print__(*args, **kwargs)

# 한 번만 패치
if not DEDUP_OFF and not hasattr(builtins, "__orig_print__"):
    builtins.__orig_print__ = builtins.print
    builtins.print          = _dedup_print

# ────────────────────────────────────────────────
# 최소 SL 간격(틱) – 진입 직후 SL 터지는 현상 방지
# (필요하면 config.settings 로 이동하세요)
# ────────────────────────────────────────────────
MIN_SL_TICKS = 5

load_dotenv()
pm = PositionManager()
import core.data_feed as df
df.set_pm(pm)          # ← 순환 import 없이 pm 전달


# ───────────────────────────── 헬퍼 ─────────────────────────────
async def handle_pair(symbol: str, meta: dict, htf_tf: str, ltf_tf: str):
    """
    symbol : Binance → BTCUSDT / Gate → BTC_USDT
    meta   : 최소 {"leverage": …}.  비어 있으면 DEFAULT_LEVERAGE 사용
    """
    leverage = meta.get("leverage", DEFAULT_LEVERAGE)

    # 표준 키/거래소 구분
    is_gate  = is_gate_sym(symbol)
    base_sym = to_binance(symbol) if not is_gate else symbol   # Binance REST용

    # ⚠️ base_sym / is_gate 를 가장 먼저 계산해 둔다
    is_gate  = "_USDT" in symbol
    base_sym = symbol.replace("_", "") if is_gate else symbol

    # ───────── 중복 진입 방지 (내부 + 실시간) ─────────
    # ① 내부 포지션 이미 보유
    if pm.has_position(symbol):
        try:
            df_ltf = candles.get(symbol, {}).get(ltf_tf)
            if df_ltf and len(df_ltf):
                last_price = float(df_ltf[-1]["close"]      # deque 는 리스트처럼
                                if isinstance(df_ltf[-1], dict)
                                else df_ltf["close"].iloc[-1])
            else:
                # 🆕 REST fallback – premiumIndex(= mark price) 사용
                r = requests.get(
                    f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={base_sym}",
                    timeout=3
                ).json()
                
                last_price = float(r["markPrice"])
            pm.update_price(symbol, last_price,
                            ltf_df=pd.DataFrame(candles.get(symbol, {}).get(ltf_tf, [])))
        except Exception as e:
            print(f"[WARN] price-update failed: {symbol} → {e}")
        return
    
    # ② 쿨-다운 중이면 스킵
    if pm.in_cooldown(symbol):
        return  
      
    # 실시간 확인 (논블로킹, 1 회 시도)
    live_pos = get_open_position(symbol, 0, 0)
    if live_pos and abs(live_pos.get("entry", 0)) > 0:
        print(f"[SKIP] 실시간 포지션 확인됨 → {symbol}")
        return
    
    try:
         # ▸ candle dict 는 항상 Binance 포맷(BTCUSDT) 키 사용
        df_htf = candles.get(symbol, {}).get(htf_tf)
        df_ltf = candles.get(symbol, {}).get(ltf_tf)
        if df_htf is None or df_ltf is None or len(df_htf) < 30 or len(df_ltf) < 30:
            return

        # ▸ 심볼·타임프레임 메타데이터 주입
        htf = pd.DataFrame(df_htf)
        htf.attrs["symbol"] = base_sym.upper()
        htf.attrs["tf"]     = htf_tf

        ltf = pd.DataFrame(df_ltf)
        ltf.attrs["symbol"] = base_sym.upper()
        ltf.attrs["tf"]     = ltf_tf

        htf_struct = detect_structure(htf)
        if (
            htf_struct is None
            or "structure" not in htf_struct.columns
            or htf_struct["structure"].dropna().empty
        ):
            return

        # Gate · Binance 모두 Decimal 로 통일 (precision 오류 방지!)
        tick_size = (
            Decimal(str(get_tick_size_gate(symbol))) if is_gate
            else Decimal(str(get_tick_size(base_sym)))
        )

        # ⬇️ htf 전체 DataFrame을 그대로 넘겨야 attrs 를 활용할 수 있음
        signal, direction, trg_zone = is_iof_entry(htf, ltf, tick_size)
        if not signal or direction is None:
            return

        entry = float(ltf["close"].iloc[-1])
        # Zone 기반 SL/TP 계산 (OB 사용)
        zone = None
        # ────────────────  ❗FVG 제외 ────────────────
        # detect_ob() 가 리턴하는 dict 예시:
        #   {"type": "long", "pattern": "ob", "high": …, "low": …}
        #   {"type": "short","pattern": "fvg", …}
        #
        # pattern(=구조 종류)이 'fvg' 이면 건너뛰고,
        # 그렇지 않은 블록(OB, BB 등)만 진입 근거로 사용한다.
        for ob in reversed(detect_ob(ltf)):
            if ob.get("pattern") == "fvg":          # ➜ 노이즈 많은 FVG 스킵
                continue

            if ob["type"].lower() == direction:     # 방향 일치하는 마지막 블록
                zone = ob
                break
        entry_dec = Decimal(str(entry))

        # ── 공통 버퍼 계산 ──────────────────────────────────────────
        # (1) **기본 버퍼** : 환경 상수 × tick
        base_buf = tick_size * Decimal(str(SL_BUFFER))

        # (2) **동적 버퍼** : HTF 트리거-존(또는 최근 OB) 폭의 10 %
        zone_range = None
        if trg_zone is not None:
            hi = Decimal(str(trg_zone["high"]))
            lo = Decimal(str(trg_zone["low"]))
            zone_range = abs(hi - lo)
        elif zone is not None:
            hi = Decimal(str(zone["high"]))
            lo = Decimal(str(zone["low"]))
            zone_range = abs(hi - lo)

        if zone_range is not None:
            dyn_buf = (zone_range * Decimal("0.10")).quantize(tick_size)
            buf_dec = max(base_buf, dyn_buf)      # ⬅️  둘 중 더 큰 값
        else:
            buf_dec = base_buf

        # ── 1) ‘트리거 Zone’ 이탈 기준 SL ──
        if trg_zone is not None:
            if direction == "long":
                sl_dec = (Decimal(str(trg_zone["low"])) - buf_dec).quantize(tick_size)
            else:
                sl_dec = (Decimal(str(trg_zone["high"])) + buf_dec).quantize(tick_size)
    
        # ── 2) fallback : 최근 OB extreme ──
        elif zone is not None:
            if direction == "long":
                sl_dec = (Decimal(str(zone["low"])) - buf_dec).quantize(tick_size)
            else:
                sl_dec = (Decimal(str(zone["high"])) + buf_dec).quantize(tick_size)
        # ── 2) fallback: 직전 캔들 extreme ──────────────────────────
        else:
            if direction == "long":
                extreme = Decimal(str(ltf["low"].iloc[-2])).quantize(tick_size)
                sl_dec = (extreme - buf_dec).quantize(tick_size)
            else:
                extreme = Decimal(str(ltf["high"].iloc[-2])).quantize(tick_size)
                sl_dec = (extreme + buf_dec).quantize(tick_size)

        # ── 3) 방향-무결성(SL이 Risk 쪽) 검사  ────────────────────
        #    ↳ 조건이 맞지 않으면 fallback extreme 로 강제 교체
        if direction == "long" and sl_dec >= entry_dec:
            extreme = Decimal(str(ltf["low"].iloc[-2])).quantize(tick_size)
            sl_dec  = (extreme - buf_dec).quantize(tick_size)
        elif direction == "short" and sl_dec <= entry_dec:
            extreme = Decimal(str(ltf["high"].iloc[-2])).quantize(tick_size)
            sl_dec  = (extreme + buf_dec).quantize(tick_size)

        # ── 4) 최소 SL 간격 보정 (전역 MIN_SL_TICKS 사용) ───────────
        min_gap = tick_size * MIN_SL_TICKS
        if abs(entry_dec - sl_dec) < min_gap:
            adj = min_gap - abs(entry_dec - sl_dec)
            sl_dec = (sl_dec + adj) if direction == "short" else (sl_dec - adj)
            sl_dec = sl_dec.quantize(tick_size)

        # ── 5) **리스크-가드** : 엔트리-SL 간격이 0.03 % 미만이면 강제 확대 ───
        # Decimal ÷ Decimal → Decimal 로 맞추면 부동소수 오차 ↓
        min_rr = Decimal("0.0003")            # 0.03 %
        risk_ratio = (abs(entry_dec - sl_dec) / entry_dec).quantize(Decimal("0.00000001"))
        if risk_ratio < min_rr:
            # `adj` 도 Decimal 로 맞추면 바로 `.quantize()` 가능
            adj = (min_rr * entry_dec - abs(entry_dec - sl_dec)).quantize(tick_size)
            sl_dec = (sl_dec - adj) if direction == "long" else (sl_dec + adj)
            sl_dec = sl_dec.quantize(tick_size)

        # ── 4) RR 비율 동일하게 TP 산출 ────────────────────────────
        rr_dec = Decimal(str(RR))
        if direction == "long":
            tp_dec = (entry_dec + (entry_dec - sl_dec) * rr_dec).quantize(tick_size)
        else:
            tp_dec = (entry_dec - (sl_dec - entry_dec) * rr_dec).quantize(tick_size)

        sl, tp = float(sl_dec), float(tp_dec)

        # ───────── 디버그 출력 위치 ─────────
        print(f"[DEBUG][SL-CALC] {symbol} "
              f"trg={trg_zone} zone={zone} "
              f"entry={entry:.4f} sl={sl:.4f} tp={tp:.4f}")
        
        order_ok = False
        if is_gate:
            balance = gate_get_balance()
            qty = calculate_quantity_gate(symbol, entry, balance, leverage)
            print(f"[GATE] 잔고={balance:.2f}, 수량={qty}")
            
            if qty <= 0:
                return
            order_ok = gate_order_with_tp_sl(
                symbol,
                "buy" if direction == "long" else "sell",
                qty, tp, sl, leverage
            )
        else:
            # ⚠️  진입 비중 = “총 잔고 10 %”
            qty = calculate_quantity(
                symbol,
                entry,
                get_total_balance(),         # ← 전체 시드 전달
                leverage,
            )
            if qty <= 0:
                return
            order_ok = binance_order_with_tp_sl(
                symbol,
                "buy" if direction == "long" else "sell",
                qty, tp, sl            # <-- hedge 파라미터 제거
            )

        if order_ok:
            # pm.enter() 내부에서 SL 주문까지 생성하므로
            # 중복 update_stop_loss() 호출을 제거합니다
            basis = None
            if trg_zone is not None:                 # ← NameError 방지
                basis = (
                    f"{trg_zone['kind'].upper()} "
                    f"{trg_zone['low']}~{trg_zone['high']}"
                )
            pm.enter(symbol, direction, entry, sl, tp, basis=basis)
        else:
            print(f"[WARN] 주문 실패로 포지션 등록 건너뜀 | {symbol}")
            send_discord_debug(f"[WARN] 주문 실패 → 포지션 미등록 | {symbol}", "aggregated")
        pm.update_price(symbol, entry, ltf_df=ltf)      # MSS 보호선 갱신

    except Exception as e:
        print(f"[ERROR] {symbol} {htf_tf}/{ltf_tf} → {e}", "aggregated")
        #send_discord_debug(f"[ERROR] {symbol} {htf_tf}/{ltf_tf} → {e}", "aggregated")

def calculate_sl_tp(entry: float, direction: str, buffer: float, rr: float):
    if direction == 'long':
        sl = entry * (1 - buffer)
        tp = entry + (entry - sl) * rr
    else:
        sl = entry * (1 + buffer)
        tp = entry - (sl - entry) * rr
    return float(sl), float(tp)

def initialize():
    print("🚀 [INIT] 초기 세팅 시작")
    send_discord_message("🚀 [INIT] 초기 세팅 시작", "aggregated")
    initialize_historical()
    failed_positions = []
    gate_leverage_ok   = []
    failed_leverage    = []

    # ─── Binance 초기화 ─────────────────────────
    if ENABLE_BINANCE:
        for symbol, data in SYMBOLS_BINANCE.items():
            # ── 포지션 동기화 ──
            try:
                pos = binance_pos(symbol)
                if pos and 'entry' in pos and 'direction' in pos:
                    sl, tp = calculate_sl_tp(
                        pos['entry'], pos['direction'], SL_BUFFER, RR
                    )
                    pm.init_position(symbol, pos['direction'], pos['entry'], sl, tp)
            except Exception:
                failed_positions.append(symbol)

            # ── 레버리지 세팅 ──
            try:
                max_lev   = get_max_leverage(symbol)
                req_lev   = data['leverage']
                applied   = min(req_lev, max_lev)
                set_leverage(symbol, applied)
            except Exception as e:
                print(f"[WARN] 레버리지 설정 실패: {symbol} → {e}")
                failed_leverage.append(symbol)

    # ─── Gate 초기화 ───────────────────────────
    if ENABLE_GATE:
        for symbol in SYMBOLS_GATE:
            try:
                # quiet=True ⇒ 개별 성공 로그 생략
                gate_set_leverage(symbol, DEFAULT_LEVERAGE, quiet=True)
                gate_leverage_ok.append(symbol)
            except Exception as e:
                failed_leverage.append(symbol)

    # ─── 레버리지 결과 요약 한 줄 출력 ──────────────────────
    if ENABLE_GATE:
        lev_used = f"{DEFAULT_LEVERAGE}배"
        ok_cnt   = len(gate_leverage_ok)
        fail_cnt = len(failed_leverage)
        ok_sym   = ", ".join(gate_leverage_ok)
        fail_sym = ", ".join(failed_leverage)
        print(f"[GATE] 레버리지 {lev_used}: ✅ 성공 {ok_cnt}개 / ❌ 실패 {fail_cnt}개")
        if fail_cnt:
            print(f"       실패 심볼 → {fail_sym}")
        send_discord_debug(f"[GATE] 레버리지{lev_used} 설정: OK={ok_cnt}, FAIL={fail_cnt}","gateio")

    if failed_positions:
        warn_msg = f"⚠️ 포지션 조회 실패: {', '.join(failed_positions)}"
        print(f"[WARN] {warn_msg}")
        send_discord_debug(warn_msg, "aggregated")
    if failed_leverage:
        warn_msg = f"⚠️ 레버리지 설정 실패: {', '.join(failed_leverage)}"
        print(f"[WARN] {warn_msg}")
        send_discord_debug(warn_msg, "aggregated")
async def strategy_loop():
    print("📈 전략 루프 시작됨 (5초 간격)")
    send_discord_message("📈 전략 루프 시작됨 (5초 간격)", "aggregated")
    while True:
        # ───── Binance 스윙 1h→5m ─────
        if ENABLE_BINANCE:
            for symbol, meta in SYMBOLS_BINANCE.items():
                await handle_pair(symbol, meta, "1h", "5m")

        # ───── Binance 단타 15m→1m (테스트) ─────
        #for symbol, meta in SYMBOLS.items():
        #    await handle_pair(symbol, meta, "15m", "1m")

        # ───── Gate.io 단타 15m→1m (듀얼 모드 전용) ─────
        if ENABLE_GATE:
            for symbol in SYMBOLS_GATE:
                try:
                    gate_sym = to_gate(symbol)
                except ValueError as e:
                    print(f"[WARN] Gate 미지원 심볼 제외: {symbol} ({e})")
                    continue
                await handle_pair(gate_sym, {}, "15m", "1m")
# ──────────────────────────────────────────────────────────────
        await asyncio.sleep(5)

        # ─── 수동(외부) 청산 ↔ 내부 포지션 동기화 ───
        await reconcile_internal_with_live()
        maybe_send_weekly_report(datetime.now(timezone.utc))

        if datetime.utcnow().second % 30 == 0:   # 30초마다
            print(f"[HB] {datetime.utcnow().isoformat()} loop alive")


# 내부(pm) ↔ 거래소 포지션 자동 동기화
async def reconcile_internal_with_live():
    """
    ① 내부 pm 에는 있지만 거래소에는 없는 경우  → force_exit()  
    ② (선택) 거래소에만 있는 포지션은 pm.init_position() 으로 끌어오기
    """
    for sym in pm.active_symbols():                 # 심볼 목록
        live = get_open_position(sym)
        # live 가 None 이거나 size == 0  → 수동 청산됐다고 판단
        if not live or abs(live.get("entry", 0)) == 0:
            print(f"[SYNC] 내부포지션 폐기(수동청산 감지) → {sym}")
            # on_exit() 호출로 P&L 정산 & 잠금 해제
            from core.monitor import on_exit
            try:
                price = pm.last_price(sym)
            except Exception:
                price = live.get("price", 0) if live else 0
            pm.force_exit(sym, price)                # 내부 on_exit 포함

    # ② 옵션 : 거래소에만 존재하고 내부엔 없는 포지션 동기화
    #   필요한 경우 아래 블록 주석 제거
    """
    all_symbols = list(SYMBOLS.keys())       # Binance 심볼 기준
    if ENABLE_GATE:
        all_symbols += [to_gate(s) for s in SYMBOLS_GATE]
    for sym in all_symbols:
        if pm.has_position(sym):
            continue
        live = get_open_position(sym)
        if live and abs(live.get("entry", 0)) > 0:
            dir_ = live["direction"]
            entry = live["entry"]
            sl, tp = calculate_sl_tp(entry, dir_, SL_BUFFER, RR)
            print(f"[SYNC] 외부 포지션 가져오기 → {sym}")
            pm.init_position(sym, dir_, entry, sl, tp)
    """

async def main():
    initialize()
    await asyncio.gather(
        start_data_feed(),   # 🌟 Binance + Gate 동시 실행
        strategy_loop()
    )

def force_entry(symbol, side, qty_override=None):
    """
    임시·수동 진입(디버그)용 헬퍼  
    side == "buy"  ➜ long,  "sell" ➜ short
    TP·SL를 **진입 방향과 일치**하도록 1 % 고정
    """
    # 현재 마크가격 조회 (Gate·Binance 모두 지원)
    if symbol.endswith("_USDT"):
        if not ENABLE_GATE:
            print("❌ Gate.io 기능이 비활성화 상태입니다 (ENABLE_GATE=False)")
            return
        import requests, json, time, requests

        def gate_mark(s: str) -> float:
            """mark_price → 실패 시 ticker 로 Fallback"""
            url = f"https://fx-api.gateio.ws/api/v4/futures/usdt/mark_price/{s}"
            data = requests.get(url, timeout=3).json()
            if isinstance(data, dict) and "mark_price" in data:
                return float(data["mark_price"])

            # ─ fallback: /tickers (배열)
            tick = requests.get(
                "https://fx-api.gateio.ws/api/v4/futures/usdt/tickers",
                params={"contract": s},
                timeout=3,
            ).json()
            if tick and isinstance(tick, list):
                return float(tick[0]["last"])
            raise RuntimeError(f"Gate mark price fetch failed: {data}")

        price = gate_mark(symbol)
    else:
        import requests
        mk = requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}").json()
        price = float(mk["markPrice"])
        
    # ───────── 수량 결정 ─────────
    leverage = DEFAULT_LEVERAGE

    if qty_override is not None:
        # 사용자가 --qty 로 직접 지정
        size = qty_override
    else:
        # 자동 산출
        if symbol.endswith("_USDT"):      # Gate 선물
            # Gate 잔고 조회 함수명 통일
            size = calculate_quantity_gate(symbol, price, gate_get_balance(), leverage)
        else:                             # Binance 선물
            set_leverage(symbol, leverage)      # 미리 적용
            size = calculate_quantity(symbol, price, get_available_balance(), leverage)

    if size <= 0:
        print("❌ 최소 주문 수량 미달 – 강제 진입 취소")
        return

    if side.lower() == "buy":      # long
        tp = price * 1.01          # +1 % 이익
        sl = price * 0.99          # −1 % 손절
    else:                          # short
        tp = price * 0.99          # −1 % 이익
        sl = price * 1.01          # +1 % 손절

    print(f"🚀 강제 진입 테스트: {symbol}, side={side}, size={size}, TP={tp}, SL={sl}")
    
    if symbol.endswith("_USDT"):          # Gate 선물
        # Gate 주문 함수는 gate_order_with_tp_sl 로 통일
        ok = gate_order_with_tp_sl(symbol, side, size, tp, sl, leverage)
    else:                                 # Binance 선물 심볼
        ok = binance_order_with_tp_sl(symbol, side, size, tp, sl)

    print("✅ 강제 진입 성공" if ok else "❌ 강제 진입 실패")


# entrypoint
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SMC-Trader launcher")
    parser.add_argument("--demo",  action="store_true",
                        help="강제 진입(debug)만 실행하고 종료")
    parser.add_argument("--side",  default="buy",
                        choices=["buy", "sell"], help="강제 진입 방향")
    parser.add_argument("--sym",   default="XRPUSDT",
                        help="거래 심볼")
    parser.add_argument("--qty",   type=float, default=None,
                        help="테스트용 강제 수량(지정 시 자동 계산 건너뜀)")
    args = parser.parse_args()

    if args.demo:
        # ▸ 단발성 진입 테스트만 수행
        force_entry(args.sym, args.side, args.qty)
    else:
        # ▸ 전체 전략 루프 실행
        asyncio.run(main())
