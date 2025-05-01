import os
import requests
from dotenv import load_dotenv

load_dotenv()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

def send_discord_alert(message: str):
    if not DISCORD_WEBHOOK_URL:
        print("[DISCORD] 웹훅 URL 없음.")
        return
    try:
        data = {"content": message}
        response = requests.post(DISCORD_WEBHOOK_URL, json=data)
        if response.status_code not in (200, 204):
            print(f"[DISCORD ERROR] {response.status_code}: {response.text}")
    except Exception as e:
        print(f"[DISCORD ERROR] 전송 실패: {e}")
