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

`voipbin-install apply` runs eight stages in order:

1. **terraform_init** — initialize Terraform backend + providers.
2. **reconcile_imports** — detect GCP resources outside Terraform state and import them. Prevents 409 conflicts on resume.
3. **terraform_apply** — provision/update GCP infrastructure.
4. **reconcile_outputs** — read Terraform outputs, auto-populate select config.yaml fields (e.g. private IPs).
5. **k8s_apply** — deploy Kubernetes workloads.
6. **reconcile_k8s_outputs** — read Kubernetes load balancer IPs into config.yaml.
7. **cert_provision** — issue Kamailio TLS certificates.
8. **ansible_run** — configure Kamailio and RTPEngine VMs.

Run individual stages via `voipbin-install apply --stage <name>`.


## Warnings

**This installer creates real GCP resources that cost money.** Estimated
~$170/mo (zonal) or ~$243/mo (regional) in `us-central1`. Costs vary by
region. See [Cost Estimates](#cost-estimates) for a full breakdown. You are
responsible for all charges incurred on your GCP project.

**`destroy` is irreversible.** Running `./voipbin-install destroy` permanently
deletes all infrastructure including the Cloud SQL database and its backups.
Export any data you need before destroying.

**deletion_protection is disabled by default.** Cloud SQL instances and the
GKE cluster are deployed with `deletion_protection = false` to support the
`./voipbin-install destroy` workflow. Production operators who want to prevent
accidental deletion can set `deletion_protection = true` via the GCP Console
or `gcloud` after initial deployment — Terraform will not revert this change
on subsequent applies (protected by `lifecycle { ignore_changes }`).

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
  project, or at minimum the following 13 IAM roles:

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
| `roles/compute.osLogin` | SSH to Kamailio/RTPEngine VMs as a non-sudo user via OS Login |
| `roles/compute.osAdminLogin` | SSH and become root on Kamailio/RTPEngine VMs (Ansible needs sudo) |

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
git clone https://github.com/voipbin/install.git
cd install

# Install Python dependencies
pip install -r requirements.txt
```

Then follow the three-step workflow below.


## Step-by-Step Usage Guide

### Step 1 — Initialize configuration

Before running `init`, authenticate with GCP:

```bash
gcloud auth login
gcloud auth application-default login
```

Then run the installer wizard:

```bash
./voipbin-install init
```

The interactive wizard prompts for:

| Question | Options / Example |
|----------|------------------|
| GCP project ID | `my-voipbin-project` |
| Region | `us-central1` |
| GKE cluster type | `zonal` (cheaper) or `regional` (HA) |
| TLS strategy | `self-signed` (auto-managed) or `byoc` (Bring Your Own Cert) |
| Docker image tag strategy | `latest` or `pinned` (fixed SHA from versions.yaml) |
| Domain name | `voipbin.example.com` |
| Kamailio cert mode | `self_signed` (auto) or `manual` (supply cert files yourself) |
| Cloud DNS mode | `auto` (GCP manages DNS) or `manual` |

After `init` completes, two files are created in the working directory:
- **`config.yaml`** — non-sensitive configuration (safe to commit)
- **`secrets.yaml`** — SOPS-encrypted secrets (safe to commit; requires KMS to decrypt)

**Re-run wizard to change settings:**
```bash
./voipbin-install init --reconfigure
```

**Preview what init would do without making changes:**
```bash
./voipbin-install init --dry-run
```


### Step 2 — Deploy infrastructure

```bash
./voipbin-install apply
```

This runs the full 8-stage deployment pipeline:

| Stage | What it does |
|-------|-------------|
| `terraform_init` | Initialize Terraform backend (GCS state bucket) |
| `reconcile_imports` | Import any pre-existing GCP resources into Terraform state |
| `terraform_apply` | Provision VPC, GKE cluster, Cloud SQL, Kamailio/RTPEngine VMs |
| `reconcile_outputs` | Read Terraform outputs (IPs, connection names) into `config.yaml` |
| `k8s_apply` | Deploy VoIPBin services to the GKE cluster |
| `reconcile_k8s_outputs` | Read Kubernetes load balancer IPs into `config.yaml` |
| `cert_provision` | Issue Kamailio TLS certificates |
| `ansible_run` | Configure Kamailio and RTPEngine VMs |

**The pipeline is resumable.** If a stage fails, fix the issue and re-run
`apply` — it continues from where it left off.

**Skip confirmation prompts (for CI/CD automation):**
```bash
./voipbin-install apply --auto-approve
```

**Preview the Terraform plan without applying:**
```bash
./voipbin-install apply --dry-run
```

**Re-run only a specific stage:**
```bash
./voipbin-install apply --stage ansible_run
```

**After `apply` completes**, configure DNS for your domain. The output shows
the IP addresses and records to create:

```
DNS Records
  api.voipbin.example.com     A    <load-balancer-ip>
  hook.voipbin.example.com    A    <load-balancer-ip>
  admin.voipbin.example.com   A    <load-balancer-ip>
  talk.voipbin.example.com    A    <load-balancer-ip>
  meet.voipbin.example.com    A    <load-balancer-ip>
  sip.voipbin.example.com     A    <sip-vm-ip>
```


### Step 3 — Verify the deployment

```bash
./voipbin-install verify
```

Runs health checks against the live deployment (API reachability, SIP
connectivity, pod readiness, TLS certificate validity). Run this after DNS
propagation completes.


### Check deployment status at any time

```bash
./voipbin-install status
```

Shows the current deployment state, Terraform resource count, GKE cluster
health, Kubernetes pod phases, and VM status.

```bash
./voipbin-install status --json    # Machine-readable output for scripting
```


### Tear down all resources

```bash
./voipbin-install destroy
```

Removes all GCP resources created by `apply` (VMs, GKE cluster, Cloud SQL,
VPC, etc.). **This is irreversible — export any data first.**

```bash
./voipbin-install destroy --auto-approve    # Skip confirmation (use with care)
```


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

> **Note:** The installer does not automate certificate issuance or renewal.
> See [Obtaining TLS Certificates](#obtaining-tls-certificates) for how to
> obtain a CA-issued cert and inject it into the cluster.

After install, verify the cert chain is production-grade with:

```bash
./voipbin-install verify --check=tls_cert_is_production
```

The check inspects all three cert sources (two `voipbin-tls` Secrets +
`voipbin-secret.SSL_CERT_BASE64`) and fails if any source still serves the
installer's placeholder cert (Subject CN = `voipbin-self-signed`).


## Obtaining TLS Certificates

VoIPBin requires TLS certificates from two independent systems:

| System | Config key | Default | Used by |
|--------|-----------|---------|---------|
| Frontend (admin, talk, meet, api, hook) | `tls_strategy` | `self-signed` (hyphen) | nginx sidecars, Go binaries |
| Kamailio SIP proxy | `cert_mode` | `self_signed` (underscore) | SIP/TLS, SIPS, WebRTC DTLS |

Both systems accept operator-supplied certificates. The installer does **not**
automate certificate issuance or renewal. Operators are responsible for
obtaining, renewing, and injecting certificates.

### Option 1 — Let's Encrypt (recommended for production)

Use [Certbot](https://certbot.eff.org/) to obtain a free, publicly trusted
certificate.

**Prerequisite:** All domain names passed to Certbot must resolve (via DNS A
records) to the machine running the standalone challenge before you run the
command. Create DNS records first (see [DNS Records](#dns-records)), wait for
propagation, then run Certbot.

**Standalone HTTP challenge** (requires port 80 reachable on the Certbot host):

```bash
certbot certonly --standalone \
  -d api.example.com \
  -d hook.example.com \
  -d admin.example.com \
  -d talk.example.com \
  -d meet.example.com
# Certs written to /etc/letsencrypt/live/api.example.com/
```

**DNS challenge** (no port 80 required; works with any DNS provider):

```bash
certbot certonly --manual \
  --preferred-challenges dns \
  -d "*.example.com" \
  -d example.com
# Follow prompts to add a TXT record at your registrar.
```

After issuance, follow the [Production Cert Replacement](#production-cert-replacement)
procedure to inject the cert into the cluster.

**Renewal:** Let's Encrypt certs expire after 90 days. Run `certbot renew`
before expiry, then re-run the replacement procedure to push the new cert
into the cluster. The installer does not automate this step.

### Option 2 — Commercial or self-managed CA

Purchase or issue a certificate from any CA. Obtain `fullchain.pem` (leaf +
intermediates) and `privkey.pem`. Then follow the
[Production Cert Replacement](#production-cert-replacement) or
[BYOC Mode](#byoc-mode) procedure.

### Kamailio certificate

Kamailio's cert is separate from the frontend cert. By default the installer
generates a self-signed CA and issues leaf certs for `sip.<domain>` and
`registrar.<domain>` (`cert_mode: self_signed`). SIP clients and WebRTC
browsers must trust this CA — export it with:

```bash
./voipbin-install cert export-ca --out ca.pem
```

To use a CA-issued cert for Kamailio instead, set `cert_mode: manual` in
`config.yaml` and provide per-SAN cert files under `cert_manual_dir`. See
[cert — Manage Kamailio TLS Certificates](#cert----manage-kamailio-tls-certificates).


## Image Policy

VoIPBin install renders all workload images through a kustomize `images:`
block in `k8s/kustomization.yaml`. The default is `:latest`, tracking the
monorepo CI's most recent build per service, with `imagePullPolicy: Always`
declared explicitly on every container. To pin a specific commit SHA (single
service) or upgrade all 31 bin-*-manager services atomically, see
[docs/operations/image-overrides.md](docs/operations/image-overrides.md).

## DNS Records

VoIPBin requires 6 DNS A records, each pointing at the reserved static IP
for its corresponding Service LoadBalancer. After `apply` completes,
`./voipbin-install verify` prints the actual IPs assigned. Example:

| Subdomain        | Service                | Notes                          |
|------------------|------------------------|--------------------------------|
| `api.<domain>`   | `api-manager` LB       | REST + WebSocket API           |
| `hook.<domain>`  | `hook-manager` LB      | Webhook ingress (HTTP+HTTPS)   |
| `admin.<domain>` | `admin` LB (frontend)  | Tenant admin SPA               |
| `talk.<domain>`  | `talk` LB (frontend)   | Agent talk SPA                 |
| `meet.<domain>`  | `meet` LB (frontend)   | Audio conference SPA           |
| `sip.<domain>`   | Kamailio external LB   | SIP/WSS traffic                |

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
`reconcile_outputs` and `k8s_apply`:

```bash
# 1. Run init to write config.yaml + secrets.yaml.
./voipbin-install init
# Edit config.yaml: set tls_strategy: byoc

# 2. Provision GCP infrastructure (stages 1–4).
./voipbin-install apply --stage terraform_init
./voipbin-install apply --stage reconcile_imports
./voipbin-install apply --stage terraform_apply
./voipbin-install apply --stage reconcile_outputs

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

# 6. Continue install (stages 5–8: k8s, cert, ansible).
./voipbin-install apply --stage k8s_apply
./voipbin-install apply --stage reconcile_k8s_outputs
./voipbin-install apply --stage cert_provision
./voipbin-install apply --stage ansible_run

# 7. Verify production cert is in place across all 3 sources.
./voipbin-install verify --check=tls_cert_is_production
```


## Commands

For detailed usage of each command, run `./voipbin-install <command> --help`.

### init -- Initialize configuration

Runs the interactive setup wizard, validates the GCP project, enables required
APIs, creates a service account with IAM bindings, provisions a KMS key ring
for SOPS encryption, generates secrets, and writes `config.yaml` and an
encrypted `secrets.yaml`.

```bash
./voipbin-install init                               # Interactive setup
./voipbin-install init --reconfigure                 # Re-run wizard to change settings
./voipbin-install init --config path/to/config.yaml # Import existing config
./voipbin-install init --dry-run                     # Preview without applying
./voipbin-install init --skip-api-enable             # Skip GCP API enablement
./voipbin-install init --skip-quota-check            # Skip quota validation
```

### apply -- Deploy infrastructure and services

Provisions all GCP infrastructure with Terraform, deploys Kubernetes workloads,
and configures VoIP VMs with Ansible. Runs 8 ordered stages. Resumable on failure.

```bash
./voipbin-install apply                              # Full deployment (interactive)
./voipbin-install apply --auto-approve               # Skip confirmation (CI/CD)
./voipbin-install apply --dry-run                    # Preview Terraform plan only
./voipbin-install apply --stage ansible_run          # Re-run a specific stage
```

### verify -- Check deployment health

Runs health checks against the live deployment. Run after `apply` and DNS propagation.

```bash
./voipbin-install verify                             # Run all checks
./voipbin-install verify --check http_health         # Run only the HTTP health check
```

### status -- Show deployment status

Shows the current state of all VoIPBin components. GKE/pod/VM details are only
shown when the deployment is in an active state.

```bash
./voipbin-install status                             # Human-readable output
./voipbin-install status --json                      # JSON output for scripting
```

### destroy -- Tear down all resources

Removes all VoIPBin GCP resources created by `apply`. **Irreversible.**

```bash
./voipbin-install destroy                            # Interactive (asks for confirmation)
./voipbin-install destroy --auto-approve             # Skip confirmation (use with care)
```

### cert -- Manage Kamailio TLS Certificates

Inspect, renew, and export the installer-managed Kamailio TLS certificates.

```bash
./voipbin-install cert status                        # Show per-SAN expiry and CA fingerprint
./voipbin-install cert status --json                 # JSON output
./voipbin-install cert renew                         # Re-run cert_provision stage
./voipbin-install cert renew --force                 # Clear cached state and force reissue
./voipbin-install cert export-ca                     # Print CA certificate to stdout (PEM)
./voipbin-install cert export-ca --out ca.pem        # Write to file
./voipbin-install cert export-ca --der --out ca.der  # DER format
./voipbin-install cert clean-staging                 # Remove temp cert-staging directory
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
| VM SSH | kamailio, rtpengine | 22 (TCP) | 0.0.0.0/0 (OS Login publickey-only) |
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

**Frontend (in `square-manager` namespace)**

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
| `bin-manager` | baseline | Backend microservices |
| `square-manager` | baseline | Frontend apps (admin, talk, meet) |
| `infrastructure` | restricted | Redis, RabbitMQ, ClickHouse, Cloud SQL Proxy |
| `voip` | baseline | Asterisk instances |

### Network Policies

Default-deny ingress and egress policies are applied. Explicit allow rules
are defined for:
- `bin-manager` namespace service-to-service communication
- Infrastructure namespace access from `bin-manager` and `voip`
- VoIP namespace Asterisk-to-infrastructure connectivity


## Deployment Pipeline

The `apply` command executes an 8-stage pipeline. See the [Pipeline stages](#pipeline-stages) section for the full ordered list. The stages map to these infrastructure concerns:

```
Terraform stages          Kubernetes stages          Ansible stages
=====================     ====================       =====================
 VPC + Subnet              Namespaces                 Kamailio VMs
 Cloud NAT + Router        Network Policies            - Docker install
 Firewall Rules            Infrastructure               - Docker Compose
 GKE Cluster + Node Pool    - Redis                    - Config templates
 Kamailio VMs               - RabbitMQ                RTPEngine VMs
 RTPEngine VMs              - ClickHouse               - Docker install
 Cloud SQL Instance         - Cloud SQL Proxy           - Docker Compose
 Cloud DNS Zone            Backend Services (31)        - Config templates
 Load Balancers            VoIP (3 Asterisk)
 KMS Key Ring              Frontend (3 apps)
 GCS Buckets               Ingress + TLS
 Service Accounts          Database Migration
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
|   |-- ansible_runner.py        # Ansible playbook execution (OS Login SSH)
|   |-- utils.py                 # Shell commands, semver parsing, crypto helpers
|   |-- verify.py                # 10 health checks (GKE, pods, DNS, HTTP, SIP, etc.)
|   |-- wizard.py                # 8-question interactive setup wizard
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
|   |-- inventory/               # Dynamic GCP inventory script
|   |   |-- gcp_inventory.py
|   |   |-- group_vars/          # Variable files per host group (auto-loaded by Ansible)
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
