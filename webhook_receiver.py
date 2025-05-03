# webhook_receiver.py


from flask import Flask, request, abort
import subprocess
import hmac
import hashlib
import os
from dotenv import load_dotenv
from notify.discord import send_discord_message

load_dotenv()

DISCORD_WEBHOOK = os.getenv("SEND_MESSAGE_BINANCE")

app = Flask(__name__)
GITHUB_SECRET = os.getenv("GITHUB_SECRET").encode()

def send_discord_alert(message: str):
    send_discord_message(f"🔔 {message}", exchange="aggregated")

def verify_signature(payload, signature):
    mac = hmac.new(GITHUB_SECRET, msg=payload, digestmod=hashlib.sha256)
    expected_signature = 'sha256=' + mac.hexdigest()
    return hmac.compare_digest(expected_signature, signature)

@app.route('/webhook', methods=['POST'])
def webhook():
    print("✅ Webhook POST 요청 받음")
    
    signature = request.headers.get('X-Hub-Signature-256')
    if signature is None or not verify_signature(request.data, signature):
        print("❌ Signature 검증 실패")
        send_discord_message("❌ Signature 검증 실패", exchange="aggregated")
        abort(403)
    
    print("✅ Signature OK > git pull 시작")
    subprocess.call(['git', '-C', '/home/ubuntu/SMC_Trader', 'pull'])
    print("✅ git pull 완료 > 응답 전송")
    send_discord_message("코드 업데이트 완료!", exchange="aggregated")
    return '✅ Verified & Pull done', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
