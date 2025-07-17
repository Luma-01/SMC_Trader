# SL 산출 로직 개선 완료

## 개선 배경

### 문제점
- **5분봉(LTF)에서는 SL/TP가 구조적으로 자연스러워 보이지만, 1시간봉(HTF)에서는 SL이 진입가와 너무 가까워서 '노이즈'에 쉽게 잘릴 수 있음**
- 기존 SL 산출 로직이 **진입근거(구조적 무효화) < 최소 거리 보정** 순서로 되어 있어, SMC 전략의 구조적 무효화 원칙을 제대로 반영하지 못함
- HTF와 LTF 간 SL/TP 일관성 부족으로 실전에서 의도치 않은 손절 발생

### SMC 전략 원칙
- SL은 **구조적 무효화 지점**에 설정되어야 함 (진입근거 이탈, 직전 고점/저점, OB 상단/하단 등)
- 최소 거리는 **구조적 무효화가 불가능할 때만** 폴백으로 사용
- HTF 구조적 무효화 > LTF 구조적 무효화 > 최소 거리 순서

## 개선 내용

### 1. HTF 구조적 무효화 지점 산출 함수 추가 (`core/utils.py`)

#### `get_htf_structural_invalidation()` 함수
- **진입근거 존 기반 무효화** (최우선): OB/BB 상단/하단 이탈 시점
- **HTF 구조적 극값**: 직전 고점/저점 기반 무효화
- **스윙 포인트 기반 무효화**: HTF 스윙 고점/저점
- **ATR 기반 무효화**: 최후 폴백 (2 ATR 거리)

#### `calculate_improved_stop_loss()` 함수
- **우선순위 기반 SL 산출**:
  1. 진입근거 존 기반 무효화 (OB/BB 상단/하단)
  2. HTF 구조적 무효화 (직전 고점/저점)
  3. Protective 레벨 (MSS 등)
  4. 최소 거리 폴백
- **최소 거리 조건 확인**: 구조적 무효화 지점이 최소 거리를 만족할 때만 선택
- **오류 처리**: 예외 발생 시 안전한 폴백 SL 제공

### 2. PositionManager 개선 (`core/position.py`)

#### `enter()` 함수 개선
- **HTF 데이터와 trigger_zone 매개변수 추가**
- **개선된 SL 산출 로직 적용**:
  ```python
  sl_result = calculate_improved_stop_loss(
      symbol=symbol,
      direction=direction,
      entry_price=entry,
      htf_df=htf_df,
      protective=protective,
      trigger_zone=trigger_zone,
      min_rr_base=MIN_RR_BASE
  )
  ```
- **상세한 SL 산출 근거 로깅**
- **기존 로직으로 폴백** 가능

#### `update_price()` 함수 개선
- **개선된 보호선 산출 로직 적용**
- **저장된 HTF 데이터와 trigger_zone 활용**
- **HTF/LTF 일관성 있는 보호선 산출**

### 3. Main 로직 개선 (`main.py`)

#### `handle_pair()` 함수 개선
- **pm.enter() 호출 시 HTF 데이터와 trigger_zone 전달**:
  ```python
  pm.enter(
      symbol=symbol,
      direction=direction,
      entry=entry,
      sl=None,  # SL은 pm.enter()에서 개선된 로직으로 계산
      tp=tp,
      basis=detailed_basis,
      protective=prot_lv,
      htf_df=htf,          # ★ HTF 데이터 전달
      trigger_zone=trg_zone  # ★ 진입근거 존 정보 전달
  )
  ```
- **거래소 주문용 SL과 내부 SL 분리**: 거래소에는 기존 계산된 SL 사용, 내부에서는 개선된 SL 사용

### 4. 보호선 로직 개선 (`core/protective.py`)

#### `get_improved_protective_level()` 함수 추가
- **우선순위 기반 보호선 산출**:
  1. 진입근거 존 기반 보호선 (OB/BB 상단/하단)
  2. HTF 구조적 보호선 (직전 고점/저점, 스윙 포인트)
  3. LTF 보호선 (스윙 포인트)
  4. 최후 폴백 (진입가 기준)
- **거리 및 방향 검증**: 최소 0.3% 거리 보장
- **HTF/LTF 일관성 보장**

#### `get_htf_structural_protective()` 함수 추가
- **HTF 구조적 보호선 산출**
- **스윙 포인트 기반 보조 보호선**
- **진입가 기준 방향 검증**

## 개선 효과

### 1. SMC 전략 원칙 준수
- **구조적 무효화 우선**: 진입근거 이탈 시점을 SL로 우선 사용
- **HTF 구조적 무효화 반영**: 1시간봉 기준 구조적 무효화 지점 활용
- **최소 거리는 폴백**: 구조적 무효화가 불가능할 때만 사용

### 2. HTF/LTF 일관성 보장
- **HTF에서도 의미 있는 SL**: 1시간봉 기준으로도 구조적으로 타당한 SL 위치
- **노이즈 저항성 향상**: HTF 구조적 무효화 기반으로 '노이즈' 손절 방지
- **통합 보호선 시스템**: HTF와 LTF 보호선의 일관성 있는 적용

### 3. 실전 신뢰성 향상
- **상세한 SL 근거 제공**: 디스코드 알림으로 SL 설정 근거 투명화
- **오류 처리 강화**: 예외 상황에서도 안전한 SL 제공
- **우선순위 기반 선택**: 여러 SL 후보 중 SMC 원칙에 맞는 최적 선택

## 사용 예시

### 진입 시 SL 산출 과정
```
[SL] BTCUSDT 개선된 SL 산출: 0.78650 | 근거: 진입근거 존(ob_htf) 하단 이탈 | 우선순위: 1
[PROTECTIVE] BTCUSDT 개선된 보호선: 0.78670 | 근거: HTF 구조적 저점(0.78670) | 우선순위: 2
```

### 기존 vs 개선된 SL 비교
- **기존**: 진입가 0.80000 → SL 0.79600 (0.5% 최소 거리)
- **개선**: 진입가 0.80000 → SL 0.78650 (HTF OB 하단 기준, 1.7% 거리)

## 결론

이번 개선으로 **SMC 전략의 구조적 무효화 원칙**에 맞는 SL 산출이 가능해졌으며, **HTF/LTF 간 일관성**이 보장되어 실전에서 의도치 않은 손절을 크게 줄일 수 있습니다.

특히 **1시간봉 기준으로도 구조적으로 타당한 SL 위치**가 설정되어, 기존에 5분봉에서만 자연스러워 보이던 SL/TP가 이제 모든 타임프레임에서 SMC 원칙에 부합하게 됩니다. 