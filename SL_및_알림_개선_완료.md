# SL 거리 및 디스코드 알림 개선 완료

## 🔍 문제 분석

### 1. SL이 너무 가까운 문제
- **현재 상황**: SL이 너무 가까워서 포지션이 금방 끝나버리는 경우가 다수 발생
- **원인**: 최소 SL 거리가 1%로 설정되어 있어서 작은 변동에도 손절 발생

### 2. 디스코드 알림 문제
- **현재 상황**: `[TP/SL] ETHUSDT 진입 0.006 → TP:3313.53, SL:3397.63` 형태의 간단한 알림만 전송
- **문제점**: 진입근거, TP 설정 이유, SL 설정 이유 등 상세 정보가 포함되지 않음

## 🛠️ 해결 방법

### 1. SL 최소 거리 증가

#### 설정 파일 수정
```python
# config/settings.py
# ▶ SL 최소 거리 설정 (진입가 대비 최소 2% 이상)
MIN_SL_DISTANCE_PCT = 0.02  # 2% 최소 거리 (기존 1%에서 증가)
```

#### SL 계산 로직 개선
```python
# main.py
# ── 5) **리스크-가드** : 엔트리-SL 간격이 최소 거리 미만이면 강제 확대 ───
min_rr = Decimal(str(MIN_SL_DISTANCE_PCT))  # 설정값 사용 (2%)
risk_ratio = (abs(entry_dec - sl_dec) / entry_dec).quantize(Decimal("0.00000001"))
if risk_ratio < min_rr:
    adj = (min_rr * entry_dec - abs(entry_dec - sl_dec)).quantize(tick_size)
    sl_dec = (sl_dec - adj) if direction == "long" else (sl_dec + adj)
    sl_dec = sl_dec.quantize(tick_size)
    print(f"[SL] {symbol} SL 최소 거리 확대: {float(risk_ratio*100):.2f}% → {float(min_rr*100):.2f}%")
```

### 2. 디스코드 알림 개선

#### 거래소 API 파일 수정
```python
# exchange/binance_api.py
# 간단한 진입 알림만 전송 (상세 정보는 main.py에서 처리)
print(f"[TP/SL] {symbol} 진입 {filled_qty} → TP:{tp_str}, SL:{sl_str}")
# send_discord_message는 main.py에서 상세 정보와 함께 전송

# exchange/gate_sdk.py
# 간단한 진입 알림만 전송 (상세 정보는 main.py에서 처리)
msg = f"[TP/SL] {symbol} 진입 및 TP/SL 설정 완료 → TP: {tp}, SL: {sl}"
print(msg)
# send_discord_message는 main.py에서 상세 정보와 함께 전송
```

#### 상세 진입근거 구성 (이미 구현됨)
```python
# main.py - 상세 진입근거 구성
entry_reason = []
entry_reason.append(f"진입근거: {basis}")
entry_reason.append(f"SL근거: {sl_reason}")
entry_reason.append(f"TP근거: {tp_method} (거리: {distance:.3f})")
entry_reason.append(f"HTF구조: {last_structure}")
entry_reason.append(f"유동성사냥: {liquidity_status}")

# core/position.py - 개선된 알림 메시지
msg = (
    f"🚀 **[ENTRY]** {symbol} | {direction.upper()} @ {entry:.4f}\n"
    f"🛡️ SL: {sl:.4f} | 🎯 TP: {tp:.4f}\n"
    f"📊 리스크: {risk_distance:.4f} | 보상: {reward_distance:.4f} | R:R = {risk_reward_ratio:.2f}"
    f"\n📋 {detailed_basis}"
)
```

## 📊 개선 효과

### SL 거리 개선
- **최소 거리 증가**: 1% → 2%로 증가하여 더 안정적인 SL 설정
- **조기 손절 방지**: 작은 변동으로 인한 조기 손절 감소
- **리스크 관리**: 적절한 리스크-보상 비율 유지

### 알림 개선
- **상세 정보 제공**: 진입근거, SL근거, TP근거, HTF구조, 유동성사냥 확인
- **투명성 향상**: 모든 거래 결정의 근거를 명확히 표시
- **디버깅 용이**: 문제 발생 시 원인 파악이 쉬워짐

## 🎯 예상 결과

### 개선된 디스코드 알림 예시
```
🚀 **[ENTRY]** ETHUSDT | LONG @ 3300.0
🛡️ SL: 3234.0 | 🎯 TP: 3397.0
📊 리스크: 66.0 | 보상: 97.0 | R:R = 1.47
📋 진입근거: OB 3290~3310 | SL근거: OB 하단 + 버퍼 | TP근거: 유동성레벨 (거리: 97.000) | HTF구조: BOS_up | 유동성사냥: 확인됨
```

### SL 거리 개선 효과
- **기존**: 1% 거리로 작은 변동에도 손절
- **개선**: 2% 거리로 더 안정적인 포지션 유지
- **예상**: 조기 손절 감소로 수익성 향상

## ⚙️ 설정 옵션

### SL 최소 거리 조정
```python
# config/settings.py
MIN_SL_DISTANCE_PCT = 0.02  # 2% (기본값)
# MIN_SL_DISTANCE_PCT = 0.015  # 1.5% (더 공격적)
# MIN_SL_DISTANCE_PCT = 0.025  # 2.5% (더 보수적)
```

### TP 최소 거리 조정
```python
# config/settings.py
MIN_TP_DISTANCE_PCT = 0.01  # 1% (기본값)
# MIN_TP_DISTANCE_PCT = 0.015  # 1.5% (더 보수적)
# MIN_TP_DISTANCE_PCT = 0.008  # 0.8% (더 공격적)
```

## 🔍 모니터링 포인트

1. **SL 거리 로그 확인**:
   ```
   [SL] ETHUSDT SL 최소 거리 확대: 1.20% → 2.00%
   ```

2. **개선된 진입 알림 확인**:
   ```
   🚀 **[ENTRY]** ETHUSDT | LONG @ 3300.0
   🛡️ SL: 3234.0 | 🎯 TP: 3397.0
   📊 리스크: 66.0 | 보상: 97.0 | R:R = 1.47
   📋 진입근거: OB 3290~3310 | SL근거: OB 하단 + 버퍼 | TP근거: 유동성레벨 (거리: 97.000) | HTF구조: BOS_up | 유동성사냥: 확인됨
   ```

## 📈 예상 성과

1. **수익성 개선**: 조기 손절 감소로 더 나은 수익 추구
2. **안정성 향상**: 적절한 SL 거리로 안정적인 포지션 관리
3. **투명성**: 모든 거래 결정의 근거를 명확히 파악 가능
4. **학습 효과**: 진입근거별 성과 분석을 통한 전략 개선 