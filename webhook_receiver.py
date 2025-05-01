from flask import Flask, request, abort
import subprocess
import hmac
import hashlib
import os
from dotenv import load_dotenv
import requests  # 🔔 디스코드 알림용

load_dotenv()

DISCORD_WEBHOOK = os.getenv("SEND_MESSAGE_BINANCE")

app = Flask(__name__)
GITHUB_SECRET = os.getenv("GITHUB_SECRET").encode()

def send_discord_alert(message: str):
    if not DISCORD_WEBHOOK:
        print("❗ 디스코드 Webhook URL이 없습니다.")
        return
    data = {"content": f"🔔 {message}"}
    try:
        requests.post(DISCORD_WEBHOOK, json=data)
    except Exception as e:
        print(f"❗ 디스코드 전송 실패: {e}")

def verify_signature(payload, signature):
    expected = 'sha256=' + hmac.new(GITHUB_SECRET, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)

@app.route('/webhook', methods=['POST'])
def webhook():
    print("✅ Webhook POST 요청 받음")
    
    signature = request.headers.get('X-Hub-Signature-256')
    if signature is None or not verify_signature(request.data, signature):
        print("❌ Signature 검증 실패")
        send_discord_alert("❌ Signature 검증 실패")
        abort(403)
    
    print("✅ Signature OK > git pull 시작")
    subprocess.call(['git', '-C', '/home/ubuntu/SMC_Trader', 'pull'])
    print("✅ git pull 완료 > 응답 전송")
    send_discord_alert("코드 업데이트 완료!")  # repo_name 제거
    return '✅ Verified & Pull done', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
