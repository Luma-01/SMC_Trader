from typing import Dict
from notify.discord import send_discord_alert

class PositionManager:
    def __init__(self):
        self.positions: Dict[str, Dict] = {}  # symbol -> {direction, entry, sl, tp}

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions

    def enter(self, symbol: str, direction: str, entry: float, sl: float, tp: float):
        self.positions[symbol] = {
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp
        }
        send_discord_alert(
            f"[ENTRY] {symbol} | {direction.upper()} @ {entry:.2f} | SL: {sl:.2f} | TP: {tp:.2f}"
        )

    def update_price(self, symbol: str, current_price: float):
        if symbol not in self.positions:
            return

        pos = self.positions[symbol]
        direction = pos['direction']
        sl, tp = pos['sl'], pos['tp']

        if direction == 'long':
            if current_price <= sl:
                send_discord_alert(f"[STOP LOSS] {symbol} LONG @ {current_price:.2f}")
                self.close(symbol)
            elif current_price >= tp:
                send_discord_alert(f"[TAKE PROFIT] {symbol} LONG @ {current_price:.2f}")
                self.close(symbol)

        elif direction == 'short':
            if current_price >= sl:
                send_discord_alert(f"[STOP LOSS] {symbol} SHORT @ {current_price:.2f}")
                self.close(symbol)
            elif current_price <= tp:
                send_discord_alert(f"[TAKE PROFIT] {symbol} SHORT @ {current_price:.2f}")
                self.close(symbol)

    def close(self, symbol: str):
        if symbol in self.positions:
            del self.positions[symbol]

    def init_position(self, symbol: str, direction: str, entry: float, sl: float, tp: float):
        # 거래소 보유 포지션 수동 등록 시
        self.positions[symbol] = {
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp
        }
