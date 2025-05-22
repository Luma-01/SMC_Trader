# core/position.py

from typing import Dict, Optional
from core.mss import get_mss_and_protective_low
from core.monitor import on_entry, on_exit     # ★ 추가
from notify.discord import send_discord_message, send_discord_debug
from exchange.router import update_stop_loss, cancel_order

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
        on_entry(symbol, direction, entry, sl, tp)   # ★ 호출

        # 진입 시 SL 주문 생성
        sl_result = update_stop_loss(symbol, direction, sl)
        if isinstance(sl_result, int):  # Binance 전용 ID
            self.positions[symbol]['sl_order_id'] = sl_result
            self.positions[symbol]['sl'] = sl
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

        # 절반 익절 전 && MSS 발생 전 → 트레일링 SL 갱신 시도
        if not half_exit and not mss_triggered:
            self.try_update_trailing_sl(symbol, current_price)

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

                # 보호선 도달 여부 먼저 체크
                if ((direction == 'long' and current_price <= protective) or
                    (direction == 'short' and current_price >= protective)):
                    print(f"[MSS EARLY STOP] {symbol} 보호선 도달 → SL 갱신 전 종료")
                    send_discord_message(f"[MSS EARLY STOP] {symbol} 보호선 도달 → SL 갱신 전 종료", "aggregated")
                    self.close(symbol)
                    return

                if self.should_update_sl(symbol, protective):
                    sl_result = update_stop_loss(symbol, direction, protective)
                    if isinstance(sl_result, int):  # Binance 전용 SL ID
                        id_info = f" (ID: {sl_result})"
                        pos["sl_order_id"] = sl_result
                        pos["sl"] = protective
                        print(f"[SL] 보호선 기반 SL 재설정 완료 | {symbol} @ {protective:.4f}{id_info}")
                        send_discord_debug(f"[SL] 보호선 기반 SL 재설정 완료 | {symbol} @ {protective:.4f}{id_info}", "aggregated")
                    else:
                        print(f"[SL] ❌ 보호선 기반 SL 주문 실패 | {symbol}")
                        send_discord_debug(f"[SL] ❌ 보호선 기반 SL 주문 실패 | {symbol}", "aggregated")
                        return

                else:
                    print(f"[SL] 보호선 SL 갱신 생략: 기존 SL보다 보수적이지 않음 | {symbol}")
                    send_discord_debug(f"[SL] 보호선 SL 갱신 생략: 기존 SL보다 보수적이지 않음 | {symbol}", "aggregated")

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

    def close(self, symbol: str, exit_price: float | None = None):
        if symbol in self.positions:
            # SL 주문 취소 시도
            sl_order_id = self.positions[symbol].get("sl_order_id")
            if sl_order_id:
                cancel_order(symbol, sl_order_id)
                print(f"[SL] 종료 전 SL 주문 취소 | {symbol} (ID: {sl_order_id})")
                send_discord_debug(f"[SL] 종료 전 SL 주문 취소 | {symbol} (ID: {sl_order_id})", "aggregated")
            if exit_price is None:
                exit_price = self.positions[symbol]["entry"]
            del self.positions[symbol]
            on_exit(symbol, exit_price)              # ★ 호출

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
    
    def should_update_sl(self, symbol: str, new_sl: float) -> bool:
        if symbol not in self.positions:
            return False
        current_sl = self.positions[symbol]['sl']
        direction = self.positions[symbol]['direction']
        if direction == 'long':
            return new_sl > current_sl
        else:
            return new_sl < current_sl
        
    def try_update_trailing_sl(self, symbol: str, current_price: float, threshold_pct: float = 0.01):
        if symbol not in self.positions:
            return

        pos = self.positions[symbol]
        direction = pos['direction']
        current_sl = pos['sl']

        # 절반 익절 이후는 보호선 로직에 맡기므로 생략
        if pos.get('half_exit'):
            return

        # 트레일링 SL 계산
        if direction == "long":
            new_sl = current_price * (1 - threshold_pct)
            if new_sl > current_sl and self.should_update_sl(symbol, new_sl):
                sl_result = update_stop_loss(symbol, direction, new_sl)
                if isinstance(sl_result, int):
                    pos['sl'] = new_sl
                    pos['sl_order_id'] = sl_result
                    print(f"[TRAILING SL] {symbol} LONG SL 갱신: {current_sl:.4f} → {new_sl:.4f}")
                    send_discord_debug(f"[TRAILING SL] {symbol} LONG SL 갱신: {current_sl:.4f} → {new_sl:.4f}", "aggregated")

        elif direction == "short":
            new_sl = current_price * (1 + threshold_pct)
            if new_sl < current_sl and self.should_update_sl(symbol, new_sl):
                sl_result = update_stop_loss(symbol, direction, new_sl)
                if isinstance(sl_result, int):
                    pos['sl'] = new_sl
                    pos['sl_order_id'] = sl_result
                    print(f"[TRAILING SL] {symbol} SHORT SL 갱신: {current_sl:.4f} → {new_sl:.4f}")
                    send_discord_debug(f"[TRAILING SL] {symbol} SHORT SL 갱신: {current_sl:.4f} → {new_sl:.4f}", "aggregated")

