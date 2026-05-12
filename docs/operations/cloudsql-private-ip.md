# Cloud SQL private IP — operator guide

VoIPBin connects to Cloud SQL (MySQL + Postgres) over the VPC peering
private IP. The install repo does not provision the peering today (see
PR #5b for the Terraform side); operators must wire the private IP
explicitly via `config.yaml`.

## 1. Find the private IP

```text
GCP Console
  → SQL
  → <your voipbin-mysql instance>
  → Connections tab
  → Private IP address
```

Equivalent gcloud:

```bash
gcloud sql instances describe voipbin-mysql \
    --project="$PROJECT_ID" \
    --format='value(ipAddresses[?type=PRIVATE].ipAddress)'
```

You should see something like `10.42.0.7`. If the field is empty the
instance has not been configured with a private IP yet — enable
"Private IP" on the instance and assign an allocated peering range
(`google_compute_global_address` + `google_service_networking_connection`
in Terraform; PR #5b will provision these for the install repo).

## 2. Verify VPC peering is active

```bash
gcloud services vpc-peerings list \
    --network=voipbin-vpc \
    --project="$PROJECT_ID"
```

Look for a peering named `servicenetworking-googleapis-com`. If it is
missing, Cloud SQL traffic from GKE will hit no route and time out.
Enable peering once:

```bash
gcloud compute addresses create voipbin-cloudsql-peering \
    --global \
    --purpose=VPC_PEERING \
    --addresses=10.42.0.0 \
    --prefix-length=16 \
    --network=voipbin-vpc \
    --project="$PROJECT_ID"

gcloud services vpc-peerings connect \
    --service=servicenetworking.googleapis.com \
    --ranges=voipbin-cloudsql-peering \
    --network=voipbin-vpc \
    --project="$PROJECT_ID"
```

## 3. Set the IP in `config.yaml`

```yaml
cloudsql_private_ip: "10.42.0.7"
# Optional: broaden the NetworkPolicy CIDR if your Cloud SQL instance
# is HA-regional and may failover to a different IP within the peering
# range. Default is "<cloudsql_private_ip>/32".
# cloudsql_private_ip_cidr: "10.42.0.0/24"
```

The default value `cloudsql-private.invalid` (RFC 2606 reserved
domain) is rejected by `voipbin-install` preflight with a pointer back
to this document. Leaving the sentinel in place produces a noisy
failure before any manifest is applied — by design.

## 4. Why the install repo does not provision this yet

Cloud SQL private IP requires:

- A `google_compute_global_address` reserving a /16 in the VPC for
  peering.
- A `google_service_networking_connection` from that range to
  `servicenetworking.googleapis.com`.
- The Cloud SQL instance configured with `ip_configuration.private_network`.

These pieces touch the Terraform `network.tf` and `cloudsql.tf` files
and have their own review surface (drift on the peering connection is
the standard horror story). PR #5b will add them and wire the
Terraform output `cloudsql_private_ip` straight into the substitution
map, replacing the operator-supplied path.

## 5. Migration note for clusters previously deployed from PR #4 main

PR #4 main carries a phantom `Deployment/cloudsql-proxy` that this PR
no longer manages. `kubectl apply` does not prune unmanaged resources.
Run once:

```bash
kubectl delete deploy cloudsql-proxy -n infrastructure
kubectl delete svc cloudsql-proxy -n infrastructure
kubectl delete sa cloudsql-proxy -n infrastructure
```

`voipbin-install` preflight emits a best-effort warning if it finds
the stale Deployment in the active kube context.
