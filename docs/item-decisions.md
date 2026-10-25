# 품목 판정 감사

현재 코드는 후보 추천과 목적별 적합성 판정을 구현합니다. **품목군 자동 분류기나 검증된 업무용 ML은 아닙니다.** 코드와 로컬 artifact를 확인한 범위의 판단이며, fixture 성과를 실제 시장 정확도로 해석하지 않습니다.

| 구분 | 확인한 동작과 근거 |
|---|---|
| 기존 추천 | [`core/legacy_matching.py`](../core/legacy_matching.py)의 가중치는 재질 55%·용도 20%·규격 10%·가격 15%입니다. [`core/comparison.py`](../core/comparison.py)는 동일단가와 자기 업체 제외 조건을 적용하고, 알려진 재질은 동일 재질만 후보로 둡니다. 규격은 각 변 ±10% 이내여야 합니다. 사람이 후보를 검수하는 추천입니다. |
| 입력 품목군·목적 | [`src/matching/cli.py`](../src/matching/cli.py)와 [`core/matching/contracts.py`](../core/matching/contracts.py)는 `category`와 `purpose`를 명시적으로 받습니다. 목적은 동일 식별(`exact_identity`), 동일단가 경쟁사(`same_price_competitor`), 기능 대체(`functional_substitute`)입니다. 입력 품목군을 학습 모델이 자동 분류하지 않습니다. |
| 실제 분류번호 | [`src/updater/source.py`](../src/updater/source.py)는 원천 `itemClsfNo`를 `category_id`로 보존합니다. 동봉 정책의 `signage`, `signage_accessory`, `generic_identity`는 초안 식별자이며 실제 분류번호와 검증된 매핑이 없습니다. |
| 정책 | [`core/matching/policies/`](../core/matching/policies/)에는 signage 3개, signage_accessory 3개, generic identity 1개로 총 7개 JSON 정책이 있으며 모두 `draft`입니다. 업무별 필수 규격·재질·단위·허용오차와 분류 매핑을 검증해야 합니다. |
| 모델 | [`src/matching/training.py`](../src/matching/training.py), [`src/matching/models.py`](../src/matching/models.py), [`core/matching/features.py`](../core/matching/features.py)는 요청–후보 쌍의 적합성을 rules·LogisticRegression·CatBoost로 비교합니다. 학습/보정/검증/시험 분리와 실제 모델 저장·읽기가 구현되어 있습니다. |
| 확인한 학습 근거 | 로컬에서 발견한 모델 artifact의 `environment`는 모두 `fixture`였습니다. [`test/test_matching_models.py`](../test/test_matching_models.py), [`test/ml_surface_qa.py`](../test/ml_surface_qa.py), [`test/bench_matching.py`](../test/bench_matching.py)는 합성 데이터 검증입니다. 실제 업무 라벨을 통한 production ML 정확도·커버리지 검증 근거는 확인하지 못했습니다. |
| 원천의 미확인 필드 | [`src/matching/catalog.py`](../src/matching/catalog.py)의 현재 변환은 검증된 재질을 채우지 않으며 `item_kind`, `latest_contract`도 제공하지 않습니다. [`core/matching/constraints.py`](../core/matching/constraints.py)는 필요한 사실이 없으면 `UNKNOWN`으로 처리하고 검토를 요구합니다. 상품명에서 임의로 추정해 확정하지 않습니다. |

이번 디렉터리 이동은 자동 확정 조건을 바꾸지 않습니다. production 원천, 활성 정책·release, 독립 평가 certificate, 필수 사실과 최신성, 완전한 검색 및 운영 권한 조건은 그대로 필요합니다. draft 정책이나 fixture 모델을 복사하는 것만으로 자동가동할 수 없습니다. 자세한 운영 조건은 [자동 매칭 운영](auto-matching-operations.md), 학습·인증 경계는 [모델 안내](matching-models.md)를 따릅니다.
