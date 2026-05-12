```
          ████████
   ██████████████████████    __     __   ___ ____  ____  _
  ██                    ██   \ \   / /__|_ _|  _ \| __ )(_)_ __
 ██████████████████████████   \ \ / / _ \| || |_) |  _ \| | '_ \
 ██                      ██    \ V / (_) | ||  __/| |_) | | | | |
  ██    ██   ██   ██    ██      \_/ \___/___|_|   |____/|_|_| |_|
  ██    ██   ██   ██    ██          Connect & Collaborate for all
  ██    ██   ██   ██    ██              I N S T A L L E R
  ██    ██   ██   ██    ██
   ██   ██   ██   ██   ██
   ██████████████████████
```

# VoIPBin Installer

One-command deployment of the VoIPBin CPaaS platform to the cloud.

VoIPBin is a Communications Platform as a Service (CPaaS) built on 31 Go
microservices, SIP/RTP VoIP infrastructure (Kamailio, RTPEngine, Asterisk),
Cloud SQL, ClickHouse, Redis, RabbitMQ, and three frontend web applications.
This installer provisions all cloud infrastructure, configures VoIP VMs, and
deploys the full Kubernetes workload through a single CLI.

**Currently supported:** Google Cloud Platform (GCP)
**Planned:** AWS, Azure, and more


## Architecture

```
                           voipbin-install CLI
                                  |
                 +----------------+----------------+
                 |                |                |
            [1] Terraform   [2] Ansible      [3] Kubernetes
                 |                |                |
         GCP Infrastructure   VM Config       K8s Workloads
                 |                |                |
    +------------+------+    +---+---+    +-------+--------+
    |    |    |    |    |    |       |    |       |        |
   VPC  GKE  SQL  DNS  LB  Kam.  RTPe  Backend  VoIP  Frontend
         |                               (31)    (3)     (3)
      Node Pool                        services Asterisk  apps
      (2 nodes)                                instances
```

```
                         +-----------------------+
                         |      GCP Project      |
                         +-----------------------+
                         |                       |
              +----------+----------+            |
              |     VPC Network     |            |
              |    10.0.0.0/16      |       Cloud DNS
              +----------+----------+       (optional)
                         |
          +--------------+--------------+--------------+
          |              |              |              |
     +----+----+   +----+----+   +----+----+   +------+------+
     |   GKE   |   |Kamailio |   |RTPEngine|   |  Cloud SQL  |
     | Cluster |   |  VMs(2) |   |  VMs(2) |   | MySQL 8.0   |
     +---------+   +---------+   +---------+   +-------------+
     | Pods:   |   | SIP/TLS |   | RTP     |   | voipbin db  |
     | - 31    |   | WSS     |   | Media   |   | Auto-backup |
     |  backend|   | Ext. LB |   | Ext. IP |   +-------------+
     | - 3     |   | Int. LB |   | per VM  |
     |  Asterisk   +---------+   +---------+
     | - 3     |
     |  frontend   Cloud NAT ---- Static IP
     | - infra |        |
     |  (Redis,|   All private nodes route
     |   RMQ,  |   outbound via NAT gateway
     |   CH,   |
     |   proxy)|
     +---------+
```


## Pipeline stages

`voipbin-install apply` runs six stages in order:

1. **terraform_init** — initialize Terraform backend + providers.
2. **reconcile_imports** — detect GCP resources outside Terraform state and import them. Prevents 409 conflicts on resume.
3. **terraform_apply** — provision/update GCP infrastructure.
4. **reconcile_outputs** — read Terraform outputs, auto-populate select config.yaml fields (e.g. private IPs).
5. **ansible_run** — configure VoIP VMs.
6. **k8s_apply** — deploy Kubernetes workloads.

Run individual stages via `voipbin-install apply --stage <name>`.


## Warnings

**This installer creates real GCP resources that cost money.** Estimated
~$182/mo (zonal) or ~$255/mo (regional) in `us-central1`. Costs vary by
region. See [Cost Estimates](#cost-estimates) for a full breakdown. You are
responsible for all charges incurred on your GCP project.

**`destroy` is irreversible.** Running `./voipbin-install destroy` permanently
deletes all infrastructure including the Cloud SQL database and its backups.
Export any data you need before destroying.

**DNS delegation is required.** If you choose `dns_mode: auto`, the installer
creates a Cloud DNS zone but you must manually update your domain registrar's
NS records to point to the GCP name servers. DNS propagation can take up to
48 hours.

**Secrets are encrypted with GCP KMS.** If you lose access to the KMS key ring
or delete the GCP project, the encrypted `secrets.yaml` becomes unrecoverable.
Keep a decrypted backup in a secure location.

**Default VM sizing is minimal.** The default `f1-micro` instances for Kamailio
and RTPEngine are suitable for testing and low-traffic deployments only.
Production workloads should use `e2-medium` or larger.

**New GCP projects may need quota increases.** The installer requires at least
12 vCPUs and 10 external IPs. New projects default to 8 vCPUs — you may need
to request a quota increase before deploying.


## Prerequisites

### Required Tools

| Tool | Minimum Version | Install |
|------|----------------|---------|
| gcloud CLI | >= 400.0.0 | https://cloud.google.com/sdk/docs/install |
| terraform | >= 1.5.0 | https://developer.hashicorp.com/terraform/downloads |
| ansible | >= 2.15.0 | `pip install ansible` |
| kubectl | >= 1.28.0 | https://kubernetes.io/docs/tasks/tools/ |
| python3 | >= 3.10.0 | https://www.python.org/downloads/ |
| sops | >= 3.7.0 | https://github.com/getsops/sops/releases |

### GCP Account Requirements

- A GCP account with **billing enabled**
- A GCP project (or create a new one — the installer validates access and billing)
- A domain name you control (for DNS records and TLS certificates)
- The authenticated gcloud user must have **Owner** or **Editor** role on the
  project, or at minimum the following 12 IAM roles:

| IAM Role | Purpose |
|----------|---------|
| `roles/compute.admin` | VPC, VMs, firewalls, load balancers, NAT |
| `roles/container.admin` | GKE cluster and node pool |
| `roles/cloudsql.admin` | Cloud SQL instance |
| `roles/dns.admin` | Cloud DNS zone and records |
| `roles/cloudkms.admin` | KMS key ring for SOPS encryption |
| `roles/secretmanager.admin` | Secret Manager (if used) |
| `roles/iam.serviceAccountAdmin` | Create per-resource service accounts |
| `roles/iam.serviceAccountUser` | Assign SAs to VMs and GKE |
| `roles/resourcemanager.projectIamAdmin` | Bind IAM roles to service accounts |
| `roles/storage.admin` | GCS buckets (Terraform state, media) |
| `roles/serviceusage.serviceUsageAdmin` | Enable GCP APIs |
| `roles/iap.tunnelResourceAccessor` | SSH to VMs through IAP tunnel |

### GCP Quota Requirements

The installer checks these quotas during `init` and warns if insufficient:

| Quota | Minimum Required | Default (New Project) | Notes |
|-------|-----------------|----------------------|-------|
| vCPUs (region) | 12 | 8 | 2x GKE n1-standard-2 + 4 VMs |
| In-use external IPs | 10 | 8 | NAT, LB, RTPEngine static IPs |
| Static external IPs | 4 | 8 | Usually sufficient |
| SSD total (GB) | 100 | 500 | Usually sufficient |

Request quota increases at: https://console.cloud.google.com/iam-admin/quotas

### GCP APIs (auto-enabled)

The `init` command automatically enables these 16 APIs on your project:

`compute`, `container`, `sqladmin`, `dns`, `cloudkms`, `secretmanager`,
`cloudresourcemanager`, `iam`, `servicenetworking`, `storage`, `storage-api`,
`logging`, `monitoring`, `oslogin`, `serviceusage`, `iap`


## Quick Start

```bash
# Clone the repository
git clone git@github.com:voipbin/install.git
cd install

# Install Python dependencies
pip install -r requirements.txt

# Step 1: Initialize configuration (interactive wizard)
./voipbin-install init

# Step 2: Deploy infrastructure and services
./voipbin-install apply

# Step 3: Check deployment health
./voipbin-install verify
```

The `init` wizard asks 7 questions: GCP project ID, region, GKE cluster type,
TLS strategy, Docker image tag strategy, domain name, and Cloud DNS mode.


## TLS Strategy

The installer ships with two TLS strategies:

- **`self-signed` (default).** On first `apply`, the installer generates a
  10-year self-signed RSA-2048 certificate in memory and stores it in
  `voipbin-secret.SSL_CERT_BASE64` / `SSL_PRIVKEY_BASE64` (consumed by
  `api-manager` and `hook-manager` Pods as env-vars), and as a
  `kubernetes.io/tls` Secret named `voipbin-tls` in both `bin-manager` and
  `square-manager` namespaces (consumed by frontend nginx-tls sidecars).
  Browsers will reject the cert with `NET::ERR_CERT_AUTHORITY_INVALID` until
  the operator either accepts it manually or replaces it. **Replace before
  exposing production traffic.**
- **`byoc` (Bring Your Own Cert).** Operator pre-creates the same Secret
  set with a real CA-issued cert before the `k8s_apply` stage. The bootstrap
  function detects populated SSL keys and skips its own writes. See
  [BYOC Mode](#byoc-mode) below.

After install, verify the cert chain is production-grade with:

```bash
./voipbin-install verify --check=tls_cert_is_production
```

The check inspects all three cert sources (two `voipbin-tls` Secrets +
`voipbin-secret.SSL_CERT_BASE64`) and fails if any source still serves the
installer's placeholder cert (Subject CN = `voipbin-self-signed`).


## DNS Records

VoIPBin requires 5 DNS A records, each pointing at the reserved static IP
for its corresponding Service LoadBalancer. After `apply` completes,
`./voipbin-install verify` prints the actual IPs assigned. Example:

| Subdomain        | Service                | Notes                          |
|------------------|------------------------|--------------------------------|
| `api.<domain>`   | `api-manager` LB       | REST + WebSocket API           |
| `hook.<domain>`  | `hook-manager` LB      | Webhook ingress (HTTP+HTTPS)   |
| `admin.<domain>` | `admin` LB (frontend)  | Tenant admin SPA               |
| `talk.<domain>`  | `talk` LB (frontend)   | Agent talk SPA                 |
| `meet.<domain>`  | `meet` LB (frontend)   | Audio conference SPA           |

If you chose `dns_mode: auto`, the installer creates the Cloud DNS zone and
the records automatically; delegate your domain's NS records to GCP. If
`dns_mode: manual`, create these A records at your registrar.


## Production Cert Replacement

The default `self-signed` cert is for bring-up only. Before serving
production traffic, replace it with a CA-issued cert. The procedure
updates THREE Secret sources; running `verify --check=tls_cert_is_production`
afterward confirms all three are consistent.

```bash
# macOS note: 'base64 -w0' below is GNU. On macOS, replace with
# 'base64 -i <file> | tr -d "\n"' or install coreutils.

# 1. Place CA-issued cert and key files locally.
#    e.g., /tmp/voipbin.crt + /tmp/voipbin.key

# 2. Replace the bin-manager Pod-level cert (api-manager, hook-manager
#    read cert/key from voipbin-secret as env-vars).
kubectl -n bin-manager patch secret voipbin-secret \
  --type=merge \
  -p "{\"data\":{\"SSL_CERT_BASE64\":\"$(base64 -w0 /tmp/voipbin.crt)\",\"SSL_PRIVKEY_BASE64\":\"$(base64 -w0 /tmp/voipbin.key)\"}}"

# 3. Replace the bin-manager voipbin-tls Secret.
kubectl -n bin-manager create secret tls voipbin-tls \
  --cert=/tmp/voipbin.crt --key=/tmp/voipbin.key \
  --dry-run=client -o yaml | kubectl apply -f -

# 4. Replace the square-manager voipbin-tls Secret (frontend nginx
#    sidecars in admin/talk/meet).
kubectl -n square-manager create secret tls voipbin-tls \
  --cert=/tmp/voipbin.crt --key=/tmp/voipbin.key \
  --dry-run=client -o yaml | kubectl apply -f -

# 5. Roll the consumers so they pick up the new cert.
kubectl -n bin-manager rollout restart deployment/api-manager deployment/hook-manager
kubectl -n square-manager rollout restart deployment/admin deployment/talk deployment/meet

# 6. Run verify to confirm.
./voipbin-install verify --check=tls_cert_is_production
```


## BYOC Mode

Operator wants to serve a real cert from the very first install (no
self-signed phase). Run the install in stages, inject the Secrets between
`ansible_run` and `k8s_apply`:

```bash
# 1. Run init to write config.yaml + secrets.yaml.
./voipbin-install init
# Edit config.yaml: set tls_strategy: byoc

# 2. Provision infrastructure (everything before k8s_apply).
./voipbin-install apply --stage terraform_init
./voipbin-install apply --stage reconcile_imports
./voipbin-install apply --stage terraform_apply
./voipbin-install apply --stage reconcile_outputs
./voipbin-install apply --stage ansible_run

# 3. Create both namespaces.
kubectl create namespace bin-manager
kubectl create namespace square-manager

# 4. Create voipbin-secret with operator-supplied SSL keys.
#    The install-shipped k8s/backend/secret.yaml declares 6 non-SSL
#    keys (JWT_KEY etc.); k8s_apply will 3-way-merge those into this
#    Secret, preserving the SSL_*_BASE64 keys we set here.
kubectl -n bin-manager create secret generic voipbin-secret \
  --from-literal=SSL_CERT_BASE64=$(base64 -w0 /tmp/your.crt) \
  --from-literal=SSL_PRIVKEY_BASE64=$(base64 -w0 /tmp/your.key)

# 5. Create voipbin-tls Secret in BOTH namespaces.
for ns in bin-manager square-manager; do
  kubectl -n $ns create secret tls voipbin-tls \
    --cert=/tmp/your.crt --key=/tmp/your.key
done

# 6. Continue install. Bootstrap detects populated SSL keys and skips
#    its self-signed generation.
./voipbin-install apply --stage k8s_apply

# 7. Verify production cert is in place across all 3 sources.
./voipbin-install verify --check=tls_cert_is_production
```


## Commands

### init -- Initialize configuration

Runs the interactive setup wizard, validates the GCP project, enables required
APIs, creates a service account with IAM bindings, provisions a KMS key ring
for SOPS encryption, generates secrets, and writes `config.yaml` and an
encrypted `secrets.yaml`.

```bash
./voipbin-install init
./voipbin-install init --reconfigure          # Re-run wizard
./voipbin-install init --config path/to/config.yaml  # Import existing config
./voipbin-install init --skip-api-enable      # Skip GCP API enablement
./voipbin-install init --skip-quota-check     # Skip quota validation
```

### apply -- Deploy infrastructure and services

Provisions all GCP infrastructure with Terraform, configures VoIP VMs with
Ansible, and deploys Kubernetes workloads.

```bash
./voipbin-install apply
```

### destroy -- Tear down all resources

Removes all VoIPBin GCP resources created by `apply`.

```bash
./voipbin-install destroy
```

### status -- Show deployment status

Displays the current state of all VoIPBin components: Terraform resources,
VM health, GKE cluster, and Kubernetes workloads.

```bash
./voipbin-install status
```


## Configuration

The `init` command generates two files in the working directory:

### config.yaml (non-sensitive)

Contains all deployment parameters. Environment variables with the prefix
`VOIPBIN_` override values at runtime (e.g., `VOIPBIN_REGION=europe-west1`).

```yaml
gcp_project_id: my-project-123
region: us-central1
zone: us-central1-a
gke_type: zonal
tls_strategy: self-signed
image_tag_strategy: pinned
domain: voipbin.example.com
dns_mode: auto
gke_machine_type: n1-standard-2
gke_node_count: 2
vm_machine_type: f1-micro
kamailio_count: 1
rtpengine_count: 1
```

### secrets.yaml (SOPS-encrypted)

Contains sensitive values encrypted with GCP KMS via SOPS:

- `jwt_key` -- JWT signing key
- `cloudsql_password` -- Cloud SQL root password
- `redis_password` -- Redis authentication password
- `rabbitmq_user` / `rabbitmq_password` -- RabbitMQ credentials
- `api_signing_key` -- API request signing key

Decrypt manually with: `sops --decrypt secrets.yaml`


## Architecture Details

### Network

- Custom-mode VPC (`voipbin-vpc`) with a single subnet (`10.0.0.0/16`)
- GKE secondary ranges: pods `10.1.0.0/16`, services `10.2.0.0/20`
- Private GKE master endpoint with global access enabled
- Cloud NAT with a static external IP for outbound traffic
- Cloud Router for NAT gateway routing

### Firewall Rules

| Rule | Targets | Ports | Source |
|------|---------|-------|--------|
| SIP/TLS/WSS | kamailio | 443, 5060, 5061 (TCP/UDP) | 0.0.0.0/0 |
| RTP media | rtpengine | 20000-30000 (UDP) | Kamailio IPs + GKE pods |
| RTPEngine control | rtpengine | 22222 (TCP/UDP) | kamailio tag |
| IAP SSH | kamailio, rtpengine | 22 (TCP) | 35.235.240.0/20 |
| GKE internal | all | 6379, 5672 (TCP) | GKE pod CIDR |
| Health checks | kamailio | 5060 (TCP) | GCP health check ranges |
| Internal subnet | all | all TCP/UDP | 10.0.0.0/16 |

### Compute

**GKE Cluster**
- Zonal (free control plane) or regional (~$73/mo control plane, HA)
- 2x `n1-standard-2` nodes (default), auto-repair, auto-upgrade
- Private nodes, shielded instances, COS_CONTAINERD image
- REGULAR release channel

**Kamailio VM** (1x `f1-micro`)
- SIP proxy handling inbound/outbound SIP, TLS, and WebSocket Secure traffic
- No public IP -- external traffic arrives via network load balancer
- Configured via Ansible with Docker Compose

**RTPEngine VM** (1x `f1-micro`)
- RTP media relay with static external IP for direct media paths
- Configured via Ansible with Docker Compose

### Load Balancers

**External (Kubernetes Service LoadBalancer x 5)**
- Each external Service gets a dedicated reserved regional static IP
  (Terraform `google_compute_address`, naming `<service>-static-ip`).
- Services: `api-manager` (443), `hook-manager` (80+443,
  `externalTrafficPolicy: Local` for client-IP preservation),
  `admin`/`talk`/`meet` (443 via nginx-tls sidecar).
- TLS termination happens at the Pod (env-var cert for backends,
  nginx-tls sidecar for frontends), not at any in-cluster ingress
  controller; the installer does NOT deploy nginx-ingress or
  cert-manager.

**External (Kamailio)**
- Static external IP
- Forwarding rules: UDP 5060 (SIP), TCP 5060-5061 (SIP/TLS), TCP 443 (WSS)
- HTTP health check on port 5060

**Internal (Kamailio)**
- Static internal IP within the VPC subnet
- TCP backend service with health check
- Used by GKE pods and RTPEngine VMs to reach Kamailio

### Database

- Cloud SQL MySQL 8.0 (`db-f1-micro`)
- SSL required, daily automated backups with binary logging
- Sunday maintenance window at 04:00 UTC
- Auto-resize enabled, deletion protection on
- Cloud SQL Proxy deployed as a sidecar in GKE for secure access

### DNS (Cloud DNS)

When `dns_mode` is `auto`, the installer creates a managed zone and A
records pointing each subdomain at the reserved static IP of its
corresponding Service LoadBalancer:

| Record           | Target                                  |
|------------------|-----------------------------------------|
| `api.<domain>`   | `api-manager` LB static IP              |
| `hook.<domain>`  | `hook-manager` LB static IP             |
| `admin.<domain>` | `admin` (frontend) LB static IP         |
| `talk.<domain>`  | `talk` (frontend) LB static IP          |
| `meet.<domain>`  | `meet` (frontend) LB static IP          |
| `sip.<domain>`   | Kamailio external LB IP (SIP/WSS)       |

When `dns_mode` is `manual`, no DNS resources are created. The installer
prints the required records for you to configure with your registrar.

### Services

**Backend (31 microservices in `bin-manager` namespace)**

agent-manager, ai-manager, api-manager, billing-manager, call-manager,
campaign-manager, conference-manager, contact-manager, conversation-manager,
customer-manager, direct-manager, email-manager, flow-manager, hook-manager,
message-manager, number-manager, outdial-manager, pipecat-manager,
queue-manager, rag-manager, registrar-manager, route-manager,
sentinel-manager, storage-manager, tag-manager, talk-manager,
timeline-manager, transcribe-manager, transfer-manager, tts-manager,
webhook-manager

**VoIP (in `voip` namespace)**

- asterisk-call -- Handles live call media and applications
- asterisk-conference -- Conference bridge instances
- asterisk-registrar -- SIP registration for user agents

**Frontend (in `bin-manager` namespace)**

- square-admin -- Admin dashboard
- square-talk -- WebRTC calling interface
- square-meet -- Conference/meeting UI

### Infrastructure Services (in `infrastructure` namespace)

- Redis 7 (Alpine) -- Caching and pub/sub
- RabbitMQ 4.0 (Management, Alpine) -- Message broker
- ClickHouse 24.3 (Alpine) -- Analytics and event storage
- Cloud SQL Proxy -- Secure tunnel to Cloud SQL

### Kubernetes Namespaces

| Namespace | Pod Security | Contents |
|-----------|-------------|----------|
| `bin-manager` | baseline | Backend microservices, frontend apps |
| `infrastructure` | restricted | Redis, RabbitMQ, ClickHouse, Cloud SQL Proxy |
| `voip` | baseline | Asterisk instances |

### Network Policies

Default-deny ingress and egress policies are applied. Explicit allow rules
are defined for:
- `bin-manager` namespace service-to-service communication
- Infrastructure namespace access from `bin-manager` and `voip`
- VoIP namespace Asterisk-to-infrastructure connectivity


## Deployment Pipeline

The `apply` command executes a 3-stage pipeline:

```
Stage 1: Terraform           Stage 2: Ansible           Stage 3: Kubernetes
========================     ====================       =====================
 VPC + Subnet                 Kamailio VMs               Namespaces
 Cloud NAT + Router            - Docker install           Network Policies
 Firewall Rules                - Docker Compose           Infrastructure
 GKE Cluster + Node Pool      - Config templates           - Redis
 Kamailio VMs                 RTPEngine VMs                - RabbitMQ
 RTPEngine VMs                 - Docker install             - ClickHouse
 Cloud SQL Instance            - Docker Compose             - Cloud SQL Proxy
 Cloud DNS Zone                - Config templates          Backend Services (31)
 Load Balancers                                            VoIP (3 Asterisk)
 KMS Key Ring                                              Frontend (3 apps)
 GCS Buckets                                               Ingress + TLS
 Service Accounts                                          Database Migration
```

Terraform state is stored in a GCS bucket (`<project>-voipbin-tf-state`)
with versioning enabled (5 versions retained).


## Cost Estimates

Estimated monthly costs for a minimal deployment:

| Resource | Type | Cost/mo |
|----------|------|--------:|
| GKE Control Plane | 1 cluster | $0 (zonal) / ~$73 (regional) |
| GKE Nodes | 2x n1-standard-2 | ~$97 |
| Kamailio VM | 1x f1-micro | ~$6 |
| RTPEngine VM | 1x f1-micro | ~$6 |
| Cloud SQL | db-f1-micro MySQL | ~$13 |
| Cloud NAT | Gateway | ~$10 |
| External IPs | 2-3 static | ~$8 |
| Load Balancers | Network LB | ~$20 |
| Other | DNS, GCS, KMS, disks | ~$6 |
| **Total** | | **~$170 (zonal) / ~$243 (regional)** |

These estimates assume `us-central1`. Costs vary by region.


## Troubleshooting

See [docs/troubleshooting.md](docs/troubleshooting.md) for common issues
and solutions covering Terraform, Ansible, Kubernetes, Cloud SQL, DNS, and
SIP registration problems.


## Directory Structure

```
install/
|-- voipbin-install              # Bash entry point (delegates to scripts/cli.py)
|-- requirements.txt             # Python dependencies (rich, click, pyyaml, jsonschema)
|-- Makefile                     # install, lint, test, clean targets
|-- config/
|   |-- defaults.py              # Regions, GKE types, TLS strategies, sizing defaults
|   |-- schema.py                # JSON Schema for config.yaml validation
|   |-- gcp_apis.yaml            # 16 GCP APIs to enable
|   |-- gcp_iam_roles.yaml       # 12 IAM roles for installer service account
|   |-- gcp_quotas.yaml          # Minimum quota requirements (CPUs, IPs, SSD)
|   |-- versions.yaml            # Docker image tags (63 images)
|-- scripts/
|   |-- cli.py                   # Click CLI entry point with command registration
|   |-- config.py                # InstallerConfig class (load/save/validate/export)
|   |-- display.py               # Rich TUI helpers (banner, tables, prompts, progress)
|   |-- gcp.py                   # GCP operations (quotas, APIs, service accounts, KMS)
|   |-- k8s.py                   # K8s operations (kustomize render, placeholder substitution, apply)
|   |-- pipeline.py              # Deployment pipeline orchestrator with checkpoint/resume
|   |-- preflight.py             # Tool version checks and GCP auth validation
|   |-- secretmgr.py             # Secret generation and SOPS encryption
|   |-- terraform.py             # Terraform init/plan/apply/destroy/output + tfvars
|   |-- ansible_runner.py        # Ansible playbook execution via IAP tunnel
|   |-- utils.py                 # Shell commands, semver parsing, crypto helpers
|   |-- verify.py                # 10 health checks (GKE, pods, DNS, HTTP, SIP, etc.)
|   |-- wizard.py                # 7-question interactive setup wizard
|   |-- commands/
|       |-- init.py              # init command: wizard + preflight + GCP setup
|       |-- apply.py             # apply command: Terraform + Ansible + K8s deploy
|       |-- destroy.py           # destroy command: tear down all resources
|       |-- status.py            # status command: show deployment state
|       |-- verify.py            # verify command: run health checks with Rich output
|-- terraform/                   # 18 Terraform files
|   |-- apis.tf                  # GCP API enablement with propagation delay
|   |-- backend.tf               # GCS remote state backend
|   |-- cloudsql.tf              # Cloud SQL MySQL 8.0 instance + proxy SA
|   |-- dns.tf                   # Cloud DNS zone and A records
|   |-- firewall.tf              # 8 firewall rules (SIP, RTP, IAP, health checks)
|   |-- gke.tf                   # GKE cluster + node pool with shielded instances
|   |-- kamailio.tf              # Kamailio VM instances + service account
|   |-- kms.tf                   # KMS key ring + crypto key for SOPS
|   |-- loadbalancer.tf          # External + internal LBs for Kamailio
|   |-- nat.tf                   # Cloud Router + Cloud NAT with static IP
|   |-- network.tf               # VPC + subnet with GKE secondary ranges
|   |-- outputs.tf               # 18 outputs (IPs, names, connection strings)
|   |-- provider.tf              # Google + Google Beta providers
|   |-- rtpengine.tf             # RTPEngine VM instances + static external IPs
|   |-- service_accounts.tf      # Central SA reference (SAs defined per resource)
|   |-- storage.tf               # GCS buckets (Terraform state + media)
|   |-- variables.tf             # 11 input variables with validation
|   |-- versions.tf              # Terraform and provider version constraints
|-- ansible/                     # Ansible configuration for VoIP VMs
|   |-- ansible.cfg              # Ansible configuration
|   |-- group_vars/              # Variable files per host group
|   |-- inventory/               # Dynamic GCP inventory script
|   |-- playbooks/               # site.yml, kamailio.yml, rtpengine.yml
|   |-- roles/
|       |-- common/              # Shared VM setup tasks
|       |-- kamailio/            # Docker Compose + config templates
|       |-- rtpengine/           # Docker Compose + config templates
|-- k8s/                         # Kubernetes manifests
|   |-- namespaces.yaml          # bin-manager, square-manager, infrastructure, voip
|   |-- network-policies/        # Default-deny + per-namespace allow rules
|   |-- infrastructure/          # Redis, RabbitMQ, ClickHouse, Cloud SQL Proxy
|   |-- backend/                 # 31 microservice deployments + api-manager-internal Service
|   |-- voip/                    # 3 Asterisk deployments
|   |-- frontend/                # 3 frontend apps (square-manager ns) + nginx-tls sidecar
|   |-- database/                # Database migration job
|-- tests/                       # 103 unit tests
    |-- test_utils.py            # parse_semver, version_gte, generate_*
    |-- test_config.py           # InstallerConfig roundtrip, validation, export
    |-- test_wizard.py           # Domain/project validation, zone derivation
    |-- test_k8s.py              # K8s placeholder substitution map
    |-- test_pipeline.py         # Checkpoint save/load, stage ordering
    |-- test_terraform.py        # tfvars generation and content
    |-- test_verify.py           # Health check logic (GKE, pods, DNS, HTTP, SIP)
```


## DNS & Domain Configuration

After `voipbin-install apply` completes, configure DNS and verify your domain settings.

### DNS Records (at your registrar)

VoIPBin requires the following DNS A records. Each HTTPS subdomain points
at its own Service LoadBalancer static IP; `sip` points at the Kamailio
external LB.

| Subdomain         | Type | Target                            |
|-------------------|------|-----------------------------------|
| api.example.com   | A    | `api-manager` LB IP               |
| hook.example.com  | A    | `hook-manager` LB IP              |
| admin.example.com | A    | `admin` (frontend) LB IP          |
| talk.example.com  | A    | `talk` (frontend) LB IP           |
| meet.example.com  | A    | `meet` (frontend) LB IP           |
| sip.example.com   | A    | Kamailio external LB IP (SIP/WSS) |

Replace `example.com` with your domain. Actual IP values are printed by
`./voipbin-install verify` after `apply` completes.

For **auto DNS mode**: delegate your domain to the GCP nameservers printed after apply completes. GCP then manages the A records.

DNS propagation can take up to 48 hours. Once complete, run `voipbin-install verify`.

### Kubernetes ConfigMap

The following domain value is set in the `voipbin-config` ConfigMap (namespace: `bin-manager`) during deployment:

```
DOMAIN    example.com
```

If your backend services also need `DOMAIN_NAME_TRUNK` or `DOMAIN_NAME_EXTENSION`, add them to `k8s/backend/configmap.yaml` before running apply. Audit which services consume these values first.

### Kamailio VM Environment (`/opt/kamailio-docker/.env`)

Kamailio runs on GCE VMs via Docker Compose. The following domain-related env vars are written by the Ansible playbook:

```
BASE_DOMAIN              example.com
DOMAIN_NAME_EXTENSION    registrar.example.com
DOMAIN_NAME_TRUNK        trunk.example.com
```

RTPEngine has no domain-specific env vars.

To update these after deployment, re-run: `voipbin-install apply`


## Contributing

1. Install development dependencies: `pip install -r requirements.txt`
2. Run linting: `make lint`
3. Run tests: `make test`
4. Add tests in `tests/` for any new functionality
5. Follow the existing patterns in `scripts/` for new commands
