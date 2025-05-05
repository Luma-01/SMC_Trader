# core/position.py

from typing import Dict, Optional
from core.mss import get_mss_and_protective_low
from notify.discord import send_discord_message, send_discord_debug
from exchange.binance_api import place_stop_loss_order, cancel_order

class PositionManager:
    def __init__(self):
        self.positions: Dict[str, Dict] = {}

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions

    def enter(self, symbol: str, direction: str, entry: float, sl: float, tp: float):
        self.positions[symbol] = {
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "half_exit": False,
            "protective_level": None,
            "mss_triggered": False,
            "sl_order_id": None
        }

        # 진입 시 SL 주문 생성
        order_id = place_stop_loss_order(symbol, direction, sl)
        if order_id:
            self.positions[symbol]['sl_order_id'] = order_id
            print(f"[SL] 초기 SL 주문 등록 완료 | {symbol} @ {sl:.4f}")
            send_discord_debug(f"[SL] 초기 SL 주문 등록 완료 | {symbol} @ {sl:.4f}", "aggregated")

        print(f"[ENTRY] 포지션 등록 완료 | {symbol} → SL: {sl:.4f}, TP: {tp:.4f}")
        send_discord_message(f"[ENTRY] {symbol} | {direction.upper()} @ {entry:.4f} | SL: {sl:.4f} | TP: {tp:.4f}", "aggregated")
        send_discord_debug(f"[ENTRY] 포지션 등록 완료 | {symbol} → SL: {sl:.4f}, TP: {tp:.4f}", "aggregated")

    def update_price(self, symbol: str, current_price: float, ltf_df=None):
        if symbol not in self.positions:
            return

        pos = self.positions[symbol]
        direction = pos['direction']
        sl, tp = pos['sl'], pos['tp']
        entry = pos['entry']
        half_exit = pos['half_exit']
        protective = pos['protective_level']
        mss_triggered = pos['mss_triggered']

        # MSS 먼저 발생했는지 확인
        if not mss_triggered and ltf_df is not None:
            mss_data = get_mss_and_protective_low(ltf_df, direction)
            if mss_data:
                pos['mss_triggered'] = True
                pos['protective_level'] = mss_data['protective_level']
                protective = mss_data['protective_level']
                print(f"[MSS] 보호선 설정됨 | {symbol} @ {protective:.4f}")
                send_discord_debug(f"[MSS] 보호선 설정됨 | {symbol} @ {protective:.4f}", "aggregated")

                if pos.get("sl_order_id"):
                    cancel_order(symbol, pos["sl_order_id"])
                    print(f"[SL] 기존 SL 주문 취소됨 | {symbol}")
                    send_discord_debug(f"[SL] 기존 SL 주문 취소됨 | {symbol}", "aggregated")

                # 보호선 도달 여부 먼저 체크 (주문 전에 종료)
                if ((direction == 'long' and current_price <= protective) or
                    (direction == 'short' and current_price >= protective)):
                    print(f"[MSS EARLY STOP] {symbol} 보호선 도달 → SL 갱신 전 종료")
                    send_discord_message(f"[MSS EARLY STOP] {symbol} 보호선 도달 → SL 갱신 전 종료", "aggregated")
                    self.close(symbol)
                    return

                # 새 SL 주문 설정
                sl_order_id = place_stop_loss_order(symbol, direction, protective)
                if sl_order_id:
                    pos["sl_order_id"] = sl_order_id
                    print(f"[SL] 보호선 기반 SL 재설정 완료 | {symbol} @ {protective:.4f} (ID: {sl_order_id})")
                    send_discord_debug(f"[SL] 보호선 기반 SL 재설정 완료 | {symbol} @ {protective:.4f} (ID: {sl_order_id})", "aggregated")
                else:
                    print(f"[SL] ❌ 보호선 기반 SL 주문 실패 | {symbol}")
                    send_discord_debug(f"[SL] ❌ 보호선 기반 SL 주문 실패 | {symbol}", "aggregated")

                # MSS 먼저 발생했을 경우 → 즉시 전체 종료
                if ((direction == 'long' and current_price <= protective) or
                    (direction == 'short' and current_price >= protective)):
                    print(f"[MSS EARLY STOP] {symbol} 보호선 이탈 → 전체 종료")
                    send_discord_message(f"[MSS EARLY STOP] {symbol} 보호선 이탈 → 전체 종료", "aggregated")
                    self.close(symbol)
                    return

        # 손절
        if direction == 'long' and current_price <= sl:
            print(f"[STOP LOSS] {symbol} LONG @ {current_price:.2f}")
            send_discord_message(f"[STOP LOSS] {symbol} LONG @ {current_price:.2f}", "aggregated")
            self.close(symbol)

        elif direction == 'short' and current_price >= sl:
            print(f"[STOP LOSS] {symbol} SHORT @ {current_price:.2f}")
            send_discord_message(f"[STOP LOSS] {symbol} SHORT @ {current_price:.2f}", "aggregated")
            self.close(symbol)

        # 절반 익절 (1:2 도달)
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

    def close(self, symbol: str):
        if symbol in self.positions:
            # SL 주문 취소 시도
            sl_order_id = self.positions[symbol].get("sl_order_id")
            if sl_order_id:
                cancel_order(symbol, sl_order_id)
                print(f"[SL] 종료 전 SL 주문 취소 | {symbol} (ID: {sl_order_id})")
                send_discord_debug(f"[SL] 종료 전 SL 주문 취소 | {symbol} (ID: {sl_order_id})", "aggregated")
            del self.positions[symbol]

    def init_position(self, symbol: str, direction: str, entry: float, sl: float, tp: float):
        self.positions[symbol] = {
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "half_exit": False,
            "protective_level": None,
            "mss_triggered": False
        }

