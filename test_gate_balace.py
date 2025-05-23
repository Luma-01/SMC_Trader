# test_gate_balance.py

from exchange.gate_sdk import get_available_balance

def main():
    print("🔍 Gate 잔고 조회 테스트 시작")
    balance = get_available_balance()
    print(f"💰 사용 가능 잔고 (USDT): {balance:.2f} USDT")

if __name__ == "__main__":
    main()
