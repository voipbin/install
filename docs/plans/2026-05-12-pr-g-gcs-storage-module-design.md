# PR-G — GCS Storage Terraform module + `k8s.py` fallback fix

**Date:** 2026-05-12
**Author:** Hermes (CPO)
**Status:** Design v1
**Repo:** `voipbin/install`
**Branch:** `NOJIRA-PR-G-gcs-storage-module`
**Parent:** main `20ce352`
**Roadmap slot:** PR-G (Phase 2, infra + config plumbing)
**Gaps addressed:** GAP-16, GAP-17, A-6
**LOC estimate:** ~180

---

## 1. Context

Today VoIPBin recordings and tmp-media buckets are referenced throughout the
stack but **never provisioned by Terraform**:

- `terraform/storage.tf:1-53` defines only `google_storage_bucket.terraform_state`
  (line 2) and `google_storage_bucket.media` (line 28). No `recordings` /
  `tmp` buckets.
- `terraform/outputs.tf:1-199` has no recording- or tmp-bucket outputs.
- `scripts/k8s.py:74-76` papers over the gap with a **silent literal fallback**:
  ```python
  "PLACEHOLDER_RECORDING_BUCKET_NAME": terraform_outputs.get(
      "recording_bucket_name", f"{project_id}-voipbin-recordings"
  ),
  ```
  The default fires every time because `recording_bucket_name` is never emitted
  by Terraform, so manifests render against a bucket that does not exist —
  symptoms surface only at runtime when `storage-manager` / `asterisk-call`
  attempt to write objects (GAP-16).
- `k8s/backend/secret.yaml:24-25` likewise bakes literal defaults
  `PLACEHOLDER_PROJECT_ID-voipbin-media` and `PLACEHOLDER_PROJECT_ID-voipbin-tmp`
  with no tmp bucket actually created anywhere (GAP-17).
- `scripts/terraform_reconcile.py:266-280` reconciles `media` + `terraform_state`
  buckets only; nothing for `recordings`/`tmp`, so resume after a 409 is
  impossible (A-6).
- `scripts/terraform_reconcile.py:432` has empty `FIELD_MAP` ready for entries
  (PR-A scaffolding) — outputs auto-population landed but no field uses it yet.

PR-G closes the loop: creates the two buckets in Terraform, exposes them as
outputs, threads outputs through `config.yaml` via `FIELD_MAP`, replaces the
silent fallback in `k8s.py` with explicit substitution tokens, and adds
reconcile registry entries so resume works.

## 2. Scope

### 2.1 `terraform/storage.tf` (NEW resources, ~50 LOC)

Append two `google_storage_bucket` resources after the existing `media` block
(after line 53):

```hcl
# Recordings bucket — versioned, regional, uniform IAM.
resource "google_storage_bucket" "recordings" {
  name     = "${var.env}-voipbin-recordings"
  location = var.region                # regional (matches media)
  project  = var.project_id

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  depends_on = [time_sleep.api_propagation]
}

# Tmp bucket — non-versioned, 7-day lifecycle delete.
resource "google_storage_bucket" "tmp" {
  name     = "${var.env}-voipbin-tmp"
  location = var.region
  project  = var.project_id

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true   # tmp data is disposable

  versioning {
    enabled = false
  }

  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [time_sleep.api_propagation]
}
```

Naming uses `var.env` (defined `terraform/variables.tf:86`) per task spec; the
existing `media`/`terraform_state` blocks use `${var.project_id}-${var.env}-…`
and will remain unchanged for back-compat (out of scope to rename).

### 2.2 `terraform/outputs.tf` (NEW outputs, ~12 LOC)

Append a new section after the `Project Info` block (after line 144) **or**
under a new `# GCS Buckets` divider:

```hcl
###############################################################################
# GCS Storage Buckets (PR-G)
###############################################################################

output "recordings_bucket_name" {
  description = "Name of the call-recordings GCS bucket"
  value       = google_storage_bucket.recordings.name
}

output "tmp_bucket_name" {
  description = "Name of the disposable tmp GCS bucket (7-day TTL)"
  value       = google_storage_bucket.tmp.name
}
```

### 2.3 `scripts/terraform_reconcile.py::build_registry` (~16 LOC)

Append two entries in the GCS-buckets section at
`scripts/terraform_reconcile.py:266-280`, right after the `media` and
`terraform_state` blocks:

```python
entries.append({
    "tf_address":   "google_storage_bucket.recordings",
    "description":  "GCS Recordings Bucket",
    "gcloud_check": ["gcloud", "storage", "buckets", "describe",
                     f"gs://{config.get('env')}-voipbin-recordings",
                     f"--project={project}"],
    "import_id":    f"{config.get('env')}-voipbin-recordings",
})
entries.append({
    "tf_address":   "google_storage_bucket.tmp",
    "description":  "GCS Tmp Bucket",
    "gcloud_check": ["gcloud", "storage", "buckets", "describe",
                     f"gs://{config.get('env')}-voipbin-tmp",
                     f"--project={project}"],
    "import_id":    f"{config.get('env')}-voipbin-tmp",
})
```

Import format `<bucket_name>` matches existing `media`/`terraform_state` entries
(reconcile.py:272, 279).

### 2.4 `scripts/terraform_reconcile.py::FIELD_MAP` (~6 LOC)

Replace empty `FIELD_MAP` at `scripts/terraform_reconcile.py:432` with:

```python
FIELD_MAP: list[TfOutputFieldMapping] = [
    TfOutputFieldMapping(
        tf_key="recordings_bucket_name",
        cfg_key="recordings_bucket",
    ),
    TfOutputFieldMapping(
        tf_key="tmp_bucket_name",
        cfg_key="tmp_bucket",
    ),
]
```

Both rely on the default `_always_valid` validator (terraform_reconcile.py:23).
PR-A's `outputs()` (line 435) already handles `None`/empty skipping and
operator-set-precedence.

### 2.5 `scripts/k8s.py` substitution map (~12 LOC)

Replace lines 74-76 (the silent-fallback block) with **explicit** tokens
matching the Terraform output keys. No literal defaults:

```python
"PLACEHOLDER_RECORDINGS_BUCKET": terraform_outputs.get(
    "recordings_bucket_name", config.get("recordings_bucket", "")
),
"PLACEHOLDER_TMP_BUCKET": terraform_outputs.get(
    "tmp_bucket_name", config.get("tmp_bucket", "")
),
```

Rationale: TF output is authoritative; config is the FIELD_MAP-populated
fallback (covers `--stage k8s_apply` runs after a partial reconcile_outputs);
empty string sentinel is detectable by preflight (left for PR-K to harden).

Drop the legacy `PLACEHOLDER_RECORDING_BUCKET_NAME` token only after callers
migrate (see §2.6 — manifests rename in same PR).

### 2.6 Kubernetes manifest token rename (~20 LOC)

Update placeholders in manifests to consume the new token names:

- `k8s/voip/configmap.yaml:8` —
  `RECORDING_BUCKET_NAME: PLACEHOLDER_RECORDING_BUCKET_NAME` →
  `RECORDING_BUCKET_NAME: PLACEHOLDER_RECORDINGS_BUCKET`.
- `k8s/backend/secret.yaml:24` —
  `GCP_BUCKET_NAME_MEDIA: "PLACEHOLDER_PROJECT_ID-voipbin-media"` keeps current
  literal **for media** (out of scope; PR-G is recordings+tmp only).
- `k8s/backend/secret.yaml:25` —
  `GCP_BUCKET_NAME_TMP: "PLACEHOLDER_PROJECT_ID-voipbin-tmp"` →
  `GCP_BUCKET_NAME_TMP: "PLACEHOLDER_TMP_BUCKET"`.
- `k8s/backend/secret.yaml:41` — leave (`PROJECT_BUCKET_NAME` → media, out of
  scope).

`asterisk-call/deployment.yaml:82-90` and `asterisk-conference/deployment.yaml:80-88`
read `RECORDING_BUCKET_NAME` from configmap — already covered transitively by
the configmap rename above.

### 2.7 `config/schema.py` (~10 LOC)

Add two optional fields under `properties` (after `cloudsql_private_ip_cidr`,
schema.py:78-84):

```python
"recordings_bucket": {
    "type": "string",
    "description": (
        "Name of the GCS bucket for call recordings. Auto-populated "
        "from Terraform output `recordings_bucket_name` by "
        "reconcile_outputs; operator override respected."
    ),
},
"tmp_bucket": {
    "type": "string",
    "description": (
        "Name of the GCS bucket for disposable tmp media (7-day TTL). "
        "Auto-populated from Terraform output `tmp_bucket_name`."
    ),
},
```

No default value emitted at init-time — empty until reconcile_outputs writes
them. `config/defaults.py:1-46` does not need changes (no constants for
bucket names; they are env-derived).

### 2.8 Tests (`tests/test_pr_g_gcs_storage.py`, ≥5 tests)

1. **`test_storage_tf_defines_recordings_and_tmp`** — parse `terraform/storage.tf`,
   assert `google_storage_bucket.recordings` exists with `versioning.enabled=true`,
   `uniform_bucket_level_access=true`; assert `google_storage_bucket.tmp` exists
   with `lifecycle_rule` `age=7` + action `Delete` and `versioning.enabled=false`.
2. **`test_outputs_expose_bucket_names`** — `terraform/outputs.tf` grep asserts
   `output "recordings_bucket_name"` and `output "tmp_bucket_name"` present,
   each `value = google_storage_bucket.<x>.name`.
3. **`test_build_registry_includes_recordings_and_tmp`** — call
   `terraform_reconcile.build_registry(fake_config)`; assert two entries with
   `tf_address == "google_storage_bucket.recordings"` /
   `"google_storage_bucket.tmp"` and `import_id` matching `<env>-voipbin-<x>`.
4. **`test_k8s_substitution_uses_tf_outputs`** — patch `k8s._build_subs` (or its
   public equivalent) with `terraform_outputs={"recordings_bucket_name":"abc",
   "tmp_bucket_name":"xyz"}`; assert resulting map contains
   `PLACEHOLDER_RECORDINGS_BUCKET == "abc"` and `PLACEHOLDER_TMP_BUCKET == "xyz"`.
   Negative case: empty TF outputs + empty config → tokens render as `""`
   (no silent literal fallback).
5. **`test_field_map_populates_config`** — call
   `terraform_reconcile.outputs(config, {"recordings_bucket_name":"r-bkt",
   "tmp_bucket_name":"t-bkt"})`; assert `config.get("recordings_bucket")=="r-bkt"`
   and `config.get("tmp_bucket")=="t-bkt"`; second call with already-set values
   does not overwrite (covers operator-precedence path at terraform_reconcile.py:453).

Tests are pure-mock per PR-A pattern; no subprocess.

## 3. Out of scope

- Renaming `media` bucket to `${var.env}-voipbin-media` (back-compat; separate
  PR if needed).
- IAM bindings for SAs to the new buckets (assumed inherited from existing
  bucket-level IAM module; tracked under GAP-18 / PR-L).
- Preflight check that errors when `recordings_bucket`/`tmp_bucket` resolve
  to empty string at `k8s_apply` time (PR-K hardening).
- `dev/gen_backend_manifests.py` regeneration (PR-G only edits the rendered
  manifests; dev generator alignment is follow-up).
- Schema-level `enum`/`pattern` validation for bucket names (GCS naming rules
  are stricter than schema can express cheaply).

## 4. Risks & dependencies

- **PR-H merge dependency (HARD).** PR-H (k8s.py substitution-map refactor)
  edits the same `_build_subs` block (`scripts/k8s.py:60-110`) that PR-G
  rewrites at lines 74-76. PR-G must rebase onto PR-H after PR-H merges. If
  PR-G lands first, expect a 3-way conflict on the substitution dict literal.
  Coordinate via roadmap v3 §merge-order: **PR-H → PR-G**.
- **Bucket name collisions.** `${var.env}-voipbin-recordings` is globally
  unique to GCS. On dogfood reruns with the same `env`, terraform_apply will
  409; reconcile_imports (§2.3) handles import. First-time naming clashes with
  an externally owned bucket name (e.g. another tenant grabbed
  `dev-voipbin-recordings`) require operator to choose a different `env`.
- **Schema additive only.** `additionalProperties: False` at schema.py:86 means
  any `config.yaml` written with the new fields by reconcile_outputs would fail
  validation on installer versions older than PR-G. Mitigation: PR-G is the
  PR that adds the fields and the writer simultaneously; older installers do
  not read newer config.yaml.
- **Silent-fallback removal regression.** Operators on `--stage k8s_apply`
  without a prior reconcile_outputs run will now see empty
  `RECORDING_BUCKET_NAME` in the configmap (instead of a stale literal).
  Surface this in §6 smoke; long-term fix is a preflight error (PR-K).
- **`config.get('env')` in reconcile registry.** §2.3 assumes `env` is present
  in `InstallerConfig`. Verified usage exists (storage.tf uses `var.env`,
  variables.tf:86 declares it, wizard.py prompts for it). If absent, fall back
  to `var.project_id` is **wrong** for bucket naming — fail-loud preferred.
- **`FIELD_MAP` first real entries.** Prior PRs left this list empty;
  test_pipeline_reconcile_split tests assumed empty-FIELD_MAP no-op path. PR-G
  flips it; update any cross-PR test that asserted `FIELD_MAP == []`.

## 5. Test plan summary

5 new tests under `tests/test_pr_g_gcs_storage.py` (§2.8) + adjustments to
`tests/test_terraform_reconcile.py` (bucket count assertion if any) and
`tests/test_k8s.py:41` (rename `recording_bucket_name` →
`recordings_bucket_name` in fixture; switch placeholder key).
Existing test counts: ~376 → target ~381+.

## 6. Smoke dogfood (post-merge)

On `voipbin-install-dev`:

1. `voipbin-install apply` end-to-end (reconcile_imports → apply →
   reconcile_outputs → ansible_run → k8s_apply).
2. `gcloud storage buckets describe gs://dev-voipbin-recordings` → succeeds,
   versioning enabled.
3. `gcloud storage buckets describe gs://dev-voipbin-tmp` → succeeds,
   lifecycle rule age=7 + Delete.
4. `cat .voipbin-state/config.yaml` → contains `recordings_bucket:
   dev-voipbin-recordings` and `tmp_bucket: dev-voipbin-tmp`.
5. `kubectl -n voip get cm voip-config -o yaml` → `RECORDING_BUCKET_NAME:
   dev-voipbin-recordings` (no `PLACEHOLDER_*` left).
6. `kubectl -n backend get secret voipbin-secret -o jsonpath='{.data.GCP_BUCKET_NAME_TMP}'
   | base64 -d` → `dev-voipbin-tmp`.
7. Resume test: `terraform state rm google_storage_bucket.recordings`,
   re-run `voipbin-install apply --stage reconcile_imports` → reports import
   prompt → import succeeds → apply is a no-op.
8. No destroy. ~25 min.

## 7. Checklist

- [x] §2.1 storage.tf bucket resources (regional, UBLA, versioning on
      recordings, 7-day lifecycle on tmp)
- [x] §2.2 outputs (recordings_bucket_name, tmp_bucket_name)
- [x] §2.3 build_registry entries (import format `<bucket_name>`)
- [x] §2.4 FIELD_MAP entries (recordings_bucket_name→config.recordings_bucket,
      tmp_bucket_name→config.tmp_bucket)
- [x] §2.5 k8s.py:74 silent-fallback replaced by explicit
      PLACEHOLDER_RECORDINGS_BUCKET / PLACEHOLDER_TMP_BUCKET tokens
- [x] §2.6 manifest token rename in configmap.yaml + secret.yaml
- [x] §2.7 schema.py adds recordings_bucket + tmp_bucket fields
- [x] §2.8 ≥5 tests
- [x] §4 documents PR-H merge dependency (HARD, k8s.py conflict)
- [ ] Design review iter 1
- [ ] Design review iter 2
