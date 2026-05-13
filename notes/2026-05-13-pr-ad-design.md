# PR-AD — K8s image rendering via kustomize images: block (Design v2)

**Branch:** NOJIRA-PR-AD-k8s-image-kustomize
**Author:** Hermes (CPO) on behalf of pchero
**Date:** 2026-05-13
**Status:** v2 (addresses R1 CHANGES_REQUESTED)

## Problem

Verify iter#14 surfaced 0/31 bin-manager pods Ready, 0/3 voip pods Ready.
`kubectl describe pod` shows `ImagePullBackOff` for 13 hours on every workload.

Root cause split into two orthogonal axes:

**Axis A — image name prefix mismatch.** Install repo manifests reference
`voipbin/agent-manager` (no prefix). The Docker Hub `voipbin` org publishes
`voipbin/bin-agent-manager` (with `bin-` prefix). 31 of 39 image references
are affected (all bin-*-manager services). The remaining 8 references
(`bin-database`, 4× `voip-asterisk-*`, 3× `square-*`) happen to already
match the published names by coincidence.

**Axis B — image tag policy.** Install repo manifests carry no explicit tag,
which means Kubernetes defaults to `:latest`. Verified live (2026-05-13):
all 39 expected image names have a published `:latest` tag on Docker Hub.
Production monorepo CI pushes both `:latest` and `:$CIRCLE_SHA1` per
release. Self-hosted operators consume `:latest`; production consumes
SHA pins via `kustomize edit set image` in CircleCI.

## R1 blocker disposition (verified 2026-05-13)

| R1 # | Blocker | v2 disposition |
|---|---|---|
| 1 | Document `:latest` known limitations | ADDRESSED — new §"Known limitations of `:latest`" |
| 2 | Migrate all 7 "coincidentally matching" manifests | ADDRESSED — scope expanded to 39 manifests |
| 3 | Block merge on monorepo `:latest` push | **NOT APPLICABLE** — all 39 `:latest` tags verified published on Docker Hub today |
| 4 | Add M9, M10, M11 mutants | ADDRESSED — matrix grows from 8 → 11 |
| 5 | CI install kubectl as hard requirement | **NOT APPLICABLE** — install repo has no CI workflow (no `.github/workflows/`, no `.circleci/`); tests run locally via developer-installed kubectl |
| 6 | Confirm scripts/k8s.py does not regenerate kustomization.yaml | **VERIFIED safe** — `grep -rn "kustomization" scripts/` returns 0 hits; the only k8s rendering call is read-only `kubectl kustomize` (scripts/k8s.py:291) |

Audit also corrected the count: actual bin-*-manager manifests are **31**,
not 32 (chat-manager exists on Docker Hub but no install manifest). Total
in-scope: **39** (31 bin-*-manager + 1 bin-database + 4 voip-asterisk-* +
3 square-*).

## Decision (per pchero, 2026-05-13)

- **Tag policy: `:latest`** (option c in decision matrix).
- **Rendering mechanism: kustomize `images:` block** (option i,
  production-parity).
- Migrate ALL 39 image references to placeholder pattern (no carve-outs).
  Uniformity removes a permanent exception in the test suite and makes the
  SHA-pin escape hatch work identically for every service.

## Known limitations of `:latest` (R1 #1)

- **Tag mutability**: `:latest` is a moving target. Two operators applying
  the same install repo SHA a week apart get different pod binaries. Operators
  who need reproducibility must pin per-service SHA via the escape hatch
  documented in `docs/operations/image-overrides.md`.
- **`imagePullPolicy` defaulting**: Kubernetes defaults `imagePullPolicy` to
  `Always` when the tag is literally `:latest`, and `IfNotPresent` otherwise.
  This means an operator who follows the SHA-pin escape hatch experiences
  different pull semantics from the default. To make pull behavior invariant
  across both paths, this PR sets `imagePullPolicy: Always` explicitly on
  every Deployment container. Operators who do NOT want re-pull on every
  pod restart can override per-container; the default is correctness +
  freshness.
- **Inter-service version skew**: 31 bin-*-manager services updating
  asynchronously when their individual `:latest` tags get repushed by
  monorepo CI = guaranteed protocol skew during the rolling window. There
  is no version compatibility matrix between bin-managers today. Operators
  who need atomic upgrades should pin all 31 to the same SHA via the escape
  hatch and update them as a group. This is documented in
  `docs/operations/image-overrides.md` as the "atomic upgrade" recipe.

## Scope (what changes)

### 1. Manifest changes (39 files)

Replace literal `image: voipbin/<svc>-name` with placeholder `image: <svc>-image`,
and add explicit `imagePullPolicy: Always`:

```yaml
# Before
        - name: agent-manager
          image: voipbin/agent-manager
# After
        - name: agent-manager
          image: agent-manager-image
          imagePullPolicy: Always
```

Files in scope (frozen after audit):

- `k8s/backend/services/*.yaml` — **31** bin-*-manager manifests
- `k8s/database/migration-job.yaml` — 1 manifest (bin-database)
- `k8s/voip/asterisk-call/deployment.yaml` — voip-asterisk-call + voip-asterisk-proxy (2 containers)
- `k8s/voip/asterisk-conference/deployment.yaml` — voip-asterisk-conference + voip-asterisk-proxy
- `k8s/voip/asterisk-registrar/deployment.yaml` — voip-asterisk-registrar + voip-asterisk-proxy
- `k8s/frontend/admin.yaml`, `meet.yaml`, `talk.yaml` — 3 square-* manifests

Total: 31 + 1 + 4 + 3 = **39 distinct `image:` literals** across ~38 files
(voip-asterisk-* manifests contain 2 containers each so distinct counts and
file counts differ).

Note: ansible-deployed kamailio/rtpengine container images (`voipbin/voip-kamailio`,
`voipbin/voip-rtpengine`) live in `ansible/roles/*/templates/env.j2` and the
audit confirmed they already match published Docker Hub names. They are
**OUT OF SCOPE** for this PR — different rendering path (docker-compose
.env), different rendering mechanism (Jinja, not kustomize).

### 2. Top-level kustomization `images:` block

`k8s/kustomization.yaml` gets a new `images:` entry per workload (39 entries).
This is the **single source of truth** for image name + tag policy.
Operators who want to pin a specific SHA edit one entry; operators who want
to track `:latest` do nothing.

```yaml
images:
  - name: agent-manager-image
    newName: docker.io/voipbin/bin-agent-manager
    newTag: latest
  # ... 30 more bin-*-manager entries
  - name: bin-database-image
    newName: docker.io/voipbin/bin-database
    newTag: latest
  - name: voip-asterisk-call-image
    newName: docker.io/voipbin/voip-asterisk-call
    newTag: latest
  - name: voip-asterisk-conference-image
    newName: docker.io/voipbin/voip-asterisk-conference
    newTag: latest
  - name: voip-asterisk-registrar-image
    newName: docker.io/voipbin/voip-asterisk-registrar
    newTag: latest
  - name: voip-asterisk-proxy-image
    newName: docker.io/voipbin/voip-asterisk-proxy
    newTag: latest
  - name: square-admin-image
    newName: docker.io/voipbin/square-admin
    newTag: latest
  - name: square-meet-image
    newName: docker.io/voipbin/square-meet
    newTag: latest
  - name: square-talk-image
    newName: docker.io/voipbin/square-talk
    newTag: latest
```

### 3. Tests — `tests/test_pr_ad_k8s_image_rendering.py` (new)

Static + rendering parity assertions:

- **Walker count guard** (R1 #4 M9): `pathlib.Path("k8s").rglob("*.yaml")` with
  zero exclusions; assert the walker visits ≥ 38 files. Detects accidental
  glob narrowing in future refactors.
- **No literal `voipbin/` `image:` reference survives anywhere under `k8s/`.**
- **Every placeholder used in any manifest has exactly one matching
  `images:` entry** (collision direction A).
- **R1 #4 M11**: every Deployment container's `name:` field shares a prefix
  with its `image:` placeholder
  (e.g. `name: agent-manager` ↔ `image: agent-manager-image`).
  Detects two manifests accidentally sharing the same placeholder name.
- **`images:` block parity** against `kustomization.yaml`:
  - every `newName` starts with `docker.io/voipbin/`
  - every `newName` matches a known-published Docker Hub repository
    (allow-list embedded as a static snapshot in the test;
    `references/docker-hub-voipbin-images.json` for re-fetch)
  - every `newTag` is non-empty (R1 #4 M7)
- **R1 #4 M10 — rendered output sanity** via `kubectl kustomize k8s/`:
  - assert kubectl exits 0 (catches semantically broken kustomization.yaml
    that still parses as YAML)
  - parse the YAML stream, assert N Deployment objects (count guard against
    accidental subtree removal in kustomization.yaml)
  - assert every container's resolved `image:` matches
    `^docker\.io/voipbin/[a-z0-9-]+:latest$`
  - assert no literal placeholder (`-image$`) survives past rendering
- **kubectl presence**: hard requirement, not skip. If `shutil.which("kubectl")`
  is None, the test FAILS with a message instructing the developer to
  install kubectl. Aligned with the install repo policy that test gates
  are operator-machine-local, not CI-bound (Verified: install repo has no
  CI workflow).

### 4. Mutant matrix (programmatic, 11 mutations)

| ID | Mutation | Caught by |
|---|---|---|
| M1 | Drop one `images:` entry | rendered-output (placeholder leaks) |
| M2 | Wrong newName prefix (`voipbin/agent-manager` instead of `bin-`) | allow-list test |
| M3 | Wrong newTag (`stable` not `latest`) | tag-policy assertion |
| M4 | Typo placeholder in manifest (`agent-mgr-image`) | collision check (M11 dir) |
| M5 | Add literal `image: voipbin/foo` in a manifest | no-stray-literal |
| M6 | Drop `docker.io/` prefix in `images:` entry | registry-prefix assertion |
| M7 | Empty newTag | tag-non-empty |
| M8 | Orphan `images:` entry (no matching manifest placeholder) | orphan check |
| **M9** | Stray literal in a deep subdirectory the walker should still visit | walker count guard + no-stray-literal |
| **M10** | Kustomization.yaml semantically broken (newName mis-indented as sibling of images) | kubectl kustomize exit-code |
| **M11** | Two manifests share the same placeholder name | name/image prefix-match |

All 11 verified programmatically by harness
`scripts/dev/pr_ad_mutant_harness.py` — verbatim stdout in PR body.

### 5. Documentation

- **New: `docs/operations/image-overrides.md`** — covers:
  - default `:latest` policy + tracking monorepo CI
  - SHA-pin recipe (per-service)
  - atomic-upgrade recipe (pin all 31 bin-*-manager to one SHA)
  - production-parity note (monorepo CircleCI uses same mechanism with
    per-release SHA, no third-party vendor mentions)
- **README.md update**: add a short "Image policy" subsection (3-5 lines)
  linking to the operations doc, naming `:latest` as default + `imagePullPolicy: Always`
  as the explicit policy.

## Out of scope

- voip-asterisk-* / voip-kamailio* image references in ansible Jinja
  templates (different rendering path, names already match)
- voipbin-install init wizard `image_tag` knob (deferred to future PR;
  escape hatch via `k8s/kustomization.yaml` edit is sufficient short-term)
- Docker Hub allow-list automation at test time (static snapshot is the
  v2 choice; live fetch trades CI hermeticity for freshness)

## Production safety

- Production deploys are driven by monorepo CircleCI, not by
  `voipbin-install apply`. This PR only changes how install repo renders
  manifests.
- `scripts/k8s.py:291` invokes `kubectl kustomize` read-only and does not
  regenerate `k8s/kustomization.yaml` (verified: 0 `kustomization` references
  in scripts/). Operator SHA-pin edits survive re-apply.
- All 39 `:latest` tags verified published on Docker Hub at design time
  (2026-05-13).

## Verification gate

After merge, dogfood iter#15 in main install repo dir:

1. `./voipbin-install apply --auto-approve` → reaches k8s_apply stage.
2. `kubectl get pods -n bin-manager` → no `ImagePullBackOff` on any pod.
3. Pods enter Running or CrashLoopBackOff (CrashLoop due to RabbitMQ/MySQL
   not yet reachable is a DIFFERENT layer for PR-AE, not a PR-AD regression).
4. Static tests still 962+ passed.

If iter#15 reveals a new layer (CrashLoop, OOMKilled, ConfigMap missing),
PR-AD-1 is considered green and the new P0 enters its own PR cluster.

## Review checklist (v2)

- [x] R1 #1 — `:latest` limitations section added with `imagePullPolicy: Always` decision
- [x] R1 #2 — scope expanded to all 39 manifests (no carve-outs)
- [x] R1 #3 — verified-not-applicable (all `:latest` tags published; documented)
- [x] R1 #4 — mutants M9, M10, M11 added (matrix grows 8 → 11)
- [x] R1 #5 — verified-not-applicable (no CI; kubectl gate is operator-local hard requirement)
- [x] R1 #6 — verified safe (scripts/k8s.py does not regenerate kustomization.yaml)
- [x] Count corrected: 39 manifests in scope (was claimed 36)
- [x] README "Image policy" subsection planned

Ready for R2.
