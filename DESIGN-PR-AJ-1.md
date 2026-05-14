# DESIGN-PR-AJ-1: Fix deletion_protection and kamailio IG detach regression

## Status
DRAFT

## Problem

3차 dogfood에서 두 가지 버그가 발견됐다.

### Bug 1: deletion_protection = true 하드코딩

`terraform/cloudsql.tf`의 `google_sql_database_instance.voipbin`(MySQL, line 14)과
`google_sql_database_instance.voipbin_postgres`(Postgres, line 149) 두 리소스에
`deletion_protection = true`가 하드코딩되어 있다.
`terraform/gke.tf`의 `google_container_cluster.voipbin`은 provider 기본값이
`true`이며 명시적 `false`가 없다.

결과: `voipbin-install destroy`가 다음 에러로 중단된다.
```
Error: Error, failed to delete instance because deletion_protection is set to true.
Error: Cannot destroy cluster because deletion_protection is set to true.
```
수동으로 `terraform apply -target`으로 `false` 반영 후 destroy 재실행해야 했다.

### Bug 2: google_compute_instance_group.kamailio 오 detach (PR #68 regression)

PR #68의 `DESTROY_STATE_DETACH` 목록에 `google_compute_instance_group.kamailio`가
포함됐다. 이 리소스는 state에서만 제거되고 GCP에서는 삭제되지 않는다.
결과: 다음 apply 시 새 VPC (다른 network ID)와 충돌이 발생한다.
```
Error: Resource is expected to be in network 'voipbin-vpc' (ID: '6803...') but
is in network 'voipbin-vpc' (ID: '7732...'). wrongNetwork
```

## 근본 원인 분석

### Bug 2 상세: kamailio IG vs. node_pool — 왜 다르게 처리하는가

`DESTROY_STATE_DETACH`에 리소스를 추가하는 올바른 기준:

**기준 A — GCS backend 보호:** destroy 중 state backend가 삭제되기 전에 detach해야
하는 리소스. `google_storage_bucket.terraform_state`, `google_kms_*` 해당.

**기준 B — GKE 관리형 lifecycle 충돌 방지:** GKE는 node pool 삭제 시 내부적으로
Managed Instance Group(MIG) 리소스를 남긴다. terraform은 이 MIG를
`google_container_node_pool`의 일부로 추적하지 않으므로, 다음 apply 시
stale ID 참조로 409/404 에러가 발생한다. state detach하면 terraform이 node pool을
처음부터 새로 생성하므로 충돌이 없다. `google_container_node_pool.voipbin` 해당.

**kamailio IG(`google_compute_instance_group.kamailio`)는 두 기준 모두 해당하지 않는다.**
- `loadbalancer.tf`에 정의된 unmanaged instance group이다.
- GKE managed lifecycle이 없다. terraform이 lifecycle을 완전히 소유한다.
- dependency graph 상 VPC보다 먼저 삭제되므로 terraform destroy가 정상 처리한다.
- PR #68에서 "stale GKE IG manager ID" 수정 대상으로 잘못 포함됐다.

결론: `google_compute_instance_group.kamailio` 제거, `google_container_node_pool.voipbin` 유지.

참고: `google_container_cluster.voipbin`은 `google_container_node_pool.voipbin`이
`DESTROY_STATE_DETACH`에 의해 state detach된 이후 terraform이 직접 삭제한다.
cluster의 `deletion_protection = false`는 이 삭제를 가능하게 하는 조건이다.
node pool 자체에는 `deletion_protection` 속성이 없으므로 별도 처리 불필요.

## PR-J 폐기 결정

기존 테스트(`test_pr_d1_cloudsql_postgres.py:105-106`)에 다음 계획이 명시돼 있었다:
> "deletion_protection must default to true. PR-J will introduce
>  var.cloudsql_deletion_protection for dev-tier teardown."

이 PR에서 PR-J를 폐기하고 다른 접근을 채택한다. 이유:

1. install repo의 1차 타겟은 self-hosted 운영자이며 destroy가 핵심 기능이다.
   `deletion_protection = true`가 기본값인 것이 오히려 비정상이다.
2. `var.cloudsql_deletion_protection` 방식은 destroy 시 `-var` 플래그를 요구하는데,
   `voipbin-install destroy` CLI가 내부적으로 terraform을 호출하는 구조에서
   이 변수를 전달하려면 CLI까지 변경이 필요하다. 불필요한 복잡도다.
3. 대신 `lifecycle { ignore_changes = [deletion_protection] }`을 적용한다.
   프로덕션 운영자가 GCP 콘솔 또는 gcloud로 `deletion_protection = true`로 수동
   변경해도 이후 `terraform apply`가 이를 되돌리지 않는다. 구조적 drift 방지.

## Fix

### Fix 1: deletion_protection = false + lifecycle ignore

**terraform/cloudsql.tf**
- `google_sql_database_instance.voipbin` (line 14): `deletion_protection = false`
- `google_sql_database_instance.voipbin` lifecycle 블록 추가:
  ```hcl
  lifecycle {
    ignore_changes = [deletion_protection]
  }
  ```
- `google_sql_database_instance.voipbin_postgres` (line 149): `deletion_protection = false`
- `google_sql_database_instance.voipbin_postgres` lifecycle 블록 추가 (동일)

**terraform/gke.tf**
- `google_container_cluster.voipbin`: `deletion_protection = false` 명시 추가
- `google_container_cluster.voipbin` lifecycle 블록 추가:
  ```hcl
  lifecycle {
    ignore_changes = [deletion_protection]
  }
  ```

**README.md** — 운영자 안내 섹션 추가:
```
### deletion_protection
All Cloud SQL instances and the GKE cluster are deployed with
`deletion_protection = false` to support the `voipbin-install destroy` workflow.
Production operators who want to prevent accidental deletion can set
`deletion_protection = true` via the GCP Console or gcloud after initial
deployment — Terraform will not revert this change on subsequent applies.
```

**테스트 (`tests/test_pr_d1_cloudsql_postgres.py`)**
- `test_deletion_protection_true` → `test_deletion_protection_false`로 rename
- assertion: `deletion_protection\s*=\s*false` (Postgres 블록 대상)
- 에러 메시지: `"deletion_protection must be false in install repo (destroy workflow enabled; lifecycle ignore_changes prevents drift)"`
- MySQL `deletion_protection = false` 단언 테스트 추가 (같은 파일 또는 `test_pr_d2a_cloudsql_resources.py`에 추가)

**테스트 (`tests/test_pr_aj_deletion_protection.py`) — 신규 파일**
- GKE cluster `deletion_protection = false` 단언
- GKE cluster `lifecycle.ignore_changes` 포함 단언

### Fix 2: DESTROY_STATE_DETACH에서 kamailio IG 제거

**scripts/pipeline.py**
- `DESTROY_STATE_DETACH`에서 `"google_compute_instance_group.kamailio"` 제거 (4-entry list)
- line 59 인라인 comment 변경:
  ```python
  # - Stale GKE node pool MIG ID blocking subsequent apply.
  #   google_compute_instance_group.kamailio is NOT detached here — it is a
  #   plain unmanaged IG and terraform fully owns its lifecycle (destroys before VPC).
  ```

**테스트 (`tests/test_pipeline.py`)**
- `test_tai_0_destroy_state_detach_contents`: 4-entry list로 업데이트
  - `"google_compute_instance_group.kamailio"` NOT in list 단언 추가
  - `"google_container_node_pool.voipbin"` IN list 단언 유지

## 변경 파일

- `terraform/cloudsql.tf`
- `terraform/gke.tf`
- `scripts/pipeline.py`
- `tests/test_pr_d1_cloudsql_postgres.py`
- `tests/test_pr_aj_deletion_protection.py` (신규)
- `tests/test_pipeline.py`
- `README.md`

## 리스크

낮음.
- `deletion_protection = false` + `lifecycle { ignore_changes }` 조합은 dogfood
  destroy를 가능하게 하면서 프로덕션 운영자의 수동 변경을 보호한다.
- kamailio IG 제거는 PR #68 버그 수정이며 3차 dogfood에서 수동 개입으로 동작
  확인됐다.
- 기존 pytest 통과 기준: 1068 passed. 이 PR 이후에도 동일 수준 유지.

## 테스트 계획

1. `pytest tests/` — 0 failed 확인
2. 4차 dogfood에서 destroy → apply 자동 검증 예정.
