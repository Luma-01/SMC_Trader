# HTF 캔들 설정 변경사항 (최근 50개 캔들 기준)

## 📋 변경 요약

이 문서는 OTE 활용을 제외하고 최근 50개 HTF 캔들을 기준으로 분석하도록 시스템을 수정한 내용을 정리합니다.

## 🔧 주요 변경사항

### 1. 설정 파일 수정 (`config/settings.py`)

```python
# HTF 캔들 제한 설정 추가
HTF_CANDLE_LIMIT = int(os.getenv("HTF_CANDLE_LIMIT", "50"))
USE_OTE_VALIDATION = os.getenv("USE_OTE_VALIDATION", "false").lower() == "true"
```

- **HTF_CANDLE_LIMIT**: HTF 타임프레임 캔들을 50개로 제한
- **USE_OTE_VALIDATION**: OTE 검증 비활성화 (기본값: false)
- 설정 상태를 Discord 메시지로 출력하도록 개선

### 2. 데이터 피드 수정 (`core/data_feed.py`)

#### 캔들 저장소 개선
```python
def _get_candle_limit(timeframe: str) -> int:
    """타임프레임별 캔들 제한 반환 (HTF는 50개로 제한)"""
    return HTF_CANDLE_LIMIT if timeframe == HTF_TF else CANDLE_LIMIT

def _create_candle_deque(tf: str):
    """타임프레임별 캔들 deque 생성"""
    limit = _get_candle_limit(tf)
    return deque(maxlen=limit)
```

#### 캔들 저장소 초기화
```python
def initialize_candle_storage(symbol: str):
    """심볼별 타임프레임 캔들 저장소 초기화"""
    if symbol not in candles:
        candles[symbol] = {}
    
    for tf in TIMEFRAMES:
        if tf not in candles[symbol]:
            candles[symbol][tf] = _create_candle_deque(tf)
```

#### 과거 캔들 로딩 개선
- `initialize_historical()` 함수에서 HTF 캔들 제한 적용
- 각 타임프레임별로 적절한 캔들 수만 로딩

### 3. 메인 분석 로직 수정 (`main.py`)

```python
# HTF 캔들 수 검증 로직 개선
min_htf_candles = min(30, HTF_CANDLE_LIMIT)  # 최대 50개 중 최소 30개 확보
if df_htf is None or df_ltf is None or len(df_htf) < min_htf_candles or len(df_ltf) < 30:
    return
```

- HTF 캔들 수 검증을 50개 제한에 맞게 조정
- 최소 30개 캔들 확보 시 분석 진행

## 🚫 OTE 활용 제외

### 현재 상태
- `core/utils.py`에 OTE 검증 로직이 존재하지만 실제로 사용되지 않음
- 메인 분석 로직은 IOF (Inefficiency of Fair Value) 기반으로 동작
- `USE_OTE_VALIDATION` 설정을 통해 향후 OTE 활용 여부 제어 가능

### OTE 로직 (참고용)
```python
def is_ote_valid(htf_df, ltf_df, direction, window=20):
    # 61.8% - 79% 리트레이스먼트 영역 계산
    ote_high = htf_low + 0.79 * (htf_high - htf_low)
    ote_low = htf_low + 0.618 * (htf_high - htf_low)
    # 현재 가격이 OTE 영역 내에 있는지 검증
```

## 📊 동작 방식

### 1. 캔들 데이터 관리
- **HTF 캔들**: 최근 50개로 제한 (`deque(maxlen=50)`)
- **LTF 캔들**: 기존 1500개 유지 (`deque(maxlen=1500)`)

### 2. 분석 로직
- HTF 구조 분석은 최근 50개 캔들 기준
- LTF 반전 확인은 기존 로직 유지
- OTE 검증 단계 완전 제외

### 3. 성능 개선
- HTF 캔들 메모리 사용량 96% 감소 (1500개 → 50개)
- 구조 분석 속도 향상
- 더 빠른 신호 감지 가능

## 🔧 환경 변수 설정

`.env` 파일에서 다음 설정 가능:

```bash
# HTF 캔들 수 제한 (기본값: 50)
HTF_CANDLE_LIMIT=50

# OTE 검증 사용 여부 (기본값: false)
USE_OTE_VALIDATION=false
```

## ✅ 확인 사항

1. **캔들 데이터**: HTF 캔들이 50개로 제한되어 저장됨
2. **OTE 비활성화**: OTE 검증 로직이 사용되지 않음
3. **분석 정확도**: 충분한 캔들 수 확보 시에만 분석 진행
4. **성능**: 메모리 사용량 및 처리 속도 개선

## 🚨 주의사항

- HTF 캔들 수가 50개로 제한되어 장기간 백테스트 시 주의 필요
- 신규 심볼 추가 시 초기 캔들 수집 시간 단축
- OTE 로직 재활성화가 필요한 경우 `USE_OTE_VALIDATION=true` 설정 후 관련 로직 추가 필요