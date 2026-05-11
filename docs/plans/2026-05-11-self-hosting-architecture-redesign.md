# Self-hosting architecture redesign plan

**Status:** Draft (in review)
**Branch:** `NOJIRA-Self-hosting-architecture-redesign-plan`
**Author:** Hermes (CPO)
**Date:** 2026-05-11
**Scope:** Plan-only PR. No code changes. This document drives a multi-PR
implementation roadmap.

> Sensitive-data policy: this document is publicly viewable on the
> `voipbin/install` repository. It MUST NOT contain real production IPs,
> real GCP project IDs, real instance names, domain names, or any
> credential material. Where a real value would normally be cited, use a
> placeholder (`example.com`, `203.0.113.x` per RFC 5737, `<your-project>`,
> etc.). The repo's verification CI (or local grep) gates the merge.

## 1. Problem statement

`voipbin/install` is the official opensource installer for the VoIPBin
CPaaS platform. Today it ships a deployment model that does not match how
VoIPBin is actually run in the canonical production environment. Major
divergences:

1. **External exposure.** Install repo deploys `nginx-ingress` plus
   `cert-manager` (Let's Encrypt) in front of `ClusterIP` Services. The
   production environment uses `Service type=LoadBalancer` per externally
   exposed app (api-manager, hook-manager, admin, talk, meet), each with a
   reserved static IP, and **no ingress controller** in the cluster.
2. **TLS termination.** Install repo terminates TLS at `nginx-ingress` via
   `cert-manager`. Production terminates TLS upstream (operator-chosen
   CDN/reverse proxy) and Pods speak HTTP only. The cluster has no
   cert-manager and no managed-certificate resources.
3. **Namespaces.** Install repo creates 3 namespaces (`bin-manager`,
   `infrastructure`, `voip`). Production has 5 (also `square-manager` and
   `monitoring`).
4. **Service naming.** Install repo's frontend Services are named
   `square-admin`, `square-talk`, `square-meet`. Production uses `admin`,
   `talk`, `meet` (no `square-` prefix).
5. **Missing workloads.** Install repo does not ship the
   `monitoring-tests` CronJob, `number-renew` CronJob, the observability
   stack (Prometheus, Grafana, Alertmanager), or the SIP capture agent
   (Heplify).
6. **Cloud-SQL Proxy.** Install repo's `infrastructure/cloudsql-proxy` is
   not deployed in production; instead the cluster reaches Cloud SQL
   directly over the VPC private IP, with the proxy used (if at all) on
   the application side. Whether the proxy is needed for the
   default-install path is part of the redesign.
7. **Verify command.** The `voipbin-install verify` command was patched
   in PR #8 to query the three install-repo namespaces, but it does not
   currently exercise the production-parity surface (5 namespaces, the
   LoadBalancer external-reachability check, the static-IP/DNS pairing
   check, etc.).

The combined effect: a fresh `voipbin-install init && voipbin-install
deploy` produces a cluster that does not look like the documented
production reference, and the corresponding documentation under
`docs.voipbin` self-hosting pages are out of sync with both.

## 2. Goals

1. The install repo's default deployment model matches the **external
   exposure topology** that production actually uses (`Service
   type=LoadBalancer` per external app, no ingress controller, no
   cert-manager).
2. The install repo's **namespace and Service naming** match production
   exactly: `bin-manager`, `infrastructure`, `voip`, `square-manager`,
   `monitoring`; frontend Services renamed to `admin`, `talk`, `meet`.
3. TLS termination and DNS resolution are explicitly **out of scope** of
   the installer. The installer guides the operator to point DNS at the
   external Service IPs and choose their own TLS strategy
   (Cloudflare proxy, external reverse proxy, or self-installed
   cert-manager add-on). The README and docs surface this clearly.
4. Missing production workloads (`monitoring-tests` CronJob,
   `number-renew` CronJob, Prometheus/Grafana/Alertmanager, Heplify) are
   added so that a fresh install matches production parity.
5. The `voipbin-install verify` command exercises the new model: it
   reports per-Service external-IP assignment, DNS A-record status, and
   namespace-scoped health for all 5 namespaces.
6. The redesign is delivered as a series of small, independently
   reviewable PRs, each conforming to the org's design-first +
   review-loop policy (Design min 2, PR min 3).
7. No production secrets, IPs, domains, or project identifiers are
   committed to the repo.

## 3. Non-goals

- **Cloudflare integration.** Cloudflare is an optional layer the
  operator chooses; the installer does not configure it. The README and
  docs will mention it as one TLS option among several.
- **Multi-cloud abstraction.** The default install path remains GCP +
  GKE. Other clouds (AWS EKS, on-prem Kubernetes) may be supported in a
  later phase; this redesign does not pretend to be cloud-neutral.
- **Automatic DNS provisioning.** The operator is responsible for
  creating DNS A records pointing at the external Service IPs. The
  installer prints the required A records but does not call any DNS
  provider API.
- **Backwards-compatibility with the old ingress model.** Existing
  installs using `nginx-ingress + cert-manager` will need a one-time
  migration (documented in the rollout PR). No dual-mode shim.
- **Production migration.** This redesign brings the **installer** to
  production parity; it does not migrate the actual production cluster
  (production is already on the LoadBalancer model). Any cleanup of
  production-only oddities (dead `event-manager` Deployment, etc.) is
  tracked separately.
- **TLS automation.** The installer does not install cert-manager or
  configure Let's Encrypt. The operator's TLS choice is theirs.
- **Internal LoadBalancer for infra/voip services.** Decided in §5.6:
  `infrastructure` and `voip` Services stay `ClusterIP`. The
  `monitoring-tests` CronJob and any future cross-namespace caller talks
  to them via cluster DNS. Internal-LB-style annotations on Service
  resources are NOT in scope.

## 4. Target architecture (after redesign)

```
                Internet
                   |
                   |  (operator-managed DNS A record per host)
                   |
            +------+------+  +-------+  +------+  +------+
            v             v          v          v       v
        api-manager   hook-mgr    admin       talk    meet
        (bin-mgr)     (bin-mgr)  (square-)   (sq-)   (sq-)
        type=LB       type=LB    type=LB    type=LB  type=LB
        :443          :443/:80   :80/:443   :80/:443 :80/:443
            |             |          |          |       |
            v             v          v          v       v
              GKE cluster, Pods speak HTTP only

  Cluster-internal traffic:
    bin-manager -> infrastructure (Redis, RabbitMQ, ClickHouse) via ClusterIP
    bin-manager -> Cloud SQL (managed, reached over VPC private IP)
    bin-manager -> voip (Asterisk control) via ClusterIP
    PSTN traffic terminates on Kamailio/RTPEngine GCE VMs (out of cluster)
```

Namespaces (5):

| Namespace | Purpose | PSS profile |
|---|---|---|
| `bin-manager` | 32 backend microservice Deployments + `number-renew` CronJob | baseline |
| `square-manager` | `admin`, `talk`, `meet` frontend Deployments | baseline |
| `infrastructure` | Redis, RabbitMQ, ClickHouse, Prometheus, Grafana, Alertmanager, Heplify | restricted |
| `voip` | Asterisk call/conference/registrar | baseline |
| `monitoring` | `monitoring-tests` CronJob (api-validator + sip-validator) | restricted |

External LoadBalancer Services (5):

| Service | Namespace | Port(s) | Static IP annotation pattern |
|---|---|---|---|
| `api-manager` | bin-manager | 443 | `api-manager-static-ip` |
| `hook-manager` | bin-manager | 80, 443 | `hook-manager-static-ip` |
| `admin` | square-manager | 80, 443 | `admin-static-ip` |
| `talk` | square-manager | 80, 443 | `talk-static-ip` |
| `meet` | square-manager | 80, 443 | `meet-static-ip` |

The `kubernetes.io/ingress.global-static-ip-name` annotation references a
GCP reserved address that the operator creates via Terraform (see §5).

## 5. Key architectural decisions

Each decision lists the chosen option, the alternatives considered, and
the rationale. Operator-facing implications are flagged.

### 5.1 External exposure: LoadBalancer per Service

- **Chosen:** `Service type=LoadBalancer` per externally exposed app, one
  reserved GCP static IP per Service, referenced via the
  `kubernetes.io/ingress.global-static-ip-name` annotation.
- **Alternatives considered:**
  - `nginx-ingress + cert-manager` (current install repo): rejected,
    diverges from production and adds infrastructure the operator must
    learn.
  - GCE Ingress (L7) with GCP managed certificate: rejected, locks the
    operator into GCP-managed cert lifecycle and forces a single
    ingress IP for all hosts; production does not do this.
  - GKE Gateway API: deferred, not yet ubiquitous; can be a later
    migration target.
- **Rationale:** matches what production has run successfully for years.
  Simple L4 LB per Service, one reserved IP per host, operator points
  DNS at each IP, TLS is the operator's choice (Cloudflare, external
  reverse proxy, or a do-it-yourself cert-manager overlay).

### 5.2 TLS termination: out of installer scope

- **Chosen:** The installer does NOT terminate TLS. Pods listen on HTTP
  only (or, where Pods historically listened on `443` for
  compatibility, the LB forwards `443` to a Pod port that speaks plain
  HTTP). The README documents three operator-side options:
  1. Cloudflare proxy (Cloudflare-managed cert, "Flexible" or "Full"
     mode against the LB IP).
  2. External reverse proxy / WAF in front of each LB IP.
  3. Self-installed `cert-manager` plus a small ingress overlay that
     the operator manages outside the install repo's default flow.
- **Alternatives considered:**
  - Bundle cert-manager + Let's Encrypt as a default (Y-B / option T-1):
    rejected because it diverges from production and forces a TLS
    strategy on operators who may have their own.
  - Bundle GCP managed cert: rejected, GCP-only, requires GCE Ingress.
- **Rationale:** keeps the installer focused on Kubernetes workload
  deployment. TLS strategy is operator preference and a known
  divergence point across self-hosting scenarios. We document, we don't
  enforce.
- **Operator-facing implication:** without TLS, browsers will refuse to
  load the SPA over plain HTTP. The README MUST prominently warn that
  the operator has to choose and configure TLS before the install is
  usable in production. The `voipbin-install verify` command MUST also
  surface this as a `warn` (not a `fail`) on every install.

### 5.3 DNS provisioning: manual, installer reports targets

- **Chosen:** The operator creates DNS A records manually. The installer
  outputs a table after a successful deploy that lists each required
  hostname and the IP to point it at.
- **Alternatives considered:** automatic Cloudflare/Route53 DNS API
  calls (rejected, ties the installer to a provider) and `external-dns`
  controller (rejected, adds another component the operator must
  understand).
- **Rationale:** DNS provider diversity is too high to standardize on
  any one. The installer's job is to produce the IPs and tell the
  operator what records are needed.
- **Operator-facing implication:** the install is NOT browser-reachable
  until the operator creates A records (5 records: api, hook, admin,
  talk, meet) and propagates them. The verify command after PR #5
  reports DNS status as `warn` until records resolve to the LB IPs.

### 5.4 Static IP lifecycle: Terraform reserves, manifests reference

- **Chosen:** A new Terraform module reserves one
  `google_compute_address` per external-facing Service. The manifests
  reference the reserved address by name via the
  `kubernetes.io/ingress.global-static-ip-name` annotation. The
  installer reads the Terraform output and renders the annotation value
  into the manifests (via the existing config/template flow).
- **Alternatives considered:**
  - Manifests with hard-coded annotation values, operator pre-reserves
    addresses by name manually: rejected, brittle and undocumented.
  - Auto-create addresses via the installer using `gcloud`: rejected,
    splits IaC ownership between Terraform and the installer.
- **Rationale:** Terraform already owns other GCP resources (Cloud SQL,
  GKE cluster, VPC). Adding addresses there keeps the IaC surface
  single-tool.
- **Operator-facing implication:** the install reserves 5 regional
  static IPs (forwarding rules), each of which incurs a small recurring
  GCP charge. Default GCP per-region static-IP quota is finite; the
  installer's pre-flight check (added in PR #2) confirms quota is
  sufficient.

### 5.5 Namespaces and Service names

- **Chosen (namespaces):** `bin-manager`, `square-manager`,
  `infrastructure`, `voip`, `monitoring`.
- **Chosen (Service rename):** `square-admin` -> `admin`,
  `square-talk` -> `talk`, `square-meet` -> `meet`. Deployment names
  also renamed to match.
- **Alternatives considered:**
  - Keep three namespaces (current install) and the `square-` prefix:
    rejected, diverges from production and means existing operators
    coming from production references see different names.
  - Drop the `square-manager` namespace and keep frontends inside
    `bin-manager` with the `square-` prefix preserved: rejected, the
    namespace boundary in production exists for blast radius and PSS
    profile separation between backend and frontend.
  - Adopt new "neutral" names (`web-admin`, `web-talk`, `web-meet`):
    rejected, generates more churn than aligning with production.
- **Rationale:** the install repo is the public reference for
  self-hosting; if a self-hoster opens a production issue ("my
  `square-manager` namespace is empty") they should be looking at the
  same names the canonical operator sees. Convergence on production
  conventions reduces support load.
- **Operator-facing implication:** any existing operator's Kustomize
  overlays or scripts that reference `square-admin` etc. will break on
  upgrade. The rollout PR includes an explicit upgrade note. The verify
  command (after PR #5) will flag the old names if any old-style
  resources remain in the cluster.

### 5.6 Workloads to add (parity gaps)

- **Chosen additions:**
  - `bin-manager`: `number-renew` CronJob (production schedule pings
    the number-manager renewal endpoint daily).
  - `infrastructure`: Prometheus, Grafana, Alertmanager, Heplify (HEP
    capture for SIP debugging). Each as Deployment + Service. All
    Services stay `ClusterIP` (operator port-forwards or runs their
    own ingress overlay to reach Grafana etc.).
  - `monitoring`: new namespace + `monitoring-tests` CronJob with two
    containers (`api-validator` and `sip-validator`).
- **Chosen removal:** `cloudsql-proxy` Deployment from
  `infrastructure`. Operators have two documented choices: (a) Cloud
  SQL private-IP from the VPC (production default), or (b)
  operator-managed sidecar proxy in their own overlay.
- **Alternatives considered:**
  - Ship Prometheus stack as opt-in flag (default OFF) to keep the
    install minimal: rejected, would mean production parity is not the
    default which contradicts the redesign goal.
  - Keep `cloudsql-proxy` Deployment as default: rejected, production
    does not use it; bundling it forces an unnecessary Pod and
    misleads operators about the canonical wiring.
  - Heplify as opt-in (default OFF): listed as open question §12.3
    because the trade-off (parity vs minimal install) is genuinely a
    pchero decision.
- **Rationale:** production parity is the redesign goal. Workloads
  that production has been running for years are valuable defaults;
  operators who want to strip them can do so in their overlay.
- **Operator-facing implication:**
  - Recurring resource cost: adding Prom/Graf/Alert/Heplify Pods
    (roughly 4 more replicas at modest resource requests) increases
    the minimum cluster size needed.
  - `cloudsql-proxy` removal: operators upgrading from the current
    install whose workloads rely on the in-cluster proxy will lose
    DB connectivity until they migrate to Cloud SQL private IP. This
    is a breaking change, documented in the upgrade note.

### 5.7 Verify command rewrite

- **Chosen:** rewrite `scripts/verify.py` and
  `scripts/commands/verify.py` to:
  - Check all 5 namespaces for pods-ready and services-endpoints.
  - For each external Service in §4, check that the LoadBalancer has an
    external IP assigned (`status.loadBalancer.ingress[0].ip`) and that
    DNS resolves the configured host to that IP.
  - Emit a `warn` on every install that says "TLS is the operator's
    responsibility; LB IPs accept plain HTTP only".
- **Alternatives considered:** keep verify as-is; rejected, the verify
  surface diverges further from production after this redesign.
- **Operator-facing implication:** `voipbin-install verify` becomes
  the primary post-install diagnostic surface and the source of the
  DNS table operators copy/paste into their DNS provider. Output
  shifts from check-success/fail to check-success/warn/fail with TLS
  always at `warn` until the operator confirms.

### 5.8 Documentation surfaces

- **Install repo `README.md`:** rewritten to reflect the new model (no
  ingress, no cert-manager, manual DNS, operator-chosen TLS). Includes
  the post-deploy DNS table screenshot/example.
- **`docs.voipbin` self-hosting pages:** updated to match.
  This is a `voipbin/monorepo` change, sequenced after the install repo
  redesign lands.
- **`install/docs/dns-guide.md`:** rewritten around per-Service A
  records. Cloudflare optional callout added.
- **Operator-facing implication:** every existing self-hosting
  reference (README, dns-guide, public Sphinx pages) changes at once.
  Operators on the old model who upgrade without re-reading docs will
  hit a broken cluster. PR #3's commit body and the rollout note make
  this explicit.

## 6. Out-of-cluster components (audit summary)

These run as GCE VMs / managed services and are out of scope for the
install repo's Kubernetes manifests, but the redesign acknowledges them:

| Component | Where | Why out of cluster |
|---|---|---|
| Kamailio (SIP proxy) | GCE VM, separate LB IP for SIP | hostNetwork / SIP needs predictable source ports, easier on VM |
| RTPEngine | GCE VM(s) | hostNetwork, kernel module access |
| IPsec gateway | GCE VM | kernel-level |
| Cloud SQL (MySQL, Postgres) | Managed | not Kubernetes |

The install repo MAY provide Terraform for the VMs in a later phase;
this redesign does not address them.

## 7. Affected files (anticipated, PR-by-PR scope in §10)

This PR (plan-only + audit script) adds two files:

| File | Why |
|---|---|
| `docs/plans/2026-05-11-self-hosting-architecture-redesign.md` | This plan. |
| `scripts/dev/check-plan-sensitive.sh` | Re-runnable audit gate referenced in §9. Greps plan files for production IP, identifier, and domain patterns. Exits non-zero on any hit. |

Subsequent implementation PRs will touch (anticipated):

- `k8s/namespaces.yaml`
- `k8s/network-policies/*.yaml`
- `k8s/frontend/*.yaml` (renamed and moved)
- `k8s/backend/services/api-manager.yaml`, `hook-manager.yaml`
- `k8s/infrastructure/*` (additions for prom/graf/alert/heplify;
  removal of `cloudsql-proxy`)
- `k8s/voip/*` (verify Service types remain ClusterIP)
- `k8s/monitoring/*` (new directory)
- `k8s/ingress/*` (deletion)
- `k8s/kustomization.yaml`
- `terraform/static_addresses.tf` (new)
- `scripts/verify.py`, `scripts/commands/verify.py`
- `scripts/templates/*` (rendering of static-ip annotation values)
- `README.md`
- `docs/dns-guide.md`

## 8. Wire / API surface checklist

No external API, no SDK code samples, no OpenAPI changes in this
plan-only PR. Subsequent implementation PRs that change `verify` output
or installer CLI flags will carry their own checklists.

## 9. Sensitive-data audit (gate to merge)

Before submitting this PR, run the audit grep script at
`scripts/dev/check-plan-sensitive.sh` (introduced in this PR, PR #1).
The script greps the plan for patterns in three categories:

1. Public IP ranges known to be in use by the canonical production
   environment (regional GCP egress blocks).
2. Private IP ranges known to be in use by the canonical production VPC
   and GKE pod/service CIDRs.
3. Identifier patterns that match the canonical production project,
   cluster name, and Cloud SQL instance names. These are kept in a
   separate, non-committed file (`~/.voipbin/sensitive-patterns.txt`),
   not in this plan.

Expected: zero matches in all categories. The plan uses only RFC 5737
documentation IPs (`203.0.113.x`), placeholder domains
(`example.com`, `<your-domain>`), and generic identifier patterns
(`<your-project>`, `<name>-static-ip`).

The independent reviewer MUST re-run the script and flag any hit. The
reviewer is also reminded that placeholders in this plan
(`<your-project>`, etc.) are intentional and must NOT be replaced with
real values during review.

## 10. Rollout / split PR roadmap

Implementation will land as 7 small PRs (this plan PR plus PR #2,
3a, 3b, 4, 5, 6; PR #3 split into 3a/3b to preserve external
reachability during the transition). Each PR follows
design-first + review-loop policy (Design min 2, PR min 3).

| # | PR scope | Risk | Depends on |
|---|---|---|---|
| 1 | **Plan PR (this PR).** Adds the plan doc and the audit script `scripts/dev/check-plan-sensitive.sh`. No behavior change. | None | - |
| 2 | **Terraform static addresses + manifest annotation rendering + GCP quota pre-flight.** Reserves the 5 GCP static addresses, threads the resolved names into the install template flow, adds quota pre-flight to `voipbin-install init`. No Service type changes yet. | Low | 1 |
| 3a | **External Services to `type=LoadBalancer`, ingress controller still present.** `api-manager`, `hook-manager` (in `bin-manager`) and the existing `square-admin/talk/meet` (still in `bin-manager`, still with old names) flipped to LoadBalancer with static-ip annotation. nginx-ingress and cert-manager kept; their rules will still route, the LB IPs become an additional reachability path. This guarantees the cluster is externally reachable continuously through the transition. | Medium | 2 |
| 3b | **Ingress controller and cert-manager removed.** `k8s/ingress/` deleted. nginx-ingress + cert-manager helm/manifests removed. After 3a is in production for at least one cycle. | Medium | 3a |
| 4 | **Service rename + namespace move.** `square-admin/talk/meet` -> `admin/talk/meet` in `square-manager` namespace. `k8s/frontend/` moved/renamed. **Breaking change for existing operators' overlays.** | High | 3b |
| 5 | **Missing workloads added (`monitoring` ns + CronJob, `number-renew` CronJob, Prom/Graf/Alertmanager/Heplify in `infrastructure`). `verify.py` rewrite. README/dns-guide rewrite.** | Medium | 4 |
| 6 | **`cloudsql-proxy` removal (dedicated PR per §5.6).** Removes the `infrastructure/cloudsql-proxy` Deployment and Service. Operators upgrading must already have migrated DB access to Cloud SQL private IP (the rollout note in PR #5 prepares them). | Medium | 5 |

Estimated total: 7 design-first PRs over multiple sessions (including
this plan PR).

## 11. Risks

1. **Operator upgrade pain (PR #4).** Anyone with the old install gets
   broken DNS and broken Kustomize overlays. Mitigation: PR #4 ships a
   one-page upgrade guide, and the PR title/body call it out as
   breaking.
2. **TLS gap on first install.** A fresh install without operator TLS
   choice leaves the cluster reachable only over plain HTTP, which
   modern browsers will downgrade or warn on. Mitigation: README +
   verify warning + post-deploy table.
3. **Static-IP quota.** Each install reserves 5 regional static IPs.
   Default GCP per-region static-IP quota in some regions is finite
   and may need an explicit increase. Mitigation: PR #2 pre-flight
   quota check; document the requirement in the install guide.
4. **Recurring GCP forwarding-rule cost.** 5 forwarding rules per
   install plus the LBs they front add a recurring monthly charge.
   Mitigation: document the cost in the install README so operators
   can choose internal-LB-only or single-LB-multiplex variants in
   their overlay.
5. **`cloudsql-proxy` removal breaks existing installs.** Operators
   whose workloads point at the in-cluster proxy lose DB connectivity
   on upgrade. Mitigation: PR #6 is a dedicated removal PR sequenced
   after PR #5 documents the migration path; the verify command warns
   if the old Deployment is found in the cluster but the new wiring
   is not configured.
6. **`hook-manager` is a publicly-exposed unauthenticated webhook
   ingress by default.** Production exposes it on a public LB IP.
   Operators who do not front it with TLS / a reverse proxy are
   accepting unauthenticated webhook traffic on the public internet.
   Mitigation: README operator-facing warning; verify command flags
   `hook-manager` LB IP as `warn` until the operator confirms via
   config flag.
7. **WebSocket and long-lived connections under L4 LB idle timeout.**
   `talk` and `meet` rely on WebSocket / WebRTC signaling. GCP L4 LB
   idle timeout defaults to a value that can drop idle WebSocket
   connections. Mitigation: PR #4 sets the appropriate
   `cloud.google.com/backend-config` (or equivalent) annotation on
   the `talk` and `meet` Services; verify command checks for the
   annotation.
8. **SPA mixed-content / HSTS on raw LB IP.** Before the operator
   configures TLS, the SPA Pods serve over plain HTTP via the LB IP.
   Browsers attempting `https://admin.<your-domain>` against a
   plain-HTTP origin will fail. Mitigation: post-install README
   instructs the operator to validate over `http://` first (or
   configure TLS before any browser visit), and the verify command
   surfaces a `warn` until TLS is confirmed.
9. **Production drift not addressed here.** Production has historical
   workloads (dead `event-manager`, etc.) that we do not propagate
   to the install repo. Mitigation: explicit non-goal in §3 and
   per-PR scope discipline; cross-reference to the production audit
   captured separately.
10. **Sensitive-data leakage in plan doc.** Mitigation: §9 audit
    gate; PR #1 introduces the script enforcing it for future plans.

## 12. Open questions (for pchero to decide before PR #2 begins)

1. **Static IP naming convention.** The plan uses `<service>-static-ip`
   (matching the production annotation pattern). Confirm or alternative?
2. **GCP region for default install.** The plan stays region-neutral;
   the operator chooses. Confirm.
3. **Heplify in default install.** Production runs Heplify in
   `infrastructure`. Some operators may not need SIP packet capture.
   Default ON (parity) or OFF (smaller install)?
4. **`hook-manager` external exposure.** Production exposes hook-manager
   on a public LB IP. For self-hosters this means webhooks are
   internet-reachable; confirm this is the intended default.
5. **Public docs site sync.** After install repo
   redesign lands, do we update the monorepo RST pages in the same
   sprint, or schedule a follow-up?

## 13. Approval status

- [ ] Sensitive-data audit (§9) passes locally
- [ ] Design approved by independent reviewer (loop min 2)
- [ ] PR approved by independent reviewer (loop min 3)
- [ ] Merged by pchero (CEO/CTO)
- [ ] Open questions in §12 answered before PR #2 work begins
