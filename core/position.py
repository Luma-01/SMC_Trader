# core/position.py

import time
from typing import Dict, Optional

# ── pandas 타입 힌트/연산에 사용 ──────────────────────
import pandas as pd

from core.protective import (
    get_ltf_protective,
    get_protective_level,      # ← MTF(5 m) 보호선
)
from config.settings import RR, USE_HTF_PROTECTIVE   # ⬅︎ 스위치 import
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

class PositionManager:
    def __init__(self):
        self.positions: Dict[str, Dict] = {}
        # ▸ 마지막 종료 시각 저장  {symbol: epoch sec}
        self._cooldowns: Dict[str, float] = {}

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

    # basis: “OB 2800~2850”, “BB_HTF 1.25~1.30” … 등 진입 근거 문자열
    def enter(
        self,
        symbol: str,
        direction: str,
        entry: float,
        sl: float,
        tp: float,
        basis: str | None = None,          # ★ NEW
    ):
        ensure_stream(symbol)
        self.positions[symbol] = {
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "last_price": entry,          # ← 한 번 넣어두면 KeyError 방지
            "half_exit": False,
            "protective_level": None,
            "mss_triggered": False,
            "sl_order_id": None
        }
        on_entry(symbol, direction, entry, sl, tp)   # ★ 호출

        # 진입 시 SL 주문 생성
        sl_result = update_stop_loss(symbol, direction, sl)
        # Binance ➜ order-id(int), Gate ➜ True  →  둘 다 “성공”으로 처리
        if sl_result is not False:
            self.positions[symbol]['sl_order_id'] = (
                sl_result if isinstance(sl_result, int) else None
            )
            self.positions[symbol]['sl'] = sl
            print(f"[SL] 초기 SL 주문 등록 완료 | {symbol} @ {sl:.4f}")
            send_discord_debug(f"[SL] 초기 SL 주문 등록 완료 | {symbol} @ {sl:.4f}", "aggregated")

        # ────────── 메시지 구성 ──────────
        basis_txt = f" | {basis}" if basis else ""
        msg = (
            f"[ENTRY] {symbol} | {direction.upper()} @ {entry:.4f} | "
            f"SL: {sl:.4f} | TP: {tp:.4f}{basis_txt}"
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
        htf5_df: Optional[pd.DataFrame] = None,
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
        #      ‘TP 상승→즉시 익절’ 오류를 방지할 수 있다
        # ───────────────────────────────────────────────
        if not half_exit:
            if direction == "long" and current_price >= pos["tp"]:
                print(f"[PARTIAL TP] {symbol} LONG 절반 익절 @ {current_price:.2f}")
                send_discord_message(f"[PARTIAL TP] {symbol} LONG 절반 익절 @ {current_price:.2f}", "aggregated")
                send_discord_debug(f"[DEBUG] {symbol} LONG 1차 익절 완료", "aggregated")
                pos["half_exit"] = True

            elif direction == "short" and current_price <= pos["tp"]:
                print(f"[PARTIAL TP] {symbol} SHORT 절반 익절 @ {current_price:.2f}")
                send_discord_message(f"[PARTIAL TP] {symbol} SHORT 절반 익절 @ {current_price:.2f}", "aggregated")
                send_discord_debug(f"[DEBUG] {symbol} SHORT 1차 익절 완료", "aggregated")
                pos["half_exit"] = True

        # ───────────────────────────────────────────────
        # ❷ SL/TP 는 **절반 익절 후에도** 계속 추적
        self.try_update_trailing_sl(symbol, current_price)

        # ───────── LTF(1 m) (+ 선택적 HTF 5 m) 보호선 후보 ────────
        candidates = []
        if ltf_df is not None:
            p = get_ltf_protective(ltf_df, direction)
            if p:
                candidates.append(p["protective_level"])
        # ➋ 스위치: 5 m 보호선 사용 여부
        if USE_HTF_PROTECTIVE and htf5_df is not None:
            # 최근 1 시간(5 m×12) 내 스윙
            p = get_protective_level(htf5_df, direction, lookback=12, span=2)
            if p:
                candidates.append(p["protective_level"])

            if candidates:
                new_protective = max(candidates) if direction == "long" else min(candidates)
                better_level   = (
                    (direction == "long"  and (protective is None or new_protective > protective)) or
                    (direction == "short" and (protective is None or new_protective < protective))
                )

                # 보호선이 더 “보수적”일 때만 교체
                if better_level:
                    pos["mss_triggered"]   = True        # 최초·후속 MSS 모두 기록
                    pos["protective_level"] = new_protective
                    protective              = new_protective

                    print(f"[MSS] 보호선 갱신 | {symbol} @ {protective:.4f}")
                    send_discord_debug(f"[MSS] 보호선 갱신 | {symbol} @ {protective:.4f}", "aggregated")

                # ─── 보호선 방향·위치 검증 ──────────────────────────────
                #   LONG  → protective < entry  (저점)
                #   SHORT → protective > entry  (고점)
                invalid_protective = (
                    (direction == "long"  and protective >= entry) or
                    (direction == "short" and protective <= entry)
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
                min_rr      = 0.0003       # 0.03 %
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
            tick = _tick(symbol)
        except Exception:
            tick = 0     # 실패 시 0 ⇒ 기존 로직과 동일

        SAFETY_TICKS = 2                 # 2 틱 이상 벗어나야 내부 종료

        if direction == 'long' and mark_price <= sl - tick * SAFETY_TICKS:
            print(f"[STOP LOSS] {symbol} LONG @ mark_price={mark_price:.2f}")
            send_discord_message(f"[STOP LOSS] {symbol} LONG @ {mark_price:.2f}", "aggregated")
            self.close(symbol)

        elif direction == 'short' and mark_price >= sl + tick * SAFETY_TICKS:
            print(f"[STOP LOSS] {symbol} SHORT @ mark_price={mark_price:.2f}")
            send_discord_message(f"[STOP LOSS] {symbol} SHORT @ {mark_price:.2f}", "aggregated")
            self.close(symbol)

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
        
    def try_update_trailing_sl(self, symbol: str, current_price: float, threshold_pct: float = 0.01):
        if symbol not in self.positions:
            return

        pos = self.positions[symbol]
        direction = pos['direction']
        current_sl = pos['sl']
        protective  = pos.get("protective_level")

        # 절반 익절 이후에도 계속 SL 추적
        # (보호선이 있으면 둘 중 더 보수적인 가격만 채택)

        # ▸ tickSize 먼저 확보 -------------------------------------
        try:
            from exchange.router import get_tick_size as _tick
            tick = _tick(symbol) or 0
        except Exception:
            tick = 0

        # ─── 최소 거리(리스크-가드) 확보 ────────────────────────────
        #   max(0.03 %,   tickSize / entry × 3)
        entry     = pos["entry"]
        tick_rr   = (float(tick) / entry) if (tick and entry) else 0
        min_rr    = max(0.0003, tick_rr * 3)

        if direction == "long":
            new_sl = current_price * (1 - threshold_pct)
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
                    # ── TP 동시 갱신 ───────────────────────────────
                    risk      = abs(pos['entry'] - new_sl)
                    new_tp    = (pos['entry'] + risk * RR
                                  if direction == "long"
                                  else pos['entry'] - risk * RR)
                    # TP 도 2 tick 이상 차이날 때만 재발행
                    if abs(new_tp - old_tp) > tick * 2 and \
                       update_take_profit(symbol, direction, new_tp) is not False:
                        pos['tp'] = float(new_tp)
                    print(f"[TRAILING SL] {symbol} LONG SL 갱신: {current_sl:.4f} → {new_sl:.4f}")
                    send_discord_debug(f"[TRAILING SL] {symbol} LONG SL 갱신: {current_sl:.4f} → {new_sl:.4f}", "aggregated")

                else:                       # ★ API 실패 → 값 원복
                    pos["sl"], pos["tp"] = old_sl, old_tp
                    return                  # 중복 갱신도 방지
                
        elif direction == "short":
            # 숏 → “위쪽” = 현재가 + 1 % 
            new_sl = current_price * (1 + threshold_pct)
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
                    # ── TP 동시 갱신 ───────────────────────────────
                    risk      = abs(pos['entry'] - new_sl)
                    new_tp    = (pos['entry'] + risk * RR
                                  if direction == "long"
                                  else pos['entry'] - risk * RR)
                    if abs(new_tp - old_tp) > tick * 2 and \
                       update_take_profit(symbol, direction, new_tp) is not False:
                        pos['tp'] = float(new_tp)

                    print(f"[TRAILING SL] {symbol} SHORT SL 갱신: {current_sl:.4f} → {new_sl:.4f}")
                    send_discord_debug(f"[TRAILING SL] {symbol} SHORT SL 갱신: {current_sl:.4f} → {new_sl:.4f}", "aggregated")

                else:                       # ★ API 실패 → 값 원복
                    pos["sl"], pos["tp"] = old_sl, old_tp
                    return
                
_ENTRY_CACHE: dict[str, str] = {}    # {symbol: 마지막 전송 메시지}
