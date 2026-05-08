# DNS & Domain Configuration Guide

**Date:** 2026-05-09
**Status:** Approved

## Problem

After `voipbin-install apply` completes, users see a "Deployment complete" box
with two vague next steps:

1. Configure DNS records (if manual)
2. Verify with: voipbin-install status

Users don't know what DNS records to create, what subdomains VoIPBin needs,
or what internal services (K8s ConfigMaps, Kamailio VM env vars) use the
domain. This causes confusion and silent failures while waiting for DNS and
TLS to work.

## Goal

Add a `voipbin-install dns` command that prints a clear, general-purpose
reference guide covering three areas of domain configuration:

1. What DNS records to create at the registrar
2. What K8s ConfigMap entries relate to the domain
3. What VM env vars Kamailio uses for domain routing

The guide is purely informational — no automation, no live queries. It uses
`example.com` / `1.2.3.4` throughout so it reads as documentation any user
can follow.

## Non-Goals

- DNS propagation polling or waiting
- Automated DNS record creation
- Modifying K8s ConfigMaps or Ansible vars
- Pulling live values from Terraform outputs or kubeconfig

## Architecture

### New files

- `scripts/commands/dns.py` — `cmd_dns()` function and guide renderer

### Modified files

- `scripts/cli.py` — register `dns` as a new `@cli.command()`
- `scripts/commands/apply.py` — call Section 1 (DNS records) after the
  success result box, so users see it immediately after deployment

### Design decision: static guide, no live data

The guide uses `example.com` and `1.2.3.4` as illustrative placeholders.
Approach A (reading `terraform output -json`) was considered but rejected:
the guide is meant to be educational for any user, not a readout of one
specific deployment. Keeping it static also means it works even when
Terraform state is unavailable (e.g., CI, a different machine).

## Output Spec

### `voipbin-install dns`

Three sections printed in sequence.

---

**Section 1 — DNS Records (at your registrar)**

```
  DNS Records
  ──────────────────────────────────────────────────

  VoIPBin requires the following DNS A records at your registrar.
  All subdomains point to the same load balancer IP.

    api.example.com      A    1.2.3.4
    admin.example.com    A    1.2.3.4
    talk.example.com     A    1.2.3.4
    meet.example.com     A    1.2.3.4
    sip.example.com      A    1.2.3.4

  For auto DNS mode: delegate your domain to the GCP nameservers
  printed after apply completes. GCP then manages the A records.

  DNS propagation can take up to 48 hours.
  Once complete, run: voipbin-install verify
```

---

**Section 2 — Kubernetes ConfigMap**

```
  Kubernetes — voipbin-config (namespace: bin-manager)
  ──────────────────────────────────────────────────

  The following domain value is set in the ConfigMap during deployment:

    DOMAIN    example.com

  If your backend services also need DOMAIN_NAME_TRUNK or
  DOMAIN_NAME_EXTENSION, add them to k8s/backend/configmap.yaml
  before running apply. Audit which services consume these values first.
```

---

**Section 3 — VM Environment (Kamailio)**

```
  Kamailio VM — /opt/kamailio-docker/.env
  ──────────────────────────────────────────────────

  Kamailio runs on GCE VMs via Docker Compose. The following
  domain-related env vars are written by the Ansible playbook:

    BASE_DOMAIN              example.com
    DOMAIN_NAME_EXTENSION    registrar.example.com
    DOMAIN_NAME_TRUNK        trunk.example.com

  RTPEngine has no domain-specific env vars.

  To update these after deployment, re-run: voipbin-install apply
```

---

### `apply` completion (condensed)

After the "Deployment complete" result box, Section 1 only is printed.
Sections 2 and 3 are internal details — not relevant to a first-time
deployer at that moment.

## CLI Registration

```python
@cli.command()
def dns():
    """Show DNS and domain configuration guide."""
    cmd_dns()
```

No options or flags needed for v1.

## Testing

- `test_dns.py` — unit tests for the guide renderer:
  - All five subdomains present in Section 1 output
  - Section 2 contains `DOMAIN`, `DOMAIN_NAME_TRUNK`, `DOMAIN_NAME_EXTENSION`
  - Section 3 contains `BASE_DOMAIN`, path `/opt/kamailio-docker/.env`
  - `voipbin-install dns` exits 0
- `apply.py` integration: Section 1 appears in success path output
