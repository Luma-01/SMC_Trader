# core/position.py

import time
from typing import Dict, Optional
from core.mss import get_mss_and_protective_low
from core.monitor import on_entry, on_exit     # ★ 추가
from exchange.binance_api import get_mark_price  # ★ 마크 가격 조회
from notify.discord import send_discord_message, send_discord_debug
from exchange.router import (
    update_stop_loss,
    update_take_profit,      # ★ NEW
    cancel_order,
    close_position_market,
)
from core.data_feed import ensure_stream
from config.settings import RR

class PositionManager:
    def __init__(self):
        self.positions: Dict[str, Dict] = {}
        # ▸ 마지막 종료 시각 저장  {symbol: epoch sec}
        self._cooldowns: Dict[str, float] = {}

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

    def update_price(self, symbol: str, current_price: float, ltf_df=None):
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

        # (수정) 절반 익절 전이라면 무조건 트레일링-SL 체크
        # └ MSS 가 이미 발생했더라도 protective level 을 범하지 않게 내부에서 제어
        if not half_exit:
            self.try_update_trailing_sl(symbol, current_price)

        # MSS 먼저 발생했는지 확인
        if not mss_triggered and ltf_df is not None:
            mss_data = get_mss_and_protective_low(ltf_df, direction)
            if mss_data:
                pos["mss_triggered"]   = True
                protective            = mss_data["protective_level"]
                pos["protective_level"] = protective
                print(f"[MSS] 보호선 설정됨 | {symbol} @ {protective:.4f}")
                send_discord_debug(f"[MSS] 보호선 설정됨 | {symbol} @ {protective:.4f}", "aggregated")

                # 보호선 도달 여부 먼저 체크
                if ((direction == 'long' and current_price <= protective) or
                    (direction == 'short' and current_price >= protective)):
                    print(f"[MSS EARLY STOP] {symbol} 보호선 도달 → SL 갱신 전 종료")
                    send_discord_message(f"[MSS EARLY STOP] {symbol} 보호선 도달 → SL 갱신 전 종료", "aggregated")
                    self.close(symbol)
                    return

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
                    # 기존 SL 주문 먼저 취소
                    if pos.get("sl_order_id"):
                        cancel_order(symbol, pos["sl_order_id"])
                        print(f"[SL] 기존 SL 주문 취소됨 | {symbol}")
                        send_discord_debug(f"[SL] 기존 SL 주문 취소됨 | {symbol}", "aggregated")

                    sl_result = update_stop_loss(symbol, direction, protective)
                    if sl_result is not False:      # 성공 여부만 판단
                        id_info = f" (ID: {sl_result})"
                        pos["sl_order_id"] = (
                            sl_result if isinstance(sl_result, int) else None
                        )
                        pos["sl"] = protective
                        print(f"[SL] 보호선 기반 SL 재설정 완료 | {symbol} @ {protective:.4f}{id_info}")
                        send_discord_debug(f"[SL] 보호선 기반 SL 재설정 완료 | {symbol} @ {protective:.4f}{id_info}", "aggregated")
                    else:
                        print(f"[SL] ❌ 보호선 기반 SL 주문 실패 | {symbol}")
                        send_discord_debug(f"[SL] ❌ 보호선 기반 SL 주문 실패 | {symbol}", "aggregated")
                        return

                else:
                    print(f"[SL] 보호선 SL 갱신 생략: 기존 SL이 더 보수적 | {symbol}")
                    send_discord_debug(f"[SL] 보호선 SL 갱신 생략: 기존 SL이 더 보수적 | {symbol}", "aggregated")

                # MSS 먼저 발생했을 경우 → 즉시 전체 종료
                if ((direction == 'long' and current_price <= protective) or
                    (direction == 'short' and current_price >= protective)):
                    print(f"[MSS EARLY STOP] {symbol} 보호선 이탈 → 전체 종료")
                    send_discord_message(f"[MSS EARLY STOP] {symbol} 보호선 이탈 → 전체 종료", "aggregated")
                    self.close(symbol)
                    return

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

        # 절반 익절 (1:2 도달) — 이 부분은 종전대로 current_price 기준 유지
        elif not half_exit:
            if direction == 'long' and current_price >= tp:
                print(f"[PARTIAL TP] {symbol} LONG 절반 익절 @ {current_price:.2f}")
                send_discord_message(f"[PARTIAL TP] {symbol} LONG 절반 익절 @ {current_price:.2f}", "aggregated")
                send_discord_debug(f"[DEBUG] {symbol} LONG 1차 익절 완료", "aggregated")
                pos['half_exit'] = True

            elif direction == 'short' and current_price <= tp:
                print(f"[PARTIAL TP] {symbol} SHORT 절반 익절 @ {current_price:.2f}")
                send_discord_message(f"[PARTIAL TP] {symbol} SHORT 절반 익절 @ {current_price:.2f}", "aggregated")
                send_discord_debug(f"[DEBUG] {symbol} SHORT 1차 익절 완료", "aggregated")
                pos['half_exit'] = True

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
        pos = self.positions.pop(symbol, None)     # ⬅️ 이미 지워졌다면 None
        if pos is None:
            return

        # SL 주문 취소
        sl_order_id = pos.get("sl_order_id")
        if sl_order_id:
            ok = cancel_order(symbol, sl_order_id)
            if ok is False:                     # -2011 = 이미 체결·삭제
                print(f"[INFO] {symbol} SL 이미 소멸 → MARKET 청산 생략")
                self._cooldowns[symbol] = time.time()
                return                          # ★ 조기 리턴

        # ────────────────────────────────
        # 1) 실거래소 포지션 시장가 청산
        # ────────────────────────────────
        try:
            close_position_market(symbol)                # ← ★ 핵심 한 줄
            print(f"[EXIT] {symbol} 시장가 청산 요청 완료")
            send_discord_debug(f"[EXIT] {symbol} 시장가 청산 완료", "aggregated")
        except Exception as e:
            print(f"[WARN] {symbol} 시장가 청산 실패 → {e}")
            send_discord_debug(f"[WARN] {symbol} 시장가 청산 실패 → {e}", "aggregated")

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
            # ㊟ 숏 포지션은 “가격을 내려서(작게 만들어서)” SL 을 끌어 올립니다
            return new_sl < current_sl
        
    def try_update_trailing_sl(self, symbol: str, current_price: float, threshold_pct: float = 0.01):
        if symbol not in self.positions:
            return

        pos = self.positions[symbol]
        direction = pos['direction']
        current_sl = pos['sl']
        protective  = pos.get("protective_level")

        # 절반 익절 이후에도 계속 SL 추적
        # (보호선이 있으면 둘 중 더 보수적인 가격만 채택)

        # ─── 최소 거리(리스크-가드) 확보 ───
        min_rr = 0.0003                      # 0.03 %
        if direction == "long":
            new_sl = current_price * (1 - threshold_pct)
            if (new_sl > current_sl
                and self.should_update_sl(symbol, new_sl)
                and (entry := pos['entry'])            # grab once
                and abs(entry - new_sl) / entry >= min_rr
                and (protective is None or new_sl > protective)):
                sl_result = update_stop_loss(symbol, direction, new_sl)
                if sl_result is not False:                           # Gate=True 허용
                    pos['sl'] = new_sl
                    pos['sl_order_id'] = (
                        sl_result if isinstance(sl_result, int) else None
                    )
                    # ── TP 동시 갱신 ───────────────────────────────
                    risk      = abs(pos['entry'] - new_sl)
                    new_tp    = (pos['entry'] + risk * RR
                                  if direction == "long"
                                  else pos['entry'] - risk * RR)
                    if update_take_profit(symbol, direction, new_tp) is not False:
                        pos['tp'] = float(new_tp)
                    print(f"[TRAILING SL] {symbol} LONG SL 갱신: {current_sl:.4f} → {new_sl:.4f}")
                    send_discord_debug(f"[TRAILING SL] {symbol} LONG SL 갱신: {current_sl:.4f} → {new_sl:.4f}", "aggregated")

        elif direction == "short":
            # 숏 → “위쪽” = 현재가 + 1 % 
            new_sl = current_price * (1 + threshold_pct)
            if (new_sl < current_sl
                and self.should_update_sl(symbol, new_sl)
                and (entry := pos['entry'])
                and abs(entry - new_sl) / entry >= min_rr
                and (protective is None or new_sl < protective)):   # 보호선보다 위험하지 않게
                sl_result = update_stop_loss(symbol, direction, new_sl)
                if sl_result is not False:                           # Gate=True 허용
                    pos['sl'] = new_sl
                    pos['sl_order_id'] = (
                        sl_result if isinstance(sl_result, int) else None
                    )
                    # ── TP 동시 갱신 ───────────────────────────────
                    risk      = abs(pos['entry'] - new_sl)
                    new_tp    = (pos['entry'] + risk * RR
                                  if direction == "long"
                                  else pos['entry'] - risk * RR)
                    if update_take_profit(symbol, direction, new_tp) is not False:
                        pos['tp'] = float(new_tp)

                    print(f"[TRAILING SL] {symbol} SHORT SL 갱신: {current_sl:.4f} → {new_sl:.4f}")
                    send_discord_debug(f"[TRAILING SL] {symbol} SHORT SL 갱신: {current_sl:.4f} → {new_sl:.4f}", "aggregated")

_ENTRY_CACHE: dict[str, str] = {}    # {symbol: 마지막 전송 메시지}
