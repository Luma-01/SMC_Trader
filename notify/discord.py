import os
import requests
from dotenv import load_dotenv

load_dotenv()

WEBHOOKS = {
    "binance_debug": os.getenv("SEND_DEBUG_BINANCE"),
    "binance_message": os.getenv("SEND_MESSAGE_BINANCE"),
    "gateio_debug": os.getenv("SEND_DEBUG_GATE_IO"),
    "gateio_message": os.getenv("SEND_MESSAGE_GATE_IO"),
    "aggregated_debug": os.getenv("SEND_DEBUG_AGGREGATED"),
    "aggregated_message": os.getenv("SEND_MESSAGE_AGGREGATED"),
}

def _send_discord(message: str, target: str):
    url = WEBHOOKS.get(target)
    if not url:
        print(f"[DISCORD] 웹훅 URL 없음: {target}")
        return
    try:
        data = {"content": message}
        response = requests.post(url, json=data)
        if response.status_code not in (200, 204):
            print(f"[DISCORD ERROR] {response.status_code}: {response.text}")
    except Exception as e:
        print(f"[DISCORD ERROR] 전송 실패: {e}")

def send_discord_debug(message: str, exchange: str = "aggregated"):
    _send_discord(message, f"{exchange}_debug")

def send_discord_message(message: str, exchange: str = "aggregated"):
    _send_discord(message, f"{exchange}_message")