from binance.client import Client
from dotenv import load_dotenv
import os

load_dotenv()
client = Client(os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET"))

try:
    res = client.futures_account()
    print("정상 연결됨:", res['totalWalletBalance'])
except Exception as e:
    print("에러 발생:", e)
