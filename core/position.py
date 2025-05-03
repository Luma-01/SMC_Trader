# core/position.py

from typing import Dict, Optional
from core.mss import get_mss_and_protective_low
from notify.discord import send_discord_message, send_discord_debug

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
            "mss_triggered": False
        }
        send_discord_message(
            f"[ENTRY] {symbol} | {direction.upper()} @ {entry:.2f} | SL: {sl:.2f} | TP: {tp:.2f}",
            "aggregated_message"
        )
        send_discord_debug(
            f"[DEBUG] {symbol} 진입 포지션 등록됨", "aggregated"
        )

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
                send_discord_debug(f"[DEBUG] {symbol} MSS 보호선 설정됨", "aggregated")

                # MSS 먼저 발생했을 경우 → 즉시 전체 종료
                if ((direction == 'long' and current_price <= protective) or
                    (direction == 'short' and current_price >= protective)):
                    send_discord_message(f"[MSS EARLY STOP] {symbol} 보호선 이탈 → 전체 종료", "aggregated")
                    self.close(symbol)
                    return

        # 손절
        if direction == 'long' and current_price <= sl:
            send_discord_message(f"[STOP LOSS] {symbol} LONG @ {current_price:.2f}", "aggregated")
            self.close(symbol)

        elif direction == 'short' and current_price >= sl:
            send_discord_message(f"[STOP LOSS] {symbol} SHORT @ {current_price:.2f}", "aggregated")
            self.close(symbol)

        # 절반 익절 (1:2 도달)
        elif not half_exit:
            if direction == 'long' and current_price >= tp:
                send_discord_message(f"[PARTIAL TP] {symbol} LONG 절반 익절 @ {current_price:.2f}", "aggregated")
                send_discord_debug(f"[DEBUG] {symbol} LONG 1차 익절 완료", "aggregated")
                pos['half_exit'] = True

            elif direction == 'short' and current_price <= tp:
                send_discord_message(f"[PARTIAL TP] {symbol} SHORT 절반 익절 @ {current_price:.2f}", "aggregated")
                send_discord_debug(f"[DEBUG] {symbol} SHORT 1차 익절 완료", "aggregated")
                pos['half_exit'] = True

        # 절반 익절 이후 보호선 이탈 체크
        elif half_exit and protective:
            if direction == 'long' and current_price <= protective:
                send_discord_message(f"[FINAL EXIT] {symbol} LONG 보호선 이탈 → 잔여 종료", "aggregated")
                send_discord_debug(f"[DEBUG] {symbol} LONG 보호선 이탈로 포지션 완전 종료", "aggregated")
                self.close(symbol)

            elif direction == 'short' and current_price >= protective:
                send_discord_message(f"[FINAL EXIT] {symbol} SHORT 보호선 이탈 → 잔여 종료", "aggregated")
                send_discord_debug(f"[DEBUG] {symbol} SHORT 보호선 이탈로 포지션 완전 종료", "aggregated")
                self.close(symbol)

    def close(self, symbol: str):
        if symbol in self.positions:
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