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


## Documentation

Full installation guide, TLS configuration, BYOC mode, cert management,
and troubleshooting: https://docs.voipbin.net/self-hosting


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
| `roles/compute.osLogin` | SSH to Kamailio/RTPEngine VMs as a non-sudo user via OS Login |
| `roles/compute.osAdminLogin` | SSH and become root on Kamailio/RTPEngine VMs (Ansible needs sudo) |

### GCP Quota Requirements

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

Then follow the three-step workflow below. For a detailed walkthrough, see
https://docs.voipbin.net/self-hosting.


## Step-by-Step Usage

### Step 1 — Initialize configuration

```bash
gcloud auth login
gcloud auth application-default login
./voipbin-install init
```

The interactive wizard prompts for project ID, region, GKE type, TLS strategy,
domain name, DNS mode, and more. On completion it writes `config.yaml`
(non-sensitive, safe to commit) and `secrets.yaml` (SOPS-encrypted).

### Step 2 — Deploy infrastructure

```bash
./voipbin-install apply
```

Runs the full 8-stage pipeline (Terraform → K8s → cert → Ansible). The pipeline
is resumable — if a stage fails, fix the issue and re-run `apply`. Use
`--stage <name>` to re-run a single stage, `--auto-approve` for CI/CD,
`--dry-run` to preview the Terraform plan.

### Step 3 — Verify the deployment

```bash
./voipbin-install verify
```

Runs health checks (API reachability, SIP connectivity, pod readiness, TLS
validity). Run after DNS propagation completes.

For full step-by-step instructions, TLS setup, BYOC mode, DNS configuration,
and cert management, see https://docs.voipbin.net/self-hosting.


## Commands

| Command | Description |
|---------|-------------|
| `./voipbin-install init` | Interactive setup wizard; writes `config.yaml` + `secrets.yaml` |
| `./voipbin-install init --reconfigure` | Re-run wizard to change settings |
| `./voipbin-install init --dry-run` | Preview without applying |
| `./voipbin-install apply` | Full 8-stage deployment (interactive) |
| `./voipbin-install apply --auto-approve` | Skip confirmation (CI/CD) |
| `./voipbin-install apply --dry-run` | Preview Terraform plan only |
| `./voipbin-install apply --stage <name>` | Re-run a specific stage |
| `./voipbin-install verify` | Run all health checks |
| `./voipbin-install verify --check <name>` | Run a single health check |
| `./voipbin-install status` | Show deployment state (human-readable) |
| `./voipbin-install status --json` | Machine-readable output |
| `./voipbin-install destroy` | Tear down all GCP resources (irreversible) |
| `./voipbin-install destroy --auto-approve` | Skip confirmation |
| `./voipbin-install cert status` | Show Kamailio cert expiry and CA fingerprint |
| `./voipbin-install cert renew` | Re-run cert_provision stage |
| `./voipbin-install cert export-ca --out ca.pem` | Export CA certificate |

Run `./voipbin-install <command> --help` for full flag reference.


## TLS Strategy

The installer ships with two TLS strategies selectable during `init`:

- **`self-signed` (default)** — generates a 10-year self-signed cert at first
  `apply` and stores it in Kubernetes Secrets and env-vars consumed by backend
  and frontend Pods. Browsers will reject the cert until replaced. **Replace
  before serving production traffic** using the procedure in the full docs.
- **`byoc` (Bring Your Own Cert)** — operator pre-creates the Kubernetes
  Secrets with a CA-issued cert before the `k8s_apply` stage. The bootstrap
  function detects populated SSL keys and skips its own writes.

For step-by-step cert replacement, BYOC mode, and cert management,
see https://docs.voipbin.net/self-hosting.


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


## Cost Estimates

Estimated monthly costs for a minimal deployment in `us-central1`:

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

Costs vary by region.


## Troubleshooting

For common issues and solutions covering Terraform, Ansible, Kubernetes,
Cloud SQL, DNS, and SIP registration, see https://docs.voipbin.net/self-hosting
or [docs/troubleshooting.md](docs/troubleshooting.md).


## Directory Structure

```
install/
|-- voipbin-install       # Bash entry point (delegates to scripts/cli.py)
|-- requirements.txt      # Python dependencies
|-- Makefile              # install, lint, test, clean targets
|-- config/               # Defaults, schema, GCP API/IAM/quota lists, image versions
|-- scripts/              # CLI commands, pipeline orchestrator, GCP/K8s/Terraform helpers
|-- terraform/            # 18 Terraform files (VPC, GKE, SQL, DNS, LBs, KMS, etc.)
|-- ansible/              # Playbooks and roles for Kamailio and RTPEngine VMs
|-- k8s/                  # Kubernetes manifests (namespaces, backend, voip, frontend)
|-- tests/                # 103 unit tests
```


## Contributing

1. Install development dependencies: `pip install -r requirements.txt`
2. Run linting: `make lint`
3. Run tests: `make test`
4. Add tests in `tests/` for any new functionality
5. Follow the existing patterns in `scripts/` for new commands
