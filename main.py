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
from notify.discord import send_discord_debug, send_discord_message
# settings 에서 새로 만든 TF 상수도 같이 가져온다
from config.settings import (
    SYMBOLS,
    SYMBOLS_BINANCE,
    SYMBOLS_GATE,
    RR,
    SL_BUFFER,
    MIN_TP_DISTANCE_PCT,
    MIN_SL_DISTANCE_PCT,
    DEFAULT_LEVERAGE,
    ENABLE_GATE,
    ENABLE_BINANCE,
    HTF_TF,
    LTF_TF,
)
from core.data_feed import (
    candles, initialize_historical, start_data_feed,
    to_binance, is_gate_sym,
)
from core.iof import is_iof_entry
from core.position import PositionManager
from core.monitor import maybe_send_weekly_report
from core.ob import detect_ob
from core.confirmation import confirm_ltf_reversal   # ← 추가
from core.liquidity import detect_equal_levels, get_nearest_liquidity_level, is_liquidity_sweep  # ← 추가
# 〃 무효-블록 유틸 가져오기
from core.iof import is_invalidated, mark_invalidated
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
#  ■ '[OB][' 또는 '[BB][' 로 시작하고 'NEW' 가 없는 "요약" 라인은
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
from core.position import PositionManagerExtended
pm = PositionManagerExtended()
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
    # Binance REST 는 'BTCUSDT', Gate 는 원형 유지
    base_sym = to_binance(symbol) if not is_gate else symbol

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
        # ── tickSize 가져오기 (Mock 모드 포함 안전 버전)
        from exchange.router import get_tick_size as router_tick
        tick_src = get_tick_size_gate if is_gate else router_tick
        tick_size = Decimal(str(tick_src(base_sym)))

        # ⬇️ htf 전체 DataFrame을 그대로 넘겨야 attrs 를 활용할 수 있음
        signal, direction, trg_zone = is_iof_entry(htf, ltf, tick_size)
        if not signal or direction is None:
            return

        # ───── LTF(1m·5m) 반전이 확인될 때까지 대기 ─────
        if not confirm_ltf_reversal(ltf, direction):
            print(f"[WAIT] {symbol} – 아직 LTF 리젝션 미확인. 진입 보류")
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
        # OB 리스트를 기관성 점수 기준으로 정렬
        ltf_obs = detect_ob(ltf)
        ltf_obs_sorted = sorted(ltf_obs, key=lambda x: x.get('institutional_score', 0), reverse=True)
        
        for ob in ltf_obs_sorted:
            # ① FVG 조건부 허용 (HTF 확인 시에만)
            if ob.get("pattern") == "fvg":
                # HTF에서 강한 구조 확인 시에만 FVG 허용
                htf_structure = detect_structure(htf)
                recent_structure = htf_structure['structure'].dropna().tail(3)
                
                strong_structure_signals = ['BOS_up', 'BOS_down', 'CHoCH_up', 'CHoCH_down']
                has_strong_htf_confirmation = any(signal in recent_structure.values for signal in strong_structure_signals)
                
                if not has_strong_htf_confirmation:
                    print(f"[FVG] {symbol} FVG 스킵 - HTF 구조 확인 부족")
                    continue
                else:
                    print(f"[FVG] {symbol} FVG 허용 - HTF 구조 확인됨")

            # ② 이미 무효화된 OB/BB 면 스킵
            if is_invalidated(symbol, "ob", htf_tf, ob["high"], ob["low"]):
                continue

            if ob["type"].lower() == direction:     # 방향 일치하는 블록
                # 기관성 점수가 높은 OB 우선 선택
                institutional_score = ob.get('institutional_score', 0)
                if institutional_score >= 1:
                    print(f"[OB] {symbol} 기관성 OB 선택 (점수: {institutional_score})")
                zone = ob
                break
        entry_dec = Decimal(str(entry))

        # ── 공통 버퍼 계산 ────────
        # (1) **기본 버퍼** : 환경 상수 × tick
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

        # ── 1) '트리거 Zone' 이탈 기준 SL ──
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
            # 직전 캔들 extreme
            if direction == "long":
                sl_dec = (Decimal(str(ltf["low"].iloc[-1])) - buf_dec).quantize(tick_size)
            else:
                sl_dec = (Decimal(str(ltf["high"].iloc[-1])) + buf_dec).quantize(tick_size)

        # ── 3) SL 최소 거리 검증 ────────────────────────────────
        entry_dec = Decimal(str(entry))
        risk_ratio = abs(entry_dec - sl_dec) / entry_dec
        min_rr = Decimal(str(MIN_SL_DISTANCE_PCT)) # 설정값 사용

        # 최소 거리 미달 시 확대
        if risk_ratio < min_rr:
            # 필요한 조정량을 Decimal 로 맞추면 바로 `.quantize()` 가능
            adj = (min_rr * entry_dec - abs(entry_dec - sl_dec)).quantize(tick_size)
            sl_dec = (sl_dec - adj) if direction == "long" else (sl_dec + adj)
            sl_dec = sl_dec.quantize(tick_size)
            print(f"[SL] {symbol} SL 최소 거리 확대: {float(risk_ratio*100):.2f}% → {float(min_rr*100):.2f}%")

        # ── 4) 유동성 레벨 기반 TP 설정 (우선순위: 유동성 > 반대 OB > RR) ─────────────────────
        tp_dec = None
        
        # 4-1) 유동성 레벨 기반 TP 설정
        try:
            htf_liquidity_levels = detect_equal_levels(htf)
            nearest_liquidity = get_nearest_liquidity_level(htf_liquidity_levels, entry, direction)
            
            if nearest_liquidity:
                liquidity_tp = Decimal(str(nearest_liquidity['price'])).quantize(tick_size)
                
                # 최소 TP 거리 검증 (진입가 대비 최소 1% 이상)
                min_tp_distance = entry_dec * Decimal(str(MIN_TP_DISTANCE_PCT))
                if direction == "long":
                    if liquidity_tp - entry_dec >= min_tp_distance:
                        tp_dec = liquidity_tp
                        print(f"[TP] {symbol} 유동성 레벨 기반 TP: {float(tp_dec):.5f} (강도: {nearest_liquidity['strength']})")
                    else:
                        print(f"[TP] {symbol} 유동성 TP 너무 가까움 - 최소 거리 미달: {float(liquidity_tp):.5f} (필요: {float(entry_dec + min_tp_distance):.5f})")
                else:  # short
                    if entry_dec - liquidity_tp >= min_tp_distance:
                        tp_dec = liquidity_tp
                        print(f"[TP] {symbol} 유동성 레벨 기반 TP: {float(tp_dec):.5f} (강도: {nearest_liquidity['strength']})")
                    else:
                        print(f"[TP] {symbol} 유동성 TP 너무 가까움 - 최소 거리 미달: {float(liquidity_tp):.5f} (필요: {float(entry_dec - min_tp_distance):.5f})")
        except Exception as e:
            print(f"[TP] {symbol} 유동성 분석 실패: {e}")
        
        # 4-2) fallback: HTF 반대 OB extreme에 TP 설정
        if tp_dec is None:
            htf_ob = detect_ob(htf)      # htf = HTF DataFrame, 위에서 이미 attrs 세팅됨
            # direction에 따라 opposite OB
            if direction == "long":
                # 가장 가까운 위쪽 bearish OB의 low
                candidates = [Decimal(str(z["low"])) for z in htf_ob if z["type"] == "bearish" and Decimal(str(z["low"])) > entry_dec]
                if candidates:
                    ob_tp = min(candidates)
                    # 최소 거리 검증
                    if ob_tp - entry_dec >= entry_dec * Decimal(str(MIN_TP_DISTANCE_PCT)):
                        tp_dec = ob_tp
                        print(f"[TP] {symbol} HTF 반대 OB 기반 TP: {float(tp_dec):.5f}")
                    else:
                        print(f"[TP] {symbol} HTF OB TP 너무 가까움 - 최소 거리 미달: {float(ob_tp):.5f}")
            else:
                # 가장 가까운 아래 bullish OB의 high
                candidates = [Decimal(str(z["high"])) for z in htf_ob if z["type"] == "bullish" and Decimal(str(z["high"])) < entry_dec]
                if candidates:
                    ob_tp = max(candidates)
                    # 최소 거리 검증
                    if entry_dec - ob_tp >= entry_dec * Decimal(str(MIN_TP_DISTANCE_PCT)):
                        tp_dec = ob_tp
                        print(f"[TP] {symbol} HTF 반대 OB 기반 TP: {float(tp_dec):.5f}")
                    else:
                        print(f"[TP] {symbol} HTF OB TP 너무 가까움 - 최소 거리 미달: {float(ob_tp):.5f}")

        # 4-3) fallback: 기존 RR TP (최소 거리 보장)
        if tp_dec is None:
            rr_dec = Decimal(str(RR))
            if direction == "long":
                tp_dec = (entry_dec + (entry_dec - sl_dec) * rr_dec).quantize(tick_size)
            else:
                tp_dec = (entry_dec - (sl_dec - entry_dec) * rr_dec).quantize(tick_size)
            print(f"[TP] {symbol} RR 기반 TP: {float(tp_dec):.5f} (RR: {float(rr_dec)})")
        else:
            tp_dec = tp_dec.quantize(tick_size)

        # ★ SL은 pm.enter()에서 개선된 로직으로 계산하도록 변경
        # 기존 SL 계산 결과는 참고용으로만 사용
        calculated_sl = float(sl_dec)
        tp = float(tp_dec)

        # ── 5) 유동성 사냥 후 진입 확인 ─────────────────────
        liquidity_sweep_confirmed = False
        try:
            ltf_liquidity_levels = detect_equal_levels(ltf)
            
            # 진입 방향에 따른 유동성 사냥 확인
            if direction == "long":
                # LONG 진입: 하락 방향 유동성 사냥 후 반전 확인
                for level in ltf_liquidity_levels:
                    if level['type'] == 'sell_side_liquidity' and level['price'] < entry:
                        if is_liquidity_sweep(ltf, level['price'], 'down'):
                            liquidity_sweep_confirmed = True
                            print(f"[LIQUIDITY] {symbol} LONG 진입 - SSL 사냥 감지 @ {level['price']:.5f}")
                            break
            else:
                # SHORT 진입: 상승 방향 유동성 사냥 후 반전 확인
                for level in ltf_liquidity_levels:
                    if level['type'] == 'buy_side_liquidity' and level['price'] > entry:
                        if is_liquidity_sweep(ltf, level['price'], 'up'):
                            liquidity_sweep_confirmed = True
                            print(f"[LIQUIDITY] {symbol} SHORT 진입 - BSL 사냥 감지 @ {level['price']:.5f}")
                            break
            
            # 유동성 사냥이 없으면 진입 보류
            if not liquidity_sweep_confirmed:
                print(f"[LIQUIDITY] {symbol} 유동성 사냥 미확인 - 진입 보류")
                return  # 유동성 사냥 확인 필수
        except Exception as e:
            print(f"[LIQUIDITY] {symbol} 유동성 사냥 확인 실패: {e}")
            # 오류 시 기존 로직 유지
            pass
        
        # ───────── 디버그 출력 위치 ─────────
        print(f"[DEBUG][SL-CALC] {symbol} "
              f"trg={trg_zone} zone={zone} "
              f"entry={entry:.4f} calculated_sl={calculated_sl:.4f} tp={tp:.4f} "
              f"liquidity_sweep={liquidity_sweep_confirmed}")
        
        order_ok = False
        if is_gate:
            balance = gate_get_balance()
            qty = calculate_quantity_gate(symbol, entry, balance, leverage)
            print(f"[GATE] 잔고={balance:.2f}, 수량={qty}")
            
            if qty <= 0:
                return
            # Gate에서는 기존 계산된 SL 사용 (거래소 주문용)
            order_ok = gate_order_with_tp_sl(
                symbol,
                "buy" if direction == "long" else "sell",
                qty, tp, calculated_sl, leverage
            )
        else:
            # ⚠️  진입 비중 = "총 잔고 10 %"
            qty = calculate_quantity(
                symbol,
                entry,
                get_total_balance(),         # ← 전체 시드 전달
                leverage,
            )
            if qty <= 0:
                return
            # Binance에서는 기존 계산된 SL 사용 (거래소 주문용)
            order_ok = binance_order_with_tp_sl(
                symbol,
                "buy" if direction == "long" else "sell",
                qty, tp, calculated_sl            # <-- hedge 파라미터 제거
            )

        if order_ok:
            try:
                # ───────── 상세 진입근거 구성 ─────────
                entry_reason = []
                
                # 1. 기본 진입 근거
                if trg_zone is not None:
                    basis = (
                        f"{trg_zone['kind'].upper()} "
                        f"{trg_zone['low']}~{trg_zone['high']}"
                    )
                    entry_reason.append(f"진입근거: {basis}")
                elif zone is not None:
                    basis = (
                        f"{zone.get('pattern','ZONE').upper()} "
                        f"{zone['low']}~{zone['high']}"
                    )
                    entry_reason.append(f"진입근거: {basis}")
                else:
                    basis = f"NO_BLOCK zone=None"
                    entry_reason.append(f"진입근거: {basis}")
                
                # 2. SL 설정 근거
                if trg_zone is not None:
                    entry_reason.append(f"SL근거: 트리거존 {trg_zone['kind'].upper()} 하단 + 버퍼")
                elif zone is not None:
                    entry_reason.append(f"SL근거: {zone.get('pattern','ZONE').upper()} 하단 + 버퍼")
                else:
                    entry_reason.append("SL근거: 직전 캔들 extreme + 버퍼")
                
                # 3. TP 설정 근거
                if tp_dec is not None:
                    tp_method = "유동성레벨" if nearest_liquidity else "HTF반대OB" if tp_dec != (entry_dec + (entry_dec - sl_dec) * Decimal(str(RR))).quantize(tick_size) else "RR기반"
                    entry_reason.append(f"TP근거: {tp_method} (거리: {abs(float(tp_dec) - entry):.3f})")
                
                # 4. 구조 확인
                if htf_struct is not None and 'structure' in htf_struct.columns:
                    recent_structure = htf_struct['structure'].dropna().tail(3)
                    if len(recent_structure) > 0:
                        last_structure = recent_structure.iloc[-1]
                        entry_reason.append(f"HTF구조: {last_structure}")
                
                # 5. 유동성 사냥 확인
                if liquidity_sweep_confirmed:
                    entry_reason.append("유동성사냥: 확인됨")
                else:
                    entry_reason.append("유동성사냥: 미확인")
                
                # 상세 근거를 문자열로 결합
                detailed_basis = " | ".join(entry_reason)
                
                # MSS-only 진입이면 trg_zone 안에 보호선이 같이 들어옴
                prot_lv = trg_zone.get("protective") if isinstance(trg_zone, dict) else None
                
                # ★ 개선된 pm.enter() 호출 - HTF 데이터와 trigger_zone 전달
                pm.enter(
                    symbol=symbol,
                    direction=direction,
                    entry=entry,
                    sl=None,  # SL은 pm.enter()에서 개선된 로직으로 계산
                    tp=tp,
                    basis=detailed_basis,
                    protective=prot_lv,
                    htf_df=htf,          # ★ HTF 데이터 전달
                    trigger_zone=trg_zone  # ★ 진입근거 존 정보 전달
                )
            except Exception as e:
                print(f"[ERROR] 포지션 등록 실패: {symbol} → {e}")
                # 오류 발생 시 디스코드 알림
                send_discord_message(f"❌ [ERROR] {symbol} 포지션 등록 실패: {e}", "aggregated")
        else:
            print(f"❌ [ORDER] {symbol} 주문 실패")
            send_discord_message(f"❌ [ORDER] {symbol} 주문 실패", "aggregated")
        pm.update_price(symbol, entry, ltf_df=ltf)      # MSS 보호선 갱신

        # ───────── 블록 무효화 감시 ─────────
        try:                                             # tickSize 확보
            tick_val = get_tick_size_gate(symbol) if is_gate else get_tick_size(base_sym)
            tick_val = float(tick_val or 0)
        except Exception:
            tick_val = 0

        if zone and tick_val:
            hi, lo   = float(zone["high"]), float(zone["low"])
            breach   = tick_val * 2                      # 2-tick 이상 돌파 시 소멸
            if direction == "long"  and entry < lo - breach:
                mark_invalidated(symbol, "ob", htf_tf, hi, lo)
            elif direction == "short" and entry > hi + breach:
                mark_invalidated(symbol, "ob", htf_tf, hi, lo)

    except Exception as e:
        print(f"[ERROR] {symbol} {htf_tf}/{ltf_tf} → {e}")
        send_discord_debug(f"[ERROR] {symbol} {htf_tf}/{ltf_tf} → {e}", "aggregated")

def calculate_sl_tp(entry: float, direction: str, buffer: float, rr: float):
    """fallback-용 단순 SL/TP 계산 (entry ± buffer %, RR 고정)"""
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
        # ───── Binance (HTF ➜ LTF) ──────
        if ENABLE_BINANCE:
            for symbol, meta in SYMBOLS_BINANCE.items():
                await handle_pair(symbol, meta, HTF_TF, LTF_TF)

        # ───── Gate.io (HTF ➜ LTF) ─────
        if ENABLE_GATE:
            for symbol in SYMBOLS_GATE:
                try:
                    gate_sym = to_gate(symbol)
                except ValueError as e:
                    print(f"[WARN] Gate 미지원 심볼 제외: {symbol} ({e})")
                    continue
                await handle_pair(gate_sym, {}, HTF_TF, LTF_TF)
# ──────────────────────────────────────────────────────────────
        await asyncio.sleep(5)

        # ─── 수동(외부) 청산 ↔ 내부 포지션 동기화 ───
        await reconcile_internal_with_live()
        maybe_send_weekly_report(datetime.now(timezone.utc))
        
        now_utc = datetime.now(timezone.utc)
        if now_utc.second % 30 == 0:             # 30초마다
            print(f"[HB] {now_utc.isoformat()} loop alive")


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


# ────────────────────────────────────────────────
#  📍 SL 검증 명령어 (수동 실행용)
# ────────────────────────────────────────────────
def check_all_stop_losses():
    """
    모든 포지션의 SL 주문 존재 여부를 확인하고 누락된 경우 재생성
    터미널에서 수동으로 호출할 수 있는 함수
    """
    print("=" * 50)
    print("🔍 모든 포지션의 SL 검증을 시작합니다...")
    print("=" * 50)
    
    try:
        pm.force_ensure_all_stop_losses()
        print("✅ SL 검증이 완료되었습니다.")
    except Exception as e:
        print(f"❌ SL 검증 중 오류 발생: {e}")
        send_discord_debug(f"[ERROR] SL 검증 중 오류: {e}", "aggregated")

# 전역에서 접근 가능하도록 별칭 생성
verify_sl = check_all_stop_losses

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

# ────────────────────────────────────────────────
#  📍 백테스트 전용 싱글-틱 헬퍼
#     backtest.py 가 매 1분봉마다 호출
# ────────────────────────────────────────────────
def backtest_tick(symbol: str, candle: dict, exec_strategy: bool = True):
    """
    ▸ candle = {"timestamp": …, "open": …, "high": …, "low": …, "close": …, "volume": …}
    ▸ 1) core.data_feed.candles 데크에 캔들 적재
    ▸ 2) handle_pair() 로 기존 진입-판단 로직 실행
    """
    from core.data_feed import candles
    
    # ────────────────────────────────
    #  🔧 타임프레임 문자열 → 분 환산
    # ────────────────────────────────
    _tf_cache = {}
    def _tf_minutes(tf: str) -> int:
        if tf not in _tf_cache:
            unit = tf[-1]
            n    = int(tf[:-1])
            _tf_cache[tf] = n * (60 if unit == "h" else 1)
        return _tf_cache[tf]

    # LTF · HTF 간격 계산
    ltf_min = _tf_minutes(LTF_TF)   # ex) 5
    htf_min = _tf_minutes(HTF_TF)   # ex) 60

    # CSV가 5m봉이므로 바로 LTF 큐에 추가
    from collections import deque
    ltf_q = candles.setdefault(symbol, {}).setdefault(LTF_TF, deque(maxlen=3000))
    ltf_q.append(candle)

    # LTF → HTF 집계만
    ratio = htf_min // ltf_min        # ex) 60//5 = 12
    buf = backtest_tick.__dict__.setdefault("buf_htf", [])
    buf.append(candle)
    if len(buf) == ratio:
        htf_candle = {
            "timestamp": buf[0]["timestamp"],
            "time":      buf[0]["time"],
            "open":      buf[0]["open"],
            "high":      max(x["high"] for x in buf),
            "low":       min(x["low"]  for x in buf),
            "close":     buf[-1]["close"],
            "volume":    sum(x["volume"] for x in buf),
        }
        htf_q = candles[symbol].setdefault(HTF_TF, deque(maxlen=1000))
        htf_q.append(htf_candle)
        buf.clear()

    # 기존 전략 로직 호출 (동기 버전)
    try:
        asyncio.run(handle_pair(symbol, {}, HTF_TF, LTF_TF))
    except RuntimeError:
        # 이미 이벤트 루프가 돌고 있을 땐 새 루프 생성
        loop = asyncio.new_event_loop()
        loop.run_until_complete(handle_pair(symbol, {}, HTF_TF, LTF_TF))
        loop.close()