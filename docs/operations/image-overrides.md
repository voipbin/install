# Image overrides

VoIPBin install renders all Kubernetes workload container images through a
single kustomize `images:` block in `k8s/kustomization.yaml`. The default
policy is `:latest`, which tracks the monorepo CI's most recent build per
service. Operators who need reproducible deployments or atomic upgrades
override on a per-service basis below.

## Default policy

- Every `Deployment` container references a placeholder image name (e.g.
  `agent-manager-image`).
- `k8s/kustomization.yaml` remaps each placeholder to
  `docker.io/voipbin/<repo>:latest`.
- Every container declares `imagePullPolicy: Always` so pull semantics stay
  invariant whether you use `:latest` or pin a SHA.

## Pin a single service to a specific SHA

Edit `k8s/kustomization.yaml` and change the `newTag` for that entry:

```yaml
images:
  - name: agent-manager-image
    newName: docker.io/voipbin/bin-agent-manager
    newTag: 54019032543dcbb487d39889951e5631eae61b2b   # <-- replace `latest`
```

Then re-apply:

```
./voipbin-install apply --auto-approve
```

`scripts/k8s.py` does not regenerate `k8s/kustomization.yaml`, so your
override survives every re-apply. (Static test:
`tests/test_pr_ad_k8s_image_rendering.py::test_every_newtag_is_latest`
will go red when your local edit lands; that is expected. Keep the edit
local; do not commit it.)

## Atomic upgrade across all 31 bin-*-manager services

The bin-*-manager services do not maintain a formal cross-service
compatibility matrix. Running mixed versions during a rolling upgrade can
trigger RPC protocol skew. For environments that need atomic upgrades:

1. Pick a known-good monorepo commit SHA (verified by your QA, or the most
   recent CircleCI release).
2. In `k8s/kustomization.yaml`, replace every `newTag: latest` under the
   `bin-*-manager-image` block with that SHA.
3. Re-apply.

Operators with stricter requirements can fork the install repo and commit
the SHA-pinned `kustomization.yaml` as their "release branch", then update
the branch in lockstep with their internal change-control process.

## Known limitations of `:latest`

- **Tag mutability.** Two operators applying the same install repo commit
  a week apart can get different container binaries because monorepo CI
  has repushed `:latest` in between. Pin per the recipe above for
  reproducibility.
- **`imagePullPolicy` interaction.** Kubernetes defaults `imagePullPolicy`
  to `Always` when the tag is literally `:latest` and `IfNotPresent`
  otherwise. PR-AD makes this invariant by declaring
  `imagePullPolicy: Always` on every container explicitly. If you want
  cached-pull semantics on a SHA-pin, change `imagePullPolicy` per
  container; this is per-Deployment surgery, not a kustomize knob.
- **Version skew.** Mixed bin-*-manager versions during rolling upgrades
  can fail RPC schema validation. Pin atomically per the recipe above.

## Production-parity note

Production VoIPBin deployments do not consume `voipbin-install`. They are
driven by the monorepo's CircleCI release pipeline, which uses the same
`images:` mechanism but resolves each placeholder to the per-release git
SHA via `kustomize edit set image <placeholder>=docker.io/voipbin/<repo>:$CIRCLE_SHA1`.
The install repo's `:latest` default and production's SHA-pin coexist
because both flow through the same kustomize `images:` block.

## Refreshing the Docker Hub allow-list snapshot

`tests/test_pr_ad_k8s_image_rendering.py` validates every `images:` entry
against a snapshot at `tests/fixtures/pr_ad_docker_hub_voipbin_snapshot.json`.
When a genuinely new `voipbin/*` repository is published (e.g. a new
service), refresh the snapshot:

```
curl -s "https://hub.docker.com/v2/repositories/voipbin/?page_size=100" \
  | jq '{captured_at: now|todate, source: input_filename, repos: [.results[].name] | sort}' \
  > tests/fixtures/pr_ad_docker_hub_voipbin_snapshot.json
```

Commit the refreshed snapshot alongside the manifest changes that consume
the new repo.
