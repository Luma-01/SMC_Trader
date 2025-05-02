from flask import Flask, request, abort
import subprocess
import hmac
import hashlib
import os
from dotenv import load_dotenv
import requests  # 🔔 디스코드 알림용

load_dotenv()

DISCORD_WEBHOOK = os.getenv("SEND_MESSAGE_BINANCE")

app = Flask(__name__)
GITHUB_SECRET = os.getenv("GITHUB_SECRET").encode()

def send_discord_alert(message: str):
    if not DISCORD_WEBHOOK:
        print("❗ 디스코드 Webhook URL이 없습니다.")
        return
    data = {"content": f"🔔 {message}"}
    try:
        requests.post(DISCORD_WEBHOOK, json=data)
    except Exception as e:
        print(f"❗ 디스코드 전송 실패: {e}")

def verify_signature(payload, signature):
    expected = 'sha256=' + hmac.new(GITHUB_SECRET, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)

@app.route('/webhook', methods=['POST'])
def webhook():
    print("✅ Webhook POST 요청 받음")
    
    signature = request.headers.get('X-Hub-Signature-256')
    if signature is None or not verify_signature(request.data, signature):
        print("❌ Signature 검증 실패")
        send_discord_alert("❌ Signature 검증 실패")
        abort(403)
    
    print("✅ Signature OK > git pull 시작")
    subprocess.call(['git', '-C', '/home/ubuntu/SMC_Trader', 'pull'])
    print("✅ git pull 완료 > 응답 전송")
    send_discord_alert("코드 업데이트 완료!")  # repo_name 제거
    return '✅ Verified & Pull done', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
