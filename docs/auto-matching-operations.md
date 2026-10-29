# 자동 매칭 운영

기존 `python compare_file.py INPUT.xls`는 suggest입니다. 온라인 또는 `--catalog-db DB --catalog-run RUN --allow-incomplete` 진단 추천을 유지합니다. 진단 자료를 shadow/auto 정본으로 사용할 수 없습니다. limited 자료는 허용하더라도 incomplete 표식을 유지합니다.

현재 실제 업무 gold 라벨과 적격 production certificate가 없으므로 **업무 자동가동 준비 완료가 아닙니다**. fixture 학습/벤치마크는 소프트웨어 검증입니다. 전건 보류를 정확도 성공으로 해석하지 않습니다.

## 설치 계정과 의존성

기본 `requirements.txt`는 기존 추천/변환에 사용합니다. CPU 매칭은 별도 환경에서 `pip install -r requirements-matching.in`을 설치합니다. `python -m src.matching.cli --help`는 CPU 모델을 로드하지 않습니다.

관리자가 보호된 실행 환경에 `NARA_MATCHING_CONFIG=/etc/nara/matching.json`을 설정합니다. 직원이 환경변수/설정/실행 계정을 바꿀 수 있는 일반 셸을 권한 경계로 사용하지 마십시오. 직원에게는 신뢰된 launcher의 run/status/read만 제공하고 운영 명령은 운영 계정으로 실행합니다. 설정은 root 또는 설치 실행 계정 소유이고 group/world writable이 아니어야 합니다. 실제 OS UID가 roles_by_uid에 매핑됩니다. XLSX reviewer나 CLI role 문자열로 권한을 얻을 수 없습니다.

```json
{
  "source_root": "/srv/nara/source",
  "authority_root": "/srv/nara/authority",
  "artifact_root": "/srv/nara/artifacts",
  "ledger": "/srv/nara/matching.sqlite",
  "policy_dir": "/srv/nara/policies",
  "dataset_root": "/srv/nara/datasets",
  "index_dir": "/srv/nara/index",
  "roles_by_uid": {"1001": ["reviewer", "evaluator"]}
}
```

운영 계정은 이 설정과 authority/artifacts/policies/datasets 디렉터리를 소유하고 쓰기 권한을 제한합니다. 자동 모드는 설정에 등록된 source와 ledger만 사용합니다. certificate JSON을 직원이 지정했다고 활성 release가 되지 않습니다. 보호된 ReleaseAuthority의 활성 pointer와 독립 평가 결과를 검증합니다.

## 직원 shadow 실행

```sh
python -m src.matching.cli index --catalog-manifest /srv/nara/source/catalog-manifest.json --source-root /srv/nara/source --out /srv/nara/index
python compare_file.py INPUT.xlsx --mode shadow --purpose functional_substitute --category CATEGORY --catalog-manifest MANIFEST.json --source-root /srv/nara/source --policy-dir /srv/nara/policies --release RELEASE.json --ledger /srv/nara/matching.sqlite --index-dir /srv/nara/index --out REPORT.xlsx
python -m src.matching.cli status --ledger /srv/nara/matching.sqlite
```

`python -m src.matching.cli run INPUT.xlsx`에도 같은 인자를 사용합니다. auto는 `--mode auto`로 지정하며 인증/활성/정본/maintenance 조건을 모두 만족해야 합니다. shadow의 model=null draft release는 허용됩니다. 목적과 품목군을 반드시 지정합니다. auto/shadow의 가격 fallback은 0이고 nonzero는 오류입니다. 이미지 HTTP는 실행하지 않습니다. malformed 입력은 오류로 종료하고 결과 파일을 성공처럼 만들지 않습니다. 출력 파일은 덮어쓰지 않습니다.

업체별 비교표에는 요청판정과 후보선택/필수조건을 따로 표시합니다. `판정요약`, 예외만 담는 `검토필요`, 모든 후보의 `판정근거`, `데이터버전`을 확인합니다. 원본 문자열은 수식으로 실행되지 않습니다. 가격은 숫자와 정확한 원문 숫자 둘 다 남깁니다. `source_snapshot_verified=false`는 외부 관측창의 사실이며, 검증된 immutable storage와 reconciled publication을 부정하는 값이 아닙니다.

## 검수와 frozen 데이터

```sh
python -m src.matching.cli review-export --ledger /srv/nara/matching.sqlite --out reviews-evidence.jsonl
python -m src.matching.cli review-import reviews.jsonl
python -m src.matching.cli labels-import labels.jsonl
python -m src.matching.cli train --labels labels.jsonl --pairs pairs.jsonl --split split.json --out training.json
python -m src.matching.cli evaluate --labels labels.jsonl --pairs pairs.jsonl --split split.json --release CANDIDATE.json --catalog-manifest MANIFEST.json --out evaluation.json
```

review-export는 읽기 전용 근거를 제공합니다. review-import JSONL 각 행은 `review_id,job_id,candidate_id,evidence_hash,target,reason,reviewed_at,expires_at`만 받습니다. 시각은 timezone 포함 ISO8601입니다. 상품/가격/근거를 수정하면 hash 불일치 또는 extra-field 오류로 거부합니다. 같은 review 재전송은 멱등이고 다른 내용으로 같은 ID를 사용하면 실패합니다. human_confirmed는 pending approval이며 즉시 자동 registry가 되지 않습니다. 독립 평가를 통과한 새 registry/release만 재사용합니다.

labels JSONL 및 split 계약은 [라벨 안내](matching-label-guide.md)를 따릅니다. pairs JSONL은 `{"request": <dumps_contract(InputItem) envelope>, "candidate": <dumps_contract(Candidate) envelope>}` 형식입니다. 각 라벨의 request/candidate ID와 정확히 대응해야 합니다. `freeze_split(labels)`의 새 manifest를 사용합니다. 모든 frozen test 라벨/요청(unknown 포함)을 평가하며 동일 holdout 재사용은 불가합니다. train은 train/calibration/validation만 사용하고 모델별 실제 vocabulary/calibrator/native binary와 frozen 입력을 보존합니다. 학습/평가 실패를 생산 인증으로 바꾸지 않습니다.

## 감사와 rollback

```sh
python -m src.matching.cli audit --review-id NEGATIVE_REVIEW_ID
python -m src.matching.cli rollback --purpose functional_substitute --category CATEGORY
python -m src.matching.cli rollback --purpose functional_substitute --category CATEGORY --release PREVIOUS_CERTIFIED_RELEASE.json
```

audit는 기존 auto_confirmed 판정의 신뢰된 negative review를 확인한 뒤 해당 cell을 즉시 shadow로 내립니다. append-only 운영 이력이 남습니다. 다른 cell을 일괄 승격하지 않습니다. 이전 release 복원은 실제 model/calibrator/vocabulary/policy/registry/frozen dataset과 평가 certificate가 있어야 하며 ReleaseAuthority가 다시 검증합니다. 신규 모델의 자동 재학습·자동 외부 알림은 없습니다.

status의 coverage는 실제 ledger 판정 비율입니다. 업무 정확도와 신규 패턴 비율은 대표 baseline/정답 없이는 null이며 한계를 함께 표시합니다. UNKNOWN 필드/건수, 검토량, 오류를 별도로 확인합니다.

## 실제 artifact 백업·복원

```sh
python -m src.matching.cli backup /backups/nara-20261023
python -m src.matching.cli restore /backups/nara-20261023
python -m src.matching.cli resume
```

CLI 운영 명령은 matching operation lock을 배타적으로 잡습니다. 실행 중 matching jobs가 끝난 다음 작업하며 source restore는 active snapshot pin이 남아 있으면 거절합니다. updater maintenance.flag는 dispatcher/bootstrap/new auto를 막습니다. source backup/restore는 updater API를 그대로 사용하며 source pointer를 먼저 복원합니다. 이후 SQLite 일관 backup, 실제 artifact 파일과 hash inventory, frozen datasets, policy/authority를 복원하고 source 검증 후 파생 index를 재생성합니다. binary 하나라도 누락되거나 hash/calibrator/registry가 틀리면 auto를 재개하지 않습니다. 실패 후에도 maintenance는 유지합니다.

복원은 자동 재개하지 않습니다. `resume`은 artifact/source 검증과 index 재생성 후 gate를 해제하지만 native schedules는 비활성으로 남습니다. 운영자가 별도 검토해야 합니다. 백업 JSON만으로 실제 모델 복원이라고 주장하지 않습니다. 활성/rollback/label/evaluation 참조 파일의 자동 삭제 기능은 없습니다. 추가 파일을 수동 삭제하지 마십시오.

## 측정

```sh
python -m test.bench_matching --rows 1000000 --queries 1000 --workers 2 --out .omo/evidence/g2b-auto-matching/task-13-benchmark.json
```

실제 streaming SQLite index와 두 프로세스의 CPU scoring을 측정합니다. 데이터는 증강 fixture이며 실제 시장 데이터/업무 정답이 아닙니다. p95 목표는 행당 5초입니다. 호스트 CPU/메모리 제한을 별도로 설정하지 않았으므로 두 worker 사용만으로 2vCPU/8GiB 환경 충족이라고 주장하지 않습니다. retained artifacts 디렉터리는 검증 후 운영자가 삭제할 수 있는 task-owned benchmark 데이터입니다.

### 명시적인 holdout/absence 근거

`evaluate --cases cases.json`은 **운영자 전용**입니다. 일반 run/Excel은 이 옵션을 받지 않습니다. JSON은 다음 구조이며 frozen split의 모든 request ID를 정확히 한 번 포함해야 합니다.

```json
{"split_hash":"64자리 split.content_hash", "cases":[{"request_id":"원래 요청 ID", "holdout":"supplier", "absence_verified":null, "evidence_hash":"64자리 실제 검수 근거 SHA256"}]}
```

holdout은 `representative`, `supplier`, `subsequent_time` 중 하나입니다. supplier/time 또는 알려진 absence를 전달하려면 실제 근거 hash가 필수입니다. 시스템이 부재나 holdout을 추정하지 않습니다. metadata는 test 소비 전에 dataset_root/split_hash/evaluation-cases에 변경 불가 파일로 저장하고, 모든 request/holdout/absence/evidence와 split hash의 canonical hash가 evaluation report에 포함됩니다. certificate의 evaluation hash가 이 값을 함께 묶습니다. `--cases`가 없으면 representative/absence unknown이며 필요한 holdout/no-match 인증 근거가 충족되지 않습니다.

auto는 설정의 index_dir만 사용합니다. cache root, descriptor와 실제 SQLite 파일의 경로·symlink·소유권·쓰기 권한을 검사하므로 직원이 별도 cache의 내용을 바꾸고 hash만 다시 계산해서 제출할 수 없습니다.
