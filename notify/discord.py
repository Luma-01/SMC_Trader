#test_position_flow.py

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

def _send_discord(message: str, category: str, exchange: str):
    key = f"{exchange}_{category}"
    url = WEBHOOKS.get(key)
    if not url:
        print(f"[DISCORD] ❌ 웹훅 URL 없음: {key}")
        return
    try:
        data = {"content": message}
        response = requests.post(url, json=data)
        if response.status_code not in (200, 204):
            print(f"[DISCORD] ❌ 응답 오류 {response.status_code} → {response.text}")
    except Exception as e:
        print(f"[DISCORD] ❌ 전송 실패 → {e}")

def send_discord_debug(message: str, exchange: str = "aggregated"):
    _send_discord(message, "debug", exchange)

def send_discord_message(message: str, exchange: str = "aggregated"):
    _send_discord(message, "message", exchange)

def send_discord_file(file_path: str, channel: str = "aggregated"):
    """이미지·CSV 등을 Discord로 전송"""
    url = WEBHOOKS.get(channel)
    if not url:
        return
    with open(file_path, "rb") as fp:
        requests.post(url, files={"file": fp})