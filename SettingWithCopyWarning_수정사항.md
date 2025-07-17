# SettingWithCopyWarning 오류 수정 사항

## 문제 상황
스크린샷에서 확인된 pandas의 SettingWithCopyWarning 오류들이 여러 파일에서 발생하고 있었습니다.

## 수정된 파일들

### 1. `core/mss.py`
**문제점**: DataFrame에 직접 할당하여 SettingWithCopyWarning 발생
**수정 사항**:
- 함수 시작 부분에 `df = df.copy()` 추가
- `df['body_high'] = ...` → `df.loc[:, 'body_high'] = ...`
- `df['body_low'] = ...` → `df.loc[:, 'body_low'] = ...`
- `df['prev_close'] = ...` → `df.loc[:, 'prev_close'] = ...`

### 2. `core/structure.py`
**문제점**: DataFrame 컬럼 할당 시 SettingWithCopyWarning 발생
**수정 사항**:
- `df['body_high'] = ...` → `df.loc[:, 'body_high'] = ...`
- `df['body_low'] = ...` → `df.loc[:, 'body_low'] = ...`
- `df['prev_high'] = ...` → `df.loc[:, 'prev_high'] = ...`
- `df['prev_low'] = ...` → `df.loc[:, 'prev_low'] = ...`
- `df['structure'] = None` → `df.loc[:, 'structure'] = None`
- `df.at[df.index[i], 'structure'] = stype` → `df.loc[df.index[i], 'structure'] = stype`

### 3. `core/iof.py`
**문제점**: HTF DataFrame 조작 시 SettingWithCopyWarning 발생
**수정 사항**:
- ATR 계산 전에 `htf_df = htf_df.copy()` 추가
- `htf_df['prev_close'] = ...` → `htf_df.loc[:, 'prev_close'] = ...`

### 4. `core/monitor.py`
**문제점**: DataFrame 시간 및 가격 컬럼 변환 시 SettingWithCopyWarning 발생
**수정 사항**:
- `df['time'] = ...` → `df.loc[:, 'time'] = ...`
- `df[price_cols] = ...` → `df.loc[:, price_cols] = ...`

### 5. `core/volatility.py`
**문제점**: DataFrame 조작 시 SettingWithCopyWarning 발생 가능성
**수정 사항**:
- 함수 시작 부분에 `df = df.copy()` 추가

## 수정 원칙

1. **DataFrame 복사**: 함수 시작 시 `df = df.copy()`로 복사본 생성
2. **안전한 할당**: `df['column'] = value` 대신 `df.loc[:, 'column'] = value` 사용
3. **인덱스 기반 할당**: `df.at[index, 'column'] = value` 대신 `df.loc[index, 'column'] = value` 사용

## 테스트 결과
- pandas의 `chained_assignment` 옵션을 'raise'로 설정하여 테스트
- 모든 수정 사항이 SettingWithCopyWarning 없이 정상 작동 확인
- 기존 기능에 영향 없이 안전하게 수정 완료

## 추가 권장사항
- 향후 새로운 DataFrame 조작 코드 작성 시 `.loc[]` 인덱서 사용 권장
- DataFrame을 함수 인자로 받을 때는 항상 `.copy()` 메서드 사용 고려
- 정기적으로 pandas warning 설정을 통한 코드 품질 검증 권장