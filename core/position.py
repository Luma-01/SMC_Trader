# core/position.py

import time
from typing import Dict, Optional
from decimal import Decimal, ROUND_DOWN, ROUND_UP
# ── pandas 타입 힌트/연산에 사용 ──────────────────────
import pandas as pd

from core.protective import (
    get_ltf_protective,
    get_protective_level,      # ← MTF(5 m) 보호선
)
from config.settings import RR, USE_HTF_PROTECTIVE, HTF_TF   # ⬅︎ 스위치 import
from core.monitor import on_entry, on_exit     # ★ 추가
from exchange.binance_api import get_mark_price  # ★ 마크 가격 조회
from notify.discord import send_discord_message, send_discord_debug
import threading, json, os
from exchange.router import (
    update_stop_loss,
    update_take_profit,      # ★ NEW
    cancel_order,
    close_position_market,
    get_open_position,
)
from core.data_feed import ensure_stream

# ────── Tunable risk / SL 파라미터 (2025-07-04) ──────────────────
TRAILING_THRESHOLD_PCT = 0.008   # 0.8 % – 트레일링 SL 민감도
SAFETY_TICKS            = 1      # 내부 종료용 버퍼(틱) 2→1
MIN_RR_BASE             = 0.005  # 0.5 % – 최소 엔트리-SL 거리
# ----------------------------------------------------------------

class PositionManager:
    def __init__(self):
        self.positions: Dict[str, Dict] = {}
        # ▸ 마지막 종료 시각 저장  {symbol: epoch sec}
        self._cooldowns: Dict[str, float] = {}
        # ▸ 스탑로스 알림 중복 방지 {symbol: epoch sec}
        self._sl_alerts: Dict[str, float] = {}

        # 🔸 WS 시작 직후 거래소-실시간과 동기화
        self.sync_from_exchange()
        # 🔸 주기적 헬스체크 스레드
        threading.Thread(
            target=self._health_loop, daemon=True
        ).start()

    # --------------------------------------------------
    # 🟢 1)  실행-직후 싱크
    # --------------------------------------------------
    def sync_from_exchange(self):
        """
        Binance / Gate 의 현재 포지션·주문을 읽어
        self.positions 캐시를 재구성한다.
        """
        from config.settings import SYMBOLS            # 모든 심볼 목록
        for sym in SYMBOLS:
            try:
                live = get_open_position(sym)
            except Exception as e:
                print(f"[SYNC] {sym} REST 실패 → {e}")
                continue

            if live and sym not in self.positions:
                # ---- SL / TP 실가격 추출 -----------------------
                sl_px = tp_px = None
                try:
                    from exchange.binance_api import client as _c
                    open_orders = _c.futures_get_open_orders(symbol=sym)
                    for od in open_orders:
                        if od["type"] == "STOP_MARKET":
                            sl_px = float(od["stopPrice"])
                        elif od["type"] == "LIMIT" and od.get("reduceOnly"):
                            tp_px = float(od["price"])
                except Exception:
                    pass

                entry   = live["entry"]
                sl_px   = sl_px or (entry * 0.98)      # 대충 2 % 폴백
                tp_px   = tp_px or (entry * 1.02)
                self.init_position(
                    sym, live["direction"], entry, sl_px, tp_px
                )
                print(f"[SYNC] {sym} → 캐시 재생성 완료")

            elif (not live) and sym in self.positions:
                # 캐시에 있는데 실제론 이미 닫힘
                self.force_exit(sym)

    # --------------------------------------------------
    # 🟢 2)  15 초마다 헬스체크
    # --------------------------------------------------
    def _health_loop(self):
        while True:
            try:
                self.sync_from_exchange()
                # SL 검증 추가
                self._verify_stop_losses()
            except Exception as e:
                print(f"[HEALTH] sync 오류: {e}")
            time.sleep(15)          # ← 주기 조정 가능
    # ─────────  쿨-다운  헬퍼  ──────────
    COOLDOWN_SEC = 300          # ★ 5 분  (원하면 조정)

    def in_cooldown(self, symbol: str) -> bool:
        """True  → 아직 쿨-다운 시간 미경과"""
        t = self._cooldowns.get(symbol)
        return t is not None and (time.time() - t) < self.COOLDOWN_SEC

    # 현재 내부에서 '열려-있다'고 간주되는 심볼 리스트
    def active_symbols(self) -> list[str]:
        return list(self.positions.keys())
    
    # 외부(거래소)에서 이미 청산됐음을 감지했을 때 메모리에서 제거
    def force_exit(self, symbol: str, exit_price: float | None = None):
        """거래소에서 이미 닫혔다고 판단될 때 호출"""
        if symbol not in self.positions:
            return
        if exit_price is None:
            exit_price = self.positions[symbol].get("last_price",   # 직전가
                         self.positions[symbol]["entry"])           # 없으면 진입가
        from datetime import datetime, timezone
        on_exit(symbol, exit_price, datetime.now(timezone.utc))
        self.positions.pop(symbol, None)

    # 최근 가격을 가져오기 (없으면 KeyError)
    def last_price(self, symbol: str) -> float:
        return self.positions[symbol]["last_price"]

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions

    # basis: "OB 2800~2850", "BB_HTF 1.25~1.30" … 등 진입 근거 문자열
    def enter(
        self,
        symbol: str,
        direction: str,
        entry: float,
        sl: float | None = None,           # ← SL 미리 못 정한 경우 None 허용
        tp: float | None = None,
        basis: dict | str | None = None,   # ← dict 도 허용
        protective: float | None = None,   # ★ NEW
        htf_df: pd.DataFrame | None = None,  # ★ HTF 데이터 추가
        trigger_zone: dict | None = None,    # ★ 진입근거 존 정보 추가
    ):
        """포지션 등록 + 개선된 SL 산출

        * SMC 전략의 구조적 무효화 원칙에 따른 SL 산출
        * 우선순위: 진입근거 존 → HTF 구조적 무효화 → 보호선 → 최소 거리
        * 진입 직후 수 초 안에 트레일링 SL 이 갱신‑체결되는 현상을 막기 위해
          `created_at` 타임스탬프를 저장한다.
        """
        basis_txt = f" | {basis}" if basis else " | NO_BASIS"
        
        # ─── ① 개선된 SL 산출 로직 ─────────────────────────
        if sl is None:
            # HTF 데이터가 있으면 개선된 SL 산출 함수 사용
            if htf_df is not None and not htf_df.empty:
                from core.utils import calculate_improved_stop_loss
                
                try:
                    sl_result = calculate_improved_stop_loss(
                        symbol=symbol,
                        direction=direction,
                        entry_price=entry,
                        htf_df=htf_df,
                        protective=protective,
                        trigger_zone=trigger_zone,
                        min_rr_base=MIN_RR_BASE
                    )
                    
                    sl = sl_result['sl_level']
                    sl_reason = sl_result['reason']
                    sl_priority = sl_result['priority']
                    
                    print(f"[SL] {symbol} 개선된 SL 산출: {sl:.5f} | 근거: {sl_reason} | 우선순위: {sl_priority}")
                    send_discord_debug(f"[SL] {symbol} 개선된 SL: {sl:.5f} | {sl_reason}", "aggregated")
                    
                except Exception as e:
                    print(f"[SL] {symbol} 개선된 SL 산출 실패: {e} → 기존 로직 사용")
                    send_discord_debug(f"[SL] {symbol} 개선된 SL 산출 실패: {e}", "aggregated")
                    sl = None  # 기존 로직으로 폴백
            
            # 기존 로직 (폴백)
            if sl is None:
                # ①-A MSS-only protective 가 있으면 그대로
                if protective is not None:
                    sl = protective
                else:
                    # ①-B 최후 폴백 = 1 % 리스크
                    sl = entry * (1 - 0.01) if direction == "long" else entry * (1 + 0.01)

        # ─── ② 최소 리스크(거리) 검증 및 보정 ──────────────────
        try:
            from exchange.router import get_tick_size as _tick
            tick = float(_tick(symbol) or 0)
        except Exception:
            tick = 0

        # 최소 위험비 검증
        min_rr = max(MIN_RR_BASE, (float(tick) / entry) * 3 if tick else 0)
        
        if direction == "long":
            gap = (entry - sl) / entry
            if gap < min_rr:
                print(f"[SL] {symbol} SL 최소 거리 미달 ({gap:.4f} < {min_rr:.4f}) → 보정")
                sl = entry * (1 - min_rr)
        else:  # short
            gap = (sl - entry) / entry
            if gap < min_rr:
                print(f"[SL] {symbol} SL 최소 거리 미달 ({gap:.4f} < {min_rr:.4f}) → 보정")
                sl = entry * (1 + min_rr)

        # ─── ③ TP를 SL 기준으로 재계산 --------------------
        # ── tickSize 라운딩을 먼저 맞춘다 ──
        from exchange.router import get_tick_size as _tick
        tick = Decimal(str(_tick(symbol) or 0))
        risk = abs(entry - sl)
        tp_f = entry + risk * RR if direction == "long" else entry - risk * RR

        if tick:                              # tick 이 0 이면 그대로
            if direction == "long":
                tp_f = float(Decimal(str(tp_f)).quantize(tick, ROUND_UP))
            else:
                tp_f = float(Decimal(str(tp_f)).quantize(tick, ROUND_DOWN))
        tp = tp_f

        ensure_stream(symbol)
        self.positions[symbol] = {
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "last_price": entry,          # ← 한 번 넣어두면 KeyError 방지
            "half_exit": False,
            "protective_level": protective,          # ← 최초부터 보유
            "mss_triggered": False,
            "sl_order_id": None,
            "tp_order_id": None,          # ← TP 주문 ID 추가
            "initial_size": None,         # ← 초기 포지션 사이즈 추가
            "_created": time.time(),        # → 트레일링 SL grace‑period 용
            "trigger_zone": trigger_zone,    # ★ 진입근거 존 정보 저장
            "htf_df": htf_df,               # ★ HTF 데이터 저장 (참조용)
        }
        on_entry(symbol, direction, entry, sl, tp)   # ★ 호출

        # 진입 시 SL 주문 생성 (강화된 로직)
        sl_success = False
        try:
            # 거래소별 SL 보장 로직
            from exchange.router import GATE_SET
            if symbol not in GATE_SET:
                # Binance의 경우 ensure_stop_loss 함수 사용
                from exchange.binance_api import ensure_stop_loss
                sl_success = ensure_stop_loss(symbol, direction, sl, max_retries=3)
            else:
                # Gate의 경우 ensure_stop_loss_gate 함수 사용
                from exchange.gate_sdk import ensure_stop_loss_gate
                sl_success = ensure_stop_loss_gate(symbol, direction, sl, max_retries=3)
                
            if sl_success:
                self.positions[symbol]['sl_order_id'] = None  # 실제 ID는 거래소에서 관리
                self.positions[symbol]['sl'] = sl
                print(f"[SL] 초기 SL 주문 등록 완료 | {symbol} @ {sl:.4f}")
                send_discord_debug(f"[SL] 초기 SL 주문 등록 완료 | {symbol} @ {sl:.4f}", "aggregated")
            else:
                print(f"[CRITICAL] {symbol} SL 주문 생성 실패 - 포지션 위험!")
                send_discord_debug(f"[CRITICAL] {symbol} SL 주문 생성 실패 - 포지션 위험!", "aggregated")
                
        except Exception as e:
            print(f"[ERROR] {symbol} SL 설정 중 오류: {e}")
            send_discord_debug(f"[ERROR] {symbol} SL 설정 중 오류: {e}", "aggregated")

        # ────────── TP 주문 생성 (절반 수량) ──────────
        tp_result = update_take_profit(symbol, direction, tp)
        if tp_result is True:       # 동일 TP → 주문 생략
            print(f"[TP] {symbol} TP unchanged")
        elif tp_result not in (False, True):
            self.positions[symbol]['tp_order_id'] = (
                tp_result if isinstance(tp_result, int) else None
            )
            print(f"[TP] 초기 TP 주문 등록 완료 | {symbol} @ {tp:.4f} (절반 수량)")
            send_discord_debug(f"[TP] 초기 TP 주문 등록 완료 | {symbol} @ {tp:.4f} (절반 수량)", "aggregated")
        else:
            print(f"[TP] {symbol} TP 주문 생성 실패")
            send_discord_debug(f"[TP] {symbol} TP 주문 생성 실패", "aggregated")

        # ────────── 초기 포지션 사이즈 저장 ──────────
        try:
            import time
            time.sleep(0.5)  # 주문 체결 대기
            pos = get_open_position(symbol)
            if pos:
                # 포지션 사이즈 추출
                def _get_pos_size(p: dict) -> float:
                    for k in ("size", "positionAmt", "qty", "amount"):
                        v = p.get(k)
                        if v not in (None, '', 0):
                            try:
                                return abs(float(v))
                            except (TypeError, ValueError):
                                continue
                    return 0.0
                
                initial_size = _get_pos_size(pos)
                self.positions[symbol]['initial_size'] = initial_size
                print(f"[ENTRY] {symbol} 초기 포지션 사이즈: {initial_size}")
                send_discord_debug(f"[ENTRY] {symbol} 초기 포지션 사이즈: {initial_size}", "aggregated")
        except Exception as e:
            print(f"[ENTRY] {symbol} 초기 포지션 사이즈 확인 실패: {e}")
            send_discord_debug(f"[ENTRY] {symbol} 초기 포지션 사이즈 확인 실패: {e}", "aggregated")

        # ────────── 메시지 구성 ──────────
        basis_txt = f"\n📋 {basis}" if basis else ""
        
        # 상세 정보 구성
        risk_distance = abs(entry - sl)
        reward_distance = abs(tp - entry)
        risk_reward_ratio = reward_distance / risk_distance if risk_distance > 0 else 0
        
        msg = (
            f"🚀 **[ENTRY]** {symbol} | {direction.upper()} @ {entry:.4f}\n"
            f"🛡️ SL: {sl:.4f} | 🎯 TP: {tp:.4f}\n"
            f"📊 리스크: {risk_distance:.4f} | 보상: {reward_distance:.4f} | R:R = {risk_reward_ratio:.2f}"
            f"{basis_txt}"
        )

        # ────────── 중복 알림 차단 ──────────
        if _ENTRY_CACHE.get(symbol) != msg:
            _ENTRY_CACHE[symbol] = msg        # 최근 메시지 기억
            print(msg)
            send_discord_message(msg, "aggregated")

    # ➊ 5 분 봉(DataFrame) 을 추가로 받을 수 있도록 인자 확장
    def update_price(
        self,
        symbol: str,
        current_price: float,
        ltf_df:  Optional[pd.DataFrame] = None,
        htf_df:  Optional[pd.DataFrame] = None,
    ):
        if symbol not in self.positions:
            return

        pos = self.positions[symbol]
        protective = pos.get("protective_level")       # ← 있을 수도/없을 수도
        pos["last_price"] = current_price          # ← 가장 먼저 업데이트
        direction = pos['direction']
        sl, tp = pos['sl'], pos['tp']
        entry = pos['entry']
        half_exit = pos['half_exit']
        mss_triggered = pos['mss_triggered']

        # ───────────────────────────────────────────────
        # ❶ 1차 TP(절반 익절) 달성 여부 **먼저** 확인
        #    – 트레일링으로 TP 가 올라가기 전에 판정해야
        #      'TP 상승→즉시 익절' 오류를 방지할 수 있다
        # ───────────────────────────────────────────────
        if not half_exit:
            # 실제 포지션 사이즈 확인을 통한 절반 익절 감지
            try:
                current_pos = get_open_position(symbol)
                if current_pos and pos.get('initial_size'):
                    # 현재 포지션 사이즈 추출
                    def _get_pos_size(p: dict) -> float:
                        for k in ("size", "positionAmt", "qty", "amount"):
                            v = p.get(k)
                            if v not in (None, '', 0):
                                try:
                                    return abs(float(v))
                                except (TypeError, ValueError):
                                    continue
                        return 0.0
                    
                    current_size = _get_pos_size(current_pos)
                    initial_size = pos['initial_size']
                    
                    # 포지션 사이즈가 60% 이하로 줄어들면 절반 익절로 판단 (약간의 여유 마진)
                    if current_size <= initial_size * 0.6:
                        if direction == "long":
                            print(f"[PARTIAL TP] {symbol} LONG 절반 익절 감지 @ {current_price:.5f} (포지션: {current_size:.6f} -> {initial_size:.6f})")
                            send_discord_message(f"[PARTIAL TP] {symbol} LONG 절반 익절 감지 @ {current_price:.5f}", "aggregated")
                        else:
                            print(f"[PARTIAL TP] {symbol} SHORT 절반 익절 감지 @ {current_price:.5f} (포지션: {current_size:.6f} -> {initial_size:.6f})")
                            send_discord_message(f"[PARTIAL TP] {symbol} SHORT 절반 익절 감지 @ {current_price:.5f}", "aggregated")
                        
                        send_discord_debug(f"[DEBUG] {symbol} {direction.upper()} 1차 익절 완료 (실제 포지션 감소)", "aggregated")
                        pos["half_exit"] = True

                        # ── NEW ── ① 익절 직후 SL → 본절(Entry)
                        new_sl = entry                         # breakeven
                        # tickSize 라운드 & 진입가와 ≥1 tick 차이 확보
                        from exchange.router import get_tick_size as _tick
                        tick = float(_tick(symbol) or 0)
                        if direction == "long":
                            new_sl = max(new_sl, sl + tick)    # 최소 1 tick ↑
                        else:  # short
                            new_sl = min(new_sl, sl - tick)    # 최소 1 tick ↓

                        if self.should_update_sl(symbol, new_sl):
                            sl_res = update_stop_loss(symbol, direction, new_sl)
                            # sl_res 가 'True' 이면 → SL 가격 변경 없음(no-op)
                            if isinstance(sl_res, bool) and sl_res is True:
                                print(f"[SL] {symbol} SL unchanged(=BE) – keep existing order")
                            elif sl_res is not False:
                                old_id = pos.get("sl_order_id")
                                pos["sl"] = new_sl
                                pos["sl_order_id"] = sl_res if isinstance(sl_res, int) else None
                                if old_id:
                                    cancel_order(symbol, old_id)
                                print(f"[SL->BE] {symbol} SL 본절로 이동 완료 @ {new_sl:.4f}")
                                send_discord_debug(f"[SL] {symbol} 본절로 이동 → {new_sl:.4f}", "aggregated")
                        return  # 절반 익절 처리 완료
                        
            except Exception as e:
                print(f"[PARTIAL TP] {symbol} 포지션 사이즈 확인 실패: {e}")
                # 실패 시 기존 방식으로 폴백
                if direction == "long" and current_price >= pos["tp"]:
                    print(f"[PARTIAL TP] {symbol} LONG 절반 익절 @ {current_price:.5f} (TP: {pos['tp']:.5f}) [폴백]")
                    send_discord_message(f"[PARTIAL TP] {symbol} LONG 절반 익절 @ {current_price:.5f} (TP: {pos['tp']:.5f})", "aggregated")
                    send_discord_debug(f"[DEBUG] {symbol} LONG 1차 익절 완료", "aggregated")
                    pos["half_exit"] = True

                    # ── NEW ── ① 익절 직후 SL → 본절(Entry)
                    new_sl = entry                         # breakeven
                    # tickSize 라운드 & 진입가와 ≥1 tick 차이 확보
                    from exchange.router import get_tick_size as _tick
                    tick = float(_tick(symbol) or 0)
                    if direction == "long":
                        new_sl = max(new_sl, sl + tick)    # 최소 1 tick ↑
                    else:  # short
                        new_sl = min(new_sl, sl - tick)    # 최소 1 tick ↓

                    if self.should_update_sl(symbol, new_sl):
                        sl_res = update_stop_loss(symbol, direction, new_sl)
                        # sl_res 가 'True' 이면 → SL 가격 변경 없음(no-op)
                        if isinstance(sl_res, bool) and sl_res is True:
                            print(f"[SL] {symbol} SL unchanged(=BE) – keep existing order")
                        elif sl_res is not False:
                            old_id = pos.get("sl_order_id")
                            pos["sl"] = new_sl
                            pos["sl_order_id"] = sl_res if isinstance(sl_res, int) else None
                            if old_id:
                                cancel_order(symbol, old_id)
                            print(f"[SL->BE] {symbol} SL 본절로 이동 완료 @ {new_sl:.4f}")
                            send_discord_debug(f"[SL] {symbol} 본절로 이동 → {new_sl:.4f}", "aggregated")
                
                elif direction == "short" and current_price <= pos["tp"]:
                    print(f"[PARTIAL TP] {symbol} SHORT 절반 익절 @ {current_price:.5f} (TP: {pos['tp']:.5f}) [폴백]")
                    send_discord_message(f"[PARTIAL TP] {symbol} SHORT 절반 익절 @ {current_price:.5f} (TP: {pos['tp']:.5f})", "aggregated")
                    send_discord_debug(f"[DEBUG] {symbol} SHORT 1차 익절 완료", "aggregated")
                    pos["half_exit"] = True

                    # ── NEW ── ① 익절 직후 SL → 본절(Entry)
                    new_sl = entry
                    from exchange.router import get_tick_size as _tick
                    tick = float(_tick(symbol) or 0)
                    if direction == "long":
                        new_sl = max(new_sl, sl + tick)
                    else:
                        new_sl = min(new_sl, sl - tick)

                    if self.should_update_sl(symbol, new_sl):
                        sl_res = update_stop_loss(symbol, direction, new_sl)
                        # sl_res 가 'True' 이면 → SL 가격 변경 없음(no-op)
                        if isinstance(sl_res, bool) and sl_res is True:
                            print(f"[SL] {symbol} SL unchanged(=BE) – keep existing order")
                        elif sl_res is not False:
                            old_id = pos.get("sl_order_id")
                            pos["sl"] = new_sl
                            pos["sl_order_id"] = sl_res if isinstance(sl_res, int) else None
                            if old_id:
                                cancel_order(symbol, old_id)
                            print(f"[SL->BE] {symbol} SL 본절로 이동 완료 @ {new_sl:.4f}")
                            send_discord_debug(f"[SL] {symbol} 본절로 이동 → {new_sl:.4f}", "aggregated")
        
        # ───────────────────────────────────────────────
        # ❷ SL/TP 는 **절반 익절 후에도** 계속 추적
        self.try_update_trailing_sl(symbol, current_price)

        # ──────────────────────────────────────────────────────────────
        #  📌 보호선(MSS) 로직은 **1차 익절(half_exit) 이후부터** 활성
        #      초기 SL 을 그대로 두고, 익절 뒤에만 '더 보수적' SL 로 교체
        # ──────────────────────────────────────────────────────────────
        candidates = []
        if half_exit:                                  # ← 핵심 변경
            # ───────── 개선된 보호선 산출 ─────────
            from core.protective import get_improved_protective_level
            
            try:
                # 저장된 HTF 데이터와 trigger_zone 사용
                stored_htf_df = pos.get("htf_df")
                stored_trigger_zone = pos.get("trigger_zone")
                
                improved_protective = get_improved_protective_level(
                    ltf_df=ltf_df,
                    htf_df=stored_htf_df if stored_htf_df is not None else htf_df,
                    direction=direction,
                    entry_price=entry,
                    trigger_zone=stored_trigger_zone,
                    use_htf=USE_HTF_PROTECTIVE
                )
                
                if improved_protective:
                    candidates.append(improved_protective["protective_level"])
                    print(f"[PROTECTIVE] {symbol} 개선된 보호선: {improved_protective['protective_level']:.5f} | "
                          f"근거: {improved_protective['reason']} | 우선순위: {improved_protective['priority']}")
                    send_discord_debug(f"[PROTECTIVE] {symbol} 개선된 보호선: {improved_protective['protective_level']:.5f} | "
                                     f"{improved_protective['reason']}", "aggregated")
                else:
                    print(f"[PROTECTIVE] {symbol} 개선된 보호선 산출 실패 → 기존 로직 사용")
                    
            except Exception as e:
                print(f"[PROTECTIVE] {symbol} 개선된 보호선 산출 오류: {e} → 기존 로직 사용")
                send_discord_debug(f"[PROTECTIVE] {symbol} 개선된 보호선 오류: {e}", "aggregated")
                
                # 기존 로직으로 폴백
                if ltf_df is not None:
                    p = get_ltf_protective(ltf_df, direction)
                    if p:
                        candidates.append(p["protective_level"])

                # ───────── HTF(5 m) 보호선 – 옵션 ────────
                if USE_HTF_PROTECTIVE and htf_df is not None:
                    # HTF_TF 를 사용하는 보호선 (lookback 파라미터는 필요에 따라 조정)
                    p = get_protective_level(htf_df, direction, lookback=12, span=2)
                    if p:
                        candidates.append(p["protective_level"])

        # half_exit 이전에는 candidates == [] → 아래 MSS 블록 스킵
        if candidates:
            new_protective = max(candidates) if direction == "long" else min(candidates)
            better_level   = (
                (direction == "long"  and (protective is None or new_protective > protective)) or
                (direction == "short" and (protective is None or new_protective < protective))
            )

            # 보호선이 더 "보수적"일 때만 교체
            if better_level:
                pos["mss_triggered"]   = True        # 최초·후속 MSS 모두 기록
                pos["protective_level"] = new_protective
                protective              = new_protective

                print(f"[MSS] 보호선 갱신 | {symbol} @ {protective:.4f}")
                send_discord_debug(f"[MSS] 보호선 갱신 | {symbol} @ {protective:.4f}", "aggregated")

            # ─── 보호선 방향·위치 검증 ──────────────────────────────
            #   LONG  → protective > entry  일 때만 유효(이미 BE·익절 구간)
            #   SHORT → protective < entry  일 때만 유효
            invalid_protective = (
                (direction == "long"  and protective <= entry) or
                (direction == "short" and protective >= entry)
            )
            if invalid_protective:
                print(f"[MSS] 보호선 무시: 방향 불일치 | {symbol} "
                    f"(entry={entry:.4f}, protective={protective:.4f})")
                send_discord_debug(
                    f"[MSS] 보호선 무시: 방향 불일치 | {symbol} "
                    f"(entry={entry:.4f}, protective={protective:.4f})",
                    "aggregated",
                )
                # ▸ ❶ 60 초 쿨다운 해시 저장
                pos["_mss_skip_until"] = time.time() + 60
                # ▸ ❷ 보호선·MSS 플래그 초기화
                pos["protective_level"] = None
                pos["mss_triggered"]    = False
                protective              = None
                return                  #   ← 이후 SL 갱신·EARLY-STOP 스킵
                
            # 📌 가격이 이미 보호선에 닿았더라도
            #     ① SL 을 보호선으로 갱신할 수 있으면 갱신
            #     ② 갱신 불가(시장가 ≤ 보호선)면 기존 SL 유지
            #        → Stop-Market 체결로 자연 종료되도록 둔다

            needs_update = self.should_update_sl(symbol, protective)

            # ─── 추가: 보호선-엔트리 거리 최소 0.03 % 보장 ───────────
            min_rr      = MIN_RR_BASE   # 0.5 %
            risk_ratio  = abs(entry - protective) / entry
            if risk_ratio < min_rr:
                print(f"[SL] 보호선 무시: 엔트리와 {risk_ratio:.4%} 격차(≥ {min_rr*100:.2f}% 필요) | {symbol}")
                send_discord_debug(
                    f"[SL] 보호선 무시: 진입가와 {risk_ratio:.4%} 격차 – 기존 SL 유지", "aggregated"
                )
                needs_update = False

            # ────────────────────────────────────────────────────────

            if needs_update:
                # ① 새 SL 주문 먼저 발행
                sl_result = update_stop_loss(symbol, direction, protective)
                if sl_result is not False:           # 성공해야만 교체 진행
                    id_info = f" (ID: {sl_result})"
                    old_id  = pos.get("sl_order_id")   # 기존 주문 기억

                    # 메모리 갱신
                    pos["sl_order_id"] = (
                        sl_result if isinstance(sl_result, int) else None
                    )
                    pos["sl"] = protective

                    # ② 기존 주문 취소 (있으면)
                    if old_id:
                        cancel_order(symbol, old_id)
                        print(f"[SL] 기존 SL 주문 취소됨 | {symbol}")

                    print(f"[SL] 보호선 기반 SL 재설정 완료 | {symbol} @ {protective:.4f}{id_info}")
                    send_discord_debug(f"[SL] 보호선 기반 SL 재설정 완료 | {symbol} @ {protective:.4f}{id_info}", "aggregated")
                else:
                    print(f"[SL] ❌ 보호선 기반 SL 주문 실패 | {symbol}")
                    send_discord_debug(f"[SL] ❌ 보호선 기반 SL 주문 실패 | {symbol}", "aggregated")
                    return

            else:
                # 디버그 노이즈 감소를 위해 half_exit 이후에만 로그
                if half_exit:
                    print(f"[SL] 보호선 SL 갱신 생략: 기존 SL이 더 보수적 | {symbol}")
                # send_discord_debug(f"[SL] 보호선 SL 갱신 생략: 기존 SL이 더 보수적 | {symbol}", "aggregated")

            # ➜ 더 이상 `EARLY STOP` 으로 시장가 종료하지 않음
            #    SL 주문이 새롭게 지정됐거나 기존에 남아 있으므로
            #    Stop-Market 자연 체결을 기다린다.

        # ───────── 손 절 판 정 ──────────────────────────────
        # ① 마크 프라이스 사용
        # ② “한 틱” 이상 뚫린 경우에만 내부-종료
        mark_price = get_mark_price(symbol)

        # → 틱사이즈 확보 (Gate, Binance 모두 대응)
        try:
            from exchange.router import get_tick_size as _tick
            tick = float(_tick(symbol) or 0)
        except Exception:
            tick = 0.0   # 실패 시 0 ⇒ 기존 로직과 동일

        # 내부 종료(Stop-loss) 판정 – 틱 버퍼 1 tick
        if direction == 'long' and mark_price <= sl - tick * SAFETY_TICKS:
            # 실제 포지션이 존재하는지 먼저 확인
            live = get_open_position(symbol)
            if live and abs(live.get("entry", 0)) > 0:
                # 스탑로스 알림 중복 방지 체크 (30초 간격)
                now = time.time()
                last_alert = self._sl_alerts.get(symbol, 0)
                if now - last_alert > 30:  # 30초마다 최대 1번 알림
                    print(f"[STOP LOSS] {symbol} LONG @ mark_price={mark_price:.2f}")
                    send_discord_message(f"[STOP LOSS] {symbol} LONG @ {mark_price:.2f}", "aggregated")
                    self._sl_alerts[symbol] = now
                self.close(symbol)
            else:
                print(f"[DEBUG] {symbol} 스탑로스 조건 충족하지만 포지션 없음 - 캐시 정리")
                self.positions.pop(symbol, None)
                self._cooldowns[symbol] = time.time()
                # 스탑로스 알림 상태도 정리
                self._sl_alerts.pop(symbol, None)

        elif direction == 'short' and mark_price >= sl + tick * SAFETY_TICKS:
            # 실제 포지션이 존재하는지 먼저 확인
            live = get_open_position(symbol)
            if live and abs(live.get("entry", 0)) > 0:
                # 스탑로스 알림 중복 방지 체크 (30초 간격)
                now = time.time()
                last_alert = self._sl_alerts.get(symbol, 0)
                if now - last_alert > 30:  # 30초마다 최대 1번 알림
                    print(f"[STOP LOSS] {symbol} SHORT @ mark_price={mark_price:.2f}")
                    send_discord_message(f"[STOP LOSS] {symbol} SHORT @ {mark_price:.2f}", "aggregated")
                    self._sl_alerts[symbol] = now
                self.close(symbol)
            else:
                print(f"[DEBUG] {symbol} 스탑로스 조건 충족하지만 포지션 없음 - 캐시 정리")
                self.positions.pop(symbol, None)
                self._cooldowns[symbol] = time.time()
                # 스탑로스 알림 상태도 정리
                self._sl_alerts.pop(symbol, None)

        # 절반 익절 이후 보호선 이탈 체크
        elif half_exit and protective:
            if direction == 'long' and current_price <= protective:
                print(f"[FINAL EXIT] {symbol} LONG 보호선 이탈 → 잔여 종료")
                send_discord_message(f"[FINAL EXIT] {symbol} LONG 보호선 이탈 → 잔여 종료", "aggregated")
                send_discord_debug(f"[DEBUG] {symbol} LONG 보호선 이탈로 포지션 완전 종료", "aggregated")
                self.close(symbol)

            elif direction == 'short' and current_price >= protective:
                print(f"[FINAL EXIT] {symbol} SHORT 보호선 이탈 → 잔여 종료")
                send_discord_message(f"[FINAL EXIT] {symbol} SHORT 보호선 이탈 → 잔여 종료", "aggregated")
                send_discord_debug(f"[DEBUG] {symbol} SHORT 보호선 이탈로 포지션 완전 종료", "aggregated")
                self.close(symbol)

    def close(self, symbol: str, exit_price: float | None = None):
        """
        * 여러 곳에서 동시에 호출돼도 안전하도록 idempotent 처리
        * pop() 을 한 번만 호출해 KeyError 방지
        """
        # ▸ SL이 이미 트리거돼 포지션이 0 인 경우 MARKET 청산·취소 생략
        from exchange.router import get_open_position
        live = get_open_position(symbol)
        if not live or abs(live.get("entry", 0)) == 0:
            print(f"[INFO] {symbol} SL 이미 소멸 → MARKET 청산 생략")
            # 내부 포지션만 제거하고 쿨-다운
            pos = self.positions.pop(symbol, None)
            self._cooldowns[symbol] = time.time()
            return
        
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return
        
        # ① 시장가 포지션 청산 시도
        try:
            close_position_market(symbol)           # 실패 시 RuntimeError

            # ② 청산 후 포지션이 0 인지 재확인
            from exchange.router import get_open_position
            still_live = get_open_position(symbol)
            if still_live and abs(still_live.get("entry", 0)) > 0:
                raise RuntimeError("position not closed")

            print(f"[EXIT] {symbol} 시장가 청산 완료")
            send_discord_debug(f"[EXIT] {symbol} 시장가 청산 완료", "aggregated")

            # ③ **확실히 닫힌 뒤** SL 주문 취소
            sl_order_id = pos.get("sl_order_id")
            if sl_order_id:
                cancel_order(symbol, sl_order_id)

        except Exception as e:
            # 실패 시 SL 그대로 둬야 하므로 취소하지 않는다
            print(f"[WARN] {symbol} 시장가 청산 실패 → {e}")
            send_discord_debug(f"[WARN] {symbol} 시장가 청산 실패 → {e}", "aggregated")
            return   # 헷지 유지 후 재시도 기회

        if exit_price is None:
            exit_price = pos.get("last_price", pos["entry"])

        from datetime import datetime, timezone
        on_exit(symbol, exit_price, datetime.now(timezone.utc))

        # ▸ 쿨-다운 시작
        self._cooldowns[symbol] = time.time()
        # ▸ 스탑로스 알림 상태 정리
        self._sl_alerts.pop(symbol, None)

    def init_position(self, symbol: str, direction: str, entry: float, sl: float, tp: float):
        self.positions[symbol] = {
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "last_price": entry,          # ← 한 번 넣어두면 KeyError 방지
            "half_exit": False,
            "protective_level": None,
            "mss_triggered": False
        }
    
    def should_update_sl(self, symbol: str, new_sl: float) -> bool:
        if symbol not in self.positions:
            return False
        current_sl = self.positions[symbol]['sl']
        direction = self.positions[symbol]['direction']
        if direction == 'long':
            # 롱 ➜ 새 SL 이 더 높아야 보수적
            return new_sl > current_sl
        else:  # short
            # 기본: 더 낮게 ↓, 또는 entry 와의 Risk 가 줄어들면 ↑ 허용
            entry      = self.positions[symbol]["entry"]
            risk_now   = abs(entry - current_sl)
            risk_new   = abs(entry - new_sl)
            return (new_sl < current_sl) or (risk_new < risk_now)
        
    def try_update_trailing_sl(
        self,
        symbol: str,
        current_price: float,
        threshold_pct: float = TRAILING_THRESHOLD_PCT,   # default 0.8 %
    ):
        if symbol not in self.positions:
            return

        pos = self.positions[symbol]
        # ① 1차 익절(half_exit) 전이면 트레일링 SL 비활성
        if not pos.get("half_exit"):
            return
        # ② half_exit 후라도 *진입 30 초 이내* 는 무시 (급격한 노이즈 방어)
        if time.time() - pos.get("_created", 0) < 30:
            return
        direction = pos['direction']
        current_sl = pos['sl']
        protective  = pos.get("protective_level")

        # 절반 익절 이후에도 계속 SL 추적
        # (보호선이 있으면 둘 중 더 보수적인 가격만 채택)

        # ▸ tickSize 먼저 확보 -------------------------------------
        from exchange.router import get_tick_size as _tick
        tick = float(_tick(symbol) or 0)

        # ─── 최소 거리(리스크-가드) 확보 ────────────────────────────
        #   max(0.03 %,   tickSize / entry × 3)
        entry     = pos["entry"]
        tick_rr   = (tick / entry) if (tick and entry) else 0
        min_rr    = max(MIN_RR_BASE, tick_rr * 3)

        if direction == "long":
            # 기본 퍼센트 트레일링
            percent_trailing = current_price * (1 - threshold_pct)
            
            # 실시간 스윙 저점 계산
            swing_low = None
            try:
                # LTF 데이터에서 스윙 저점 찾기
                from core.data_feed import get_cached_data
                ltf_df = get_cached_data(symbol, "1m")
                if ltf_df is not None and len(ltf_df) > 10:
                    swing_data = get_ltf_protective(ltf_df, direction, lookback=20, span=2)
                    if swing_data:
                        swing_low = swing_data["protective_level"]
            except Exception as e:
                print(f"[SWING] {symbol} 스윙 저점 계산 실패: {e}")
            
            # 하이브리드 트레일링: 스윙 저점과 퍼센트 트레일링 중 더 보수적인 값
            if swing_low and swing_low > percent_trailing:
                new_sl = swing_low
                print(f"[HYBRID] {symbol} 스윙 저점 기준 트레일링: {swing_low:.4f}")
            else:
                new_sl = percent_trailing
                print(f"[HYBRID] {symbol} 퍼센트 기준 트레일링: {percent_trailing:.4f}")
            
            if (
                (new_sl - current_sl) > tick * 2                    # 최소 2 tick 위
                and self.should_update_sl(symbol, new_sl)
                and (entry := pos["entry"])
                and abs(entry - new_sl) / entry >= min_rr
                and (protective is None or new_sl > protective)
            ):
                old_sl, old_tp = pos["sl"], pos["tp"]   # ▸ rollback 저장

                pos["sl"] = new_sl                      # ① 선(先)-메모리 갱신
                # 두 번째 쓰레드는 여기서 diff<=2tick 조건에 걸려 바로 return

                sl_result = update_stop_loss(symbol, direction, new_sl)
                if sl_result is not False:
                    pos['sl_order_id'] = (
                        sl_result if isinstance(sl_result, int) else None
                    )
                    # 📌 1차 익절 이후에는 TP 를 새로 만들지 않는다
                    #     잔여 물량은 트레일링 SL 로만 관리
                    print(f"[TRAILING SL] {symbol} LONG SL 갱신: {current_sl:.4f} → {new_sl:.4f}")
                    send_discord_debug(f"[TRAILING SL] {symbol} LONG SL 갱신: {current_sl:.4f} → {new_sl:.4f}", "aggregated")

                else:                       # ★ API 실패 → 값 원복
                    pos["sl"], pos["tp"] = old_sl, old_tp
                    return                  # 중복 갱신도 방지
                
        elif direction == "short":
            # 숏 → “위쪽” = 현재가 + 1 % 
            # 기본 퍼센트 트레일링
            percent_trailing = current_price * (1 + threshold_pct)
            
            # 실시간 스윙 고점 계산
            swing_high = None
            try:
                # LTF 데이터에서 스윙 고점 찾기
                from core.data_feed import get_cached_data
                ltf_df = get_cached_data(symbol, "1m")
                if ltf_df is not None and len(ltf_df) > 10:
                    swing_data = get_ltf_protective(ltf_df, direction, lookback=20, span=2)
                    if swing_data:
                        swing_high = swing_data["protective_level"]
            except Exception as e:
                print(f"[SWING] {symbol} 스윙 고점 계산 실패: {e}")
            
            # 하이브리드 트레일링: 스윙 고점과 퍼센트 트레일링 중 더 보수적인 값
            if swing_high and swing_high < percent_trailing:
                new_sl = swing_high
                print(f"[HYBRID] {symbol} 스윙 고점 기준 트레일링: {swing_high:.4f}")
            else:
                new_sl = percent_trailing
                print(f"[HYBRID] {symbol} 퍼센트 기준 트레일링: {percent_trailing:.4f}")
            if (
                (current_sl - new_sl) > tick * 2                 # 최소 2 tick 아래
                and self.should_update_sl(symbol, new_sl)
                and (entry := pos["entry"])
                and abs(entry - new_sl) / entry >= min_rr
                and (protective is None or new_sl < protective)   # 보호선보다 위험하지 않게
            ):
                old_sl, old_tp = pos["sl"], pos["tp"]   # ▸ rollback 저장

                pos["sl"] = new_sl                      # ① 선(先)-메모리 갱신
                # 두 번째 쓰레드는 여기서 diff<=2tick 조건에 걸려 바로 return

                sl_result = update_stop_loss(symbol, direction, new_sl)
                if sl_result is not False:
                    pos['sl_order_id'] = (
                        sl_result if isinstance(sl_result, int) else None
                    )
                    # 📌 1차 익절 이후에는 TP 를 새로 만들지 않는다

                    print(f"[TRAILING SL] {symbol} SHORT SL 갱신: {current_sl:.4f} → {new_sl:.4f}")
                    send_discord_debug(f"[TRAILING SL] {symbol} SHORT SL 갱신: {current_sl:.4f} → {new_sl:.4f}", "aggregated")

                else:                       # ★ API 실패 → 값 원복
                    pos["sl"], pos["tp"] = old_sl, old_tp
                    return
    def dump(self, sym=None):
        import json, pprint, datetime
        now = datetime.datetime.utcnow().isoformat(timespec="seconds")
        data = self.positions if sym is None else {sym: self.positions.get(sym, {})}
        pprint.pp({ "ts": now, **data })

    def _verify_stop_losses(self):
        """
        모든 포지션의 SL 주문 존재 여부를 주기적으로 검증
        기본 구현 - 확장 클래스에서 오버라이드 가능
        """
        # 기본 구현에서는 아무것도 하지 않음 (안전한 기본값)
        pass


# Global cache for entry messages
_ENTRY_CACHE: dict[str, str] = {}    # {symbol: 마지막 전송 메시지}


class PositionManagerExtended(PositionManager):
    def _verify_stop_losses(self):
        """
        모든 포지션의 SL 주문 존재 여부를 주기적으로 검증
        확장된 구현 - 실제 SL 검증 수행
        """
        if not self.positions:
            return
            
        try:
            from exchange.router import GATE_SET
            
            # 딕셔너리 순회 중 수정 방지를 위해 복사본 사용
            positions_copy = dict(self.positions)
            
            for symbol, pos in positions_copy.items():
                sl_price = pos.get('sl')
                if not sl_price:
                    continue
                    
                # 거래소별 SL 검증
                if symbol not in GATE_SET:
                    # Binance 심볼 검증
                    try:
                        from exchange.binance_api import verify_sl_exists, ensure_stop_loss
                        if not verify_sl_exists(symbol, sl_price):
                            print(f"[WARN] {symbol} Binance SL 주문 누락 감지 - 재생성 시도")
                            send_discord_debug(f"[WARN] {symbol} Binance SL 주문 누락 감지", "aggregated")
                            
                            # SL 재생성 시도
                            direction = pos.get('direction')
                            if direction:
                                success = ensure_stop_loss(symbol, direction, sl_price, max_retries=2)
                                if not success:
                                    send_discord_debug(f"[CRITICAL] {symbol} Binance SL 재생성 실패!", "aggregated")
                                     
                    except Exception as e:
                        print(f"[ERROR] {symbol} Binance SL 검증 중 오류: {e}")
                else:
                    # Gate 심볼 검증
                    try:
                        from exchange.gate_sdk import verify_sl_exists_gate, ensure_stop_loss_gate
                        if not verify_sl_exists_gate(symbol, sl_price):
                            print(f"[WARN] {symbol} Gate SL 주문 누락 감지 - 재생성 시도")
                            send_discord_debug(f"[WARN] {symbol} Gate SL 주문 누락 감지", "aggregated")
                            
                            # SL 재생성 시도
                            direction = pos.get('direction')
                            if direction:
                                success = ensure_stop_loss_gate(symbol, direction, sl_price, max_retries=2)
                                if not success:
                                    send_discord_debug(f"[CRITICAL] {symbol} Gate SL 재생성 실패!", "aggregated")
                                    
                    except Exception as e:
                        print(f"[ERROR] {symbol} Gate SL 검증 중 오류: {e}")
                        
        except Exception as e:
            print(f"[ERROR] SL 검증 프로세스 오류: {e}")

    def force_ensure_all_stop_losses(self):
        """
        모든 포지션의 SL을 강제로 확인하고 누락된 경우 재생성
        수동 호출용 메서드
        """
        if not self.positions:
            print("[INFO] 활성 포지션이 없습니다.")
            return
            
        print("[INFO] 모든 포지션의 SL 검증을 시작합니다...")
        
        try:
            from exchange.router import GATE_SET
            
            for symbol, pos in self.positions.items():
                sl_price = pos.get('sl')
                direction = pos.get('direction')
                
                if not sl_price or not direction:
                    print(f"[WARN] {symbol} 포지션 정보 불완전 - 건너뜀")
                    continue
                    
                print(f"[CHECK] {symbol} SL 검증 중...")
                
                if symbol not in GATE_SET:
                    # Binance 심볼
                    try:
                        from exchange.binance_api import verify_sl_exists, ensure_stop_loss
                        if verify_sl_exists(symbol, sl_price):
                            print(f"[OK] {symbol} Binance SL 주문 존재 확인 @ {sl_price:.4f}")
                        else:
                            print(f"[FIXING] {symbol} Binance SL 주문 누락 - 재생성 중...")
                            success = ensure_stop_loss(symbol, direction, sl_price, max_retries=3)
                            if success:
                                print(f"[FIXED] {symbol} Binance SL 주문 재생성 완료")
                                send_discord_debug(f"[FIXED] {symbol} Binance SL 주문 재생성 완료", "aggregated")
                            else:
                                print(f"[FAILED] {symbol} Binance SL 주문 재생성 실패")
                                send_discord_debug(f"[FAILED] {symbol} Binance SL 주문 재생성 실패", "aggregated")
                    except Exception as e:
                        print(f"[ERROR] {symbol} Binance SL 처리 중 오류: {e}")
                else:
                    # Gate 심볼
                    try:
                        from exchange.gate_sdk import verify_sl_exists_gate, ensure_stop_loss_gate
                        if verify_sl_exists_gate(symbol, sl_price):
                            print(f"[OK] {symbol} Gate SL 주문 존재 확인 @ {sl_price:.4f}")
                        else:
                            print(f"[FIXING] {symbol} Gate SL 주문 누락 - 재생성 중...")
                            success = ensure_stop_loss_gate(symbol, direction, sl_price, max_retries=3)
                            if success:
                                print(f"[FIXED] {symbol} Gate SL 주문 재생성 완료")
                                send_discord_debug(f"[FIXED] {symbol} Gate SL 주문 재생성 완료", "aggregated")
                            else:
                                print(f"[FAILED] {symbol} Gate SL 주문 재생성 실패")
                                send_discord_debug(f"[FAILED] {symbol} Gate SL 주문 재생성 실패", "aggregated")
                    except Exception as e:
                        print(f"[ERROR] {symbol} Gate SL 처리 중 오류: {e}")
                    
        except Exception as e:
            print(f"[ERROR] 강제 SL 검증 중 오류: {e}")
            
        print("[INFO] SL 검증 완료")
