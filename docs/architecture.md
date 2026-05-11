# VoIPBin Architecture

Detailed architecture documentation for the VoIPBin GCP deployment.


## System Architecture

```
                        +--------------------------------------------+
                        |              GCP Project                   |
                        |                                            |
    Internet            |   +------------------------------------+   |
       |                |   |          VPC (10.0.0.0/16)         |   |
       |                |   |                                    |   |
       |   +-------+    |   |  +--------+  +--------+           |   |
  SIP/ |   | Ext.  |----+-->|  |Kamailio|  |Kamailio|           |   |
  TLS/ +-->| LB    |    |   |  | VM-0   |  | VM-1   |           |   |
  WSS  |   |(static|    |   |  +----+---+  +----+---+           |   |
       |   | IP)   |    |   |       |           |               |   |
       |   +-------+    |   |       +-----+-----+               |   |
       |                |   |             |                      |   |
       |   +-------+    |   |        +----+----+                 |   |
  RTP  +-->|Static |----+-->|        | Int. LB |                 |   |
  Media|   |Ext IPs|    |   |        +---------+                 |   |
       |   |(per   |    |   |             |                      |   |
       |   | VM)   |    |   |  +----------+----------+           |   |
       |   +-------+    |   |  |RTPEngine |RTPEngine |           |   |
       |                |   |  | VM-0     | VM-1     |           |   |
       |                |   |  +----------+----------+           |   |
       |                |   |                                    |   |
       |                |   |  +------------------------------+  |   |
       |                |   |  |  GKE Cluster (private nodes) |  |   |
       |                |   |  |                              |  |   |
       |                |   |  |  +---------+ +----------+    |  |   |
       |                |   |  |  |bin-mgr  | |  voip    |    |  |   |
       |                |   |  |  |namespace| | namespace|    |  |   |
       |                |   |  |  |         | |          |    |  |   |
       |                |   |  |  | 31 Go   | | Asterisk |    |  |   |
       |                |   |  |  | services| | x3       |    |  |   |
       |                |   |  |  | 3 front | |          |    |  |   |
       |                |   |  |  +---------+ +----------+    |  |   |
       |                |   |  |                              |  |   |
       |                |   |  |  +------------------------+  |  |   |
       |                |   |  |  | infrastructure ns      |  |  |   |
       |                |   |  |  | Redis | RMQ | CH | SQL |  |  |   |
       |                |   |  |  +------------------------+  |  |   |
       |                |   |  +------------------------------+  |   |
       |                |   |                                    |   |
       |                |   +------------------------------------+   |
       |                |                                            |
       |                |   +-----------+  +-----------+             |
       |                |   | Cloud SQL |  | Cloud DNS |             |
       |                |   | MySQL 8.0 |  | (optional)|             |
       |                |   +-----------+  +-----------+             |
       |                |                                            |
       |                |   +-----------+  +-----------+             |
       |                |   | Cloud KMS |  | GCS       |             |
       |                |   | (SOPS)    |  | Buckets   |             |
       |                |   +-----------+  +-----------+             |
       |                |                                            |
       |                |   +-----------+                            |
       |                |   | Cloud NAT |---> Static outbound IP     |
       |                |   +-----------+                            |
       +                +--------------------------------------------+
```


## Network Topology

```
    External Traffic                    Internal Traffic
    ================                    ================

    SIP (UDP 5060) --+                  GKE Pods (10.1.0.0/16)
    SIP (TCP 5060) --+--> Ext. LB -->      |
    TLS (TCP 5061) --+    (static IP)      +---> Int. LB --> Kamailio VMs
    WSS (TCP 443)  --+                     |
                                           +---> Redis (6379)
    RTP (UDP 20000-30000)                  |
         |                                 +---> RabbitMQ (5672)
         +---> RTPEngine VMs              |
               (static ext IPs)            +---> Cloud SQL Proxy --> Cloud SQL


    +-------------------------------------------------------------------+
    |                        VPC: 10.0.0.0/16                           |
    |                                                                   |
    |   Primary Subnet: 10.0.0.0/16                                    |
    |   +-----------------------------------------------------------+   |
    |   |                                                           |   |
    |   |   Kamailio VMs          RTPEngine VMs        GKE Nodes   |   |
    |   |   10.0.x.x              10.0.x.x             10.0.x.x    |   |
    |   |                                                           |   |
    |   +-----------------------------------------------------------+   |
    |                                                                   |
    |   GKE Pod Range: 10.1.0.0/16                                     |
    |   GKE Service Range: 10.2.0.0/20                                 |
    |   GKE Master CIDR: 10.32.0.0/28                                  |
    |                                                                   |
    |   Cloud NAT (static IP) --> all outbound traffic                  |
    |                                                                   |
    +-------------------------------------------------------------------+
```

### Firewall Rule Matrix

```
    Source              -->  Target           Ports              Rule Name
    ============================================================================
    0.0.0.0/0           -->  [kamailio]       443,5060,5061      fw-kamailio-sip
    Kamailio IPs        -->  [rtpengine]      20000-30000/UDP    fw-rtpengine-rtp
    + GKE pod CIDR
    [kamailio] tag      -->  [rtpengine]      22222              fw-rtpengine-ctrl
    35.235.240.0/20     -->  [kamailio,       22/TCP             fw-iap-ssh
                              rtpengine]
    GKE pod CIDR        -->  *                6379,5672/TCP      fw-gke-internal
    [kamailio,          -->  *                6379,5672/TCP      fw-vm-to-infra
     rtpengine] tags
    35.191.0.0/16,      -->  [kamailio]       5060/TCP           fw-healthcheck
    130.211.0.0/22
    10.0.0.0/16         -->  *                all TCP/UDP        fw-allow-internal
```


## Service Dependency Map

```
                             +---------------+
                             |   api-manager |<---- HTTP clients
                             +-------+-------+
                                     |
                    +----------------+----------------+
                    |                |                |
              +-----+-----+   +-----+-----+   +-----+-----+
              |call-manager|   |agent-mgr  |   |customer-  |
              +-----+------+  +-----+------+  | manager   |
                    |                |         +-----------+
                    |                |
              +-----+------+  +-----+------+
              |Kamailio    |  |flow-manager|
              |(SIP proxy) |  +-----+------+
              +-----+------+        |
                    |          +----+----+
              +-----+------+  | ai-mgr  |
              | RTPEngine  |  +---------+
              | (RTP media)|
              +------------+

    All services share:
    +----------+  +----------+  +-----------+  +----------+
    |  Redis   |  | RabbitMQ |  | ClickHouse|  |Cloud SQL |
    |  (cache/ |  | (message |  | (analytics|  | (primary |
    |   pubsub)|  |  broker) |  |  events)  |  |  store)  |
    +----------+  +----------+  +-----------+  +----------+
```


## Data Flow

### Inbound SIP Call

```
    Phone/SIP Client
         |
         | SIP INVITE (UDP/TCP 5060 or TLS 5061)
         v
    External LB (static IP)
         |
         v
    Kamailio VM
         |
         +---> RabbitMQ (publish call event)
         |
         +---> call-manager (HTTP notify)
         |         |
         |         +---> Cloud SQL (store call record)
         |         +---> Redis (cache call state)
         |         +---> ClickHouse (analytics event)
         |
         | SIP (to Asterisk inside GKE)
         v
    Asterisk (asterisk-call pod)
         |
         | RTP media setup
         v
    RTPEngine VM (static external IP)
         |
         | RTP media (UDP 20000-30000)
         v
    Remote endpoint
```

### WebRTC Call (Browser)

```
    Browser (square-talk)
         |
         | WSS (TCP 443)
         v
    External LB
         |
         v
    Kamailio VM (WebSocket SIP)
         |
         +---> Same flow as SIP call above
         |
         v
    RTPEngine VM (DTLS-SRTP media relay)
         |
         v
    Remote endpoint
```


## Deployment Sequence

```
    voipbin-install init
    =====================
    1. Preflight checks (tool versions)
    2. GCP auth validation
    3. Interactive wizard (7 questions)
    4. Project + billing validation
    5. Quota check
    6. Enable 16 GCP APIs (with retry)
    7. Create installer service account + 12 IAM roles
    8. Create KMS key ring + crypto key
    9. Generate secrets + SOPS encrypt
    10. Write .sops.yaml
    11. Save config.yaml

    voipbin-install apply
    =====================
    Stage 1: Terraform (GCP infrastructure)
    +-----------------------------------------+
    | 1. terraform init (GCS backend)         |
    | 2. terraform plan                       |
    | 3. terraform apply                      |
    |    - APIs + propagation wait            |
    |    - VPC + subnet + secondary ranges    |
    |    - Cloud NAT + router                 |
    |    - Firewall rules (8)                 |
    |    - GKE cluster + node pool            |
    |    - Kamailio VM (1)                    |
    |    - RTPEngine VM (1) + static IP       |
    |    - Cloud SQL MySQL                    |
    |    - Cloud DNS zone + records           |
    |    - Load balancers (ext + int)         |
    |    - KMS key ring                       |
    |    - GCS buckets (state + media)        |
    |    - Service accounts + IAM             |
    +-----------------------------------------+
                      |
                      v
    Stage 2: Ansible (VM configuration)
    +-----------------------------------------+
    | 1. Dynamic GCP inventory                |
    | 2. Common role (all VMs)                |
    |    - Package updates, Docker install    |
    | 3. Kamailio role                        |
    |    - Docker Compose + config templates  |
    |    - Environment variables              |
    |    - Service start                      |
    | 4. RTPEngine role                       |
    |    - Docker Compose + config templates  |
    |    - Environment variables              |
    |    - Service start                      |
    +-----------------------------------------+
                      |
                      v
    Stage 3: Kubernetes (workload deployment)
    +-----------------------------------------+
    | 1. kubectl get-credentials              |
    | 2. Namespaces (bin-manager, infra, voip)|
    | 3. Network policies (default-deny +     |
    |    allow rules)                         |
    | 4. Infrastructure                       |
    |    - Redis + ConfigMap + Secret         |
    |    - RabbitMQ + Secret                  |
    |    - ClickHouse + ConfigMap             |
    |    - Cloud SQL Proxy                    |
    | 5. Backend services (31 deployments)    |
    | 6. VoIP (3 Asterisk deployments)        |
    | 7. Frontend (3 web apps in square-manager ns)  |
    | 8. Per-Service LoadBalancers + TLS bootstrap   |
    | 9. Database migration job               |
    +-----------------------------------------+
```


## Security Architecture

### Encryption

- **Secrets at rest**: SOPS + GCP KMS with 90-day key rotation
- **Database connections**: Cloud SQL Proxy (encrypted tunnel), SSL required
- **GKE nodes**: Shielded instances with Secure Boot and Integrity Monitoring
- **Storage**: Uniform bucket-level access, public access prevention enforced

### Network Isolation

- Private GKE nodes (no public IPs on cluster nodes)
- VM SSH access only through IAP tunnel (source: 35.235.240.0/20)
- Kubernetes network policies with default-deny and explicit allow rules
- Pod security standards enforced per namespace (baseline / restricted)

### IAM

- Dedicated service accounts per resource type:
  - `sa-voipbin-gke-nodes` -- GKE node pool
  - `sa-voipbin-kamailio` -- Kamailio VMs
  - `sa-voipbin-rtpengine` -- RTPEngine VMs
  - `sa-voipbin-cloudsql-proxy` -- Cloud SQL Proxy
- Least-privilege scopes (logging, monitoring, storage read)
- OS Login enabled on all VMs (no SSH key management)

### Terraform State

- Stored in a versioned GCS bucket with 5-version retention
- Bucket has uniform access, public access prevention enforced
