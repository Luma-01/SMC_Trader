# SL(Stop Loss) 설정 문제점 분석

## 🚨 현재 상황
포지션에 SL이 걸려있지 않아서 청산까지 유지되다가 청산되는 경우가 발생하고 있습니다.

## 🔍 예상되는 문제점들

### 1. **SL 주문 생성 실패**
**위치**: `exchange/binance_api.py:294-295`
```python
# SL 주문은 update_stop_loss_order() 에서 일괄 관리하므로
# 이 지점에서는 SL 생성 로직을 비활성화합니다.
# client.futures_create_order(**sl_kwargs)
```

**문제**: 
- 진입 시 SL 주문이 **주석 처리**되어 있어 실제로 생성되지 않음
- `update_stop_loss_order()`에 의존하고 있지만 진입 직후 호출이 보장되지 않음

### 2. **PositionManager의 SL 설정 타이밍 문제**
**위치**: `core/position.py:213`
```python
sl_result = update_stop_loss(symbol, direction, sl)
if sl_result is True:       # 동일 SL → 주문 생략
    print(f"[SL] {symbol} SL unchanged (=BE)")
```

**문제**:
- `sl_result is True`일 때 "동일 SL"로 판단하여 주문을 생략
- 하지만 실제로는 SL 주문이 존재하지 않을 수 있음
- 초기 진입 시에도 이 로직이 적용되어 SL이 설정되지 않을 가능성

### 3. **SL 주문 검증 로직의 허점**
**위치**: `exchange/router.py:98-120`
```python
def _current_sl_price(sym: str) -> float | None:
    try:
        if sym in GATE_SET:                 # ── Gate
            # Gate SL 조회 로직
        else:                               # ── Binance
            # Binance SL 조회 로직
    except Exception as e:
        print(f"[router] SL 가격 조회 실패({sym}) → {e}")
    return None

cur_sl = _current_sl_price(symbol)
if cur_sl is not None and abs(cur_sl - stop_price) < float(tick):
    # ±1 tick 이내면 동일 주문으로 간주 → no-op
    return True
```

**문제**:
- SL 조회 실패 시 `None`을 반환하지만, 이후 로직에서 "SL이 없다"와 "조회 실패"를 구분하지 못함
- 조회 실패 시에도 새로운 SL 주문을 생성해야 하는데 그렇지 않을 수 있음

### 4. **진입 시 SL 설정 순서 문제**
**위치**: `core/position.py:655-670`
```python
def init_position(self, symbol: str, direction: str, entry: float, sl: float, tp: float):
    self.positions[symbol] = {
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        # ...
    }
```

**문제**:
- `init_position()`에서는 메모리상 포지션만 생성하고 실제 거래소 SL 주문은 생성하지 않음
- 별도의 `update_stop_loss()` 호출에 의존하는데, 이것이 실패하면 SL이 없는 상태가 됨

### 5. **동기화 문제**
**위치**: `core/position.py:sync_from_exchange()`
```python
sl_px = sl_px or (entry * 0.98)      # 대충 2 % 폴백
```

**문제**:
- 거래소에서 포지션을 동기화할 때 SL이 없으면 임의의 2% 값으로 설정
- 실제 거래소에는 SL 주문이 없는데 메모리상으로만 존재하게 됨

### 6. **에러 처리 부족**
**위치**: 여러 곳에서 SL 주문 생성 실패에 대한 적절한 에러 처리가 부족

**문제**:
- SL 주문 생성 실패 시 재시도 로직이 없음
- 실패 시 알림이나 로깅이 충분하지 않음
- 포지션은 열려있지만 SL은 없는 위험한 상태가 지속될 수 있음

## 🛠️ 권장 해결방안

### 1. **진입 시 SL 강제 생성**
```python
# binance_api.py에서 주석 해제 및 강화
try:
    sl_order = client.futures_create_order(**sl_kwargs)
    print(f"[SL] 진입 시 SL 주문 생성 완료: {sl_order['orderId']}")
except Exception as e:
    print(f"[ERROR] SL 주문 생성 실패: {e}")
    # 재시도 로직 또는 포지션 강제 종료
```

### 2. **SL 검증 및 재시도 로직**
```python
def ensure_stop_loss(symbol: str, direction: str, sl_price: float, max_retries: int = 3):
    """SL 주문이 확실히 존재하도록 보장"""
    for attempt in range(max_retries):
        if verify_sl_exists(symbol, sl_price):
            return True
        
        success = update_stop_loss(symbol, direction, sl_price)
        if success:
            time.sleep(1)  # 주문 반영 대기
            continue
        
        time.sleep(2 ** attempt)  # 지수백오프
    
    # 최종 실패 시 포지션 강제 종료 고려
    send_alert(f"[CRITICAL] {symbol} SL 설정 실패 - 수동 확인 필요")
    return False
```

### 3. **주기적 SL 검증**
```python
def health_check_stop_losses(self):
    """모든 포지션의 SL 주문 존재 여부 검증"""
    for symbol, pos in self.positions.items():
        if not verify_sl_exists(symbol, pos['sl']):
            print(f"[WARN] {symbol} SL 주문 누락 감지 - 재생성 시도")
            update_stop_loss(symbol, pos['direction'], pos['sl'])
```

### 4. **알림 강화**
- SL 설정 실패 시 즉시 Discord 알림
- 주기적으로 SL 없는 포지션 체크 및 알림
- 청산 위험 임박 시 긴급 알림

## ⚠️ 즉시 확인이 필요한 사항

1. **현재 열린 포지션들의 실제 SL 주문 존재 여부 확인**
2. **바이낸스 API에서 SL 주문 생성이 주석처리된 이유 확인**
3. **`update_stop_loss()` 함수의 실제 동작 검증**
4. **에러 로그에서 SL 관련 실패 메시지 확인**

이러한 문제들을 해결하면 포지션이 SL 없이 청산까지 가는 위험을 크게 줄일 수 있습니다.