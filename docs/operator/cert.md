# TLS Certificates for Kamailio

The Kamailio SIP proxy serves TLS on two SANs:

- `sip.<domain>` (used by client TLS)
- `registrar.<domain>` (used by server-side WSS, accepts the wildcard `*.registrar.<domain>`)

This document describes how `voipbin-install` provisions those certificates, the two supported modes, and the operator actions needed to make browsers and SIP clients trust them.

## Modes

### `self_signed` (default)

`voipbin-install init` defaults to `cert_mode: self_signed`. On each `apply`, the `cert_provision` stage:

1. Generates a per-install Certificate Authority (`KAMAILIO_CA_*` keys in `secrets.yaml`, sops-encrypted).
2. Issues two RSA-2048 leaf certificates signed by that CA, one per SAN. The `registrar` leaf includes the wildcard SAN `*.registrar.<domain>`.
3. Persists fingerprints/expiry to `state.yaml.cert_state`.
4. Materializes the PEM files to `<workdir>/.cert-staging/<san>/` for the ansible playbook to deploy to the VM.

Validity periods: CA = 10 years, leaf = 365 days. Leaf certificates auto-renew when `voipbin-install apply` is run with fewer than 30 days remaining.

### `manual` (BYO certificates)

Set `cert_mode: manual` in `config.yaml` and supply `cert_manual_dir` pointing to a directory with this exact layout:

```
<cert_manual_dir>/
├── sip.<domain>/
│   ├── fullchain.pem
│   └── privkey.pem
└── registrar.<domain>/
    ├── fullchain.pem
    └── privkey.pem
```

`cert_provision` validates the layout, parses each PEM, rejects expired material, and base64-encodes the four leaf files into `secrets.yaml` (no CA keys are recorded because the CA is external).

Use this mode with publicly-trusted CA certificates (Let's Encrypt issued out-of-band, internal-PKI bundles, etc.) to avoid browser-trust prompts.

## Trusting the install CA (self_signed only)

Browsers and operating systems will not trust the per-install CA by default. WSS connections from web clients will fail until the CA is installed.

### Export the CA certificate

```
cd <workdir>
sops -d secrets.yaml | yq '.KAMAILIO_CA_CERT_BASE64' | tr -d '"' | base64 -d > install-ca.pem
```

(A future `voipbin-install cert export-ca` subcommand is tracked under PR-AA.)

### Install on macOS

```
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain install-ca.pem
```

### Install on Linux (Debian/Ubuntu)

```
sudo cp install-ca.pem /usr/local/share/ca-certificates/voipbin-install-ca.crt
sudo update-ca-certificates
```

### Install on Linux (RHEL/Fedora)

```
sudo cp install-ca.pem /etc/pki/ca-trust/source/anchors/voipbin-install-ca.crt
sudo update-ca-trust
```

### Install on Windows

```
certutil -addstore -f "ROOT" install-ca.pem
```

After installing, restart browsers so they reload the trust store.

## Renewal

Self-signed leaves auto-renew during `voipbin-install apply` when fewer than 30 days of validity remain.

The `cert renew` subcommand re-runs the `cert_provision` stage:

```
voipbin-install cert renew           # leaves reissued only if <30d remaining
voipbin-install cert renew --force   # leaves reissued unconditionally
```

**Scope of `--force`**: forces re-issuance of LEAF certificates only. The CA in `secrets.yaml` is preserved across `--force` runs — if the existing CA is still valid (>30d remaining), `--force` will issue new leaves signed by that same CA. The CA fingerprint published to operator browsers/trust stores therefore stays stable.

**Rotating the CA itself** (e.g. on operator handoff or suspected compromise) is a separate manual procedure:

```
sops edit secrets.yaml
# delete the KAMAILIO_CA_CERT_BASE64 and KAMAILIO_CA_KEY_BASE64 fields, save & exit
voipbin-install apply --stage cert_provision
```

The `cert_provision` stage will detect the missing CA keys, mint a fresh CA, re-issue all leaves under it, and update `state.yaml.cert_state.ca_fingerprint_sha256`. Operators must then re-install the new CA in every trust store that pinned the old one. A first-class `voipbin-install cert rotate-ca` subcommand that automates this flow is tracked under PR-AA.

To check current expiry:

```
voipbin-install cert status
voipbin-install cert status --json
```

For manual mode, replace the contents of `cert_manual_dir` with new fullchain.pem/privkey.pem files and run `voipbin-install apply` again.

A cron-style automation suggestion: schedule `voipbin-install apply` weekly. The `cert_provision` stage short-circuits on every run that finds >30d remaining, so the only side effect of frequent runs is an idempotent re-deploy of unchanged certificates.

## Threat model and limitations

### Workdir plaintext window

After each successful `apply`, `<workdir>/.cert-staging/` is removed. The directory is gitignored and created with mode `0700`. On ext4/xfs/COW filesystems, removed file contents may persist in unallocated blocks until overwritten. This is acceptable for v7 because:

- the workdir is operator-controlled with the same threat model as `secrets.yaml`,
- all key material is also present sops-encrypted in `secrets.yaml`,
- `voipbin-install cert clean-staging` provides explicit cleanup.

For hostile-host scenarios use full-disk encryption.

### CA blast radius

The CA is per-install and stored exclusively in this workdir's `secrets.yaml` (sops-encrypted). If you migrate the install to a new workdir, copy `secrets.yaml` to bring the CA along; otherwise expect the new workdir to mint a new CA on its first `apply`. Operators handing off the install should rotate the CA (`cert renew --force` after also rotating the secrets) as part of the handoff.

### Multi-replica restart

v7 deploys a single Kamailio replica. The `Recreate kamailio containers` handler restarts that single instance during `apply`. PR-AC will add a rolling-restart strategy for multi-replica deployments.

### Not in scope

- ACME (Let's Encrypt) automation. Not supported. Setting `cert_mode: acme`
  is rejected at config validation. Use `cert_mode: self_signed` or
  `cert_mode: manual`; see
  [Obtaining TLS Certificates](../../README.md#obtaining-tls-certificates)
  for external cert options.
- HSM/TPM-backed key storage.
- Certificate Transparency log monitoring.
- DANE/TLSA records.

## State files

| File | Contents |
|---|---|
| `secrets.yaml` (sops-encrypted) | `KAMAILIO_CA_CERT_BASE64`, `KAMAILIO_CA_KEY_BASE64`, `KAMAILIO_CERT_SIP_BASE64`, `KAMAILIO_PRIVKEY_SIP_BASE64`, `KAMAILIO_CERT_REGISTRAR_BASE64`, `KAMAILIO_PRIVKEY_REGISTRAR_BASE64`. `KAMAILIO_CA_*` keys are absent in `manual` mode. |
| `state.yaml` -> `cert_state` | Metadata only: schema_version, config_mode, actual_mode, ca_subject, ca_not_after, ca_fingerprint_sha256, san_list, leaf_certs (expiry/fingerprint/serial per SAN). No key material. |
| `.cert-staging/` (gitignored) | Ephemeral plaintext PEMs the ansible playbook reads, removed post-success. |

## Troubleshooting

- `cert_provision` failed with "cert_mode=acme is not supported": set `cert_mode` back to `self_signed` or `manual`.
- `cert_provision` failed with "manual cert layout invalid": ensure `cert_manual_dir/<sip|registrar>.<domain>/{fullchain,privkey}.pem` all exist and parse as PEM.
- Browser shows certificate warning despite CA install: clear the browser's cert cache (Firefox: about:certs; Chrome: chrome://settings/security) or restart the browser.
- Kamailio container reports "load_cert failed": run `voipbin-install cert status` to confirm expiry and SAN list match expectations, then `voipbin-install apply --stage cert_provision --force` followed by `--stage ansible_run`.
- `.cert-staging/` survives a failed `apply`: this is intentional for post-mortem. Remove manually with `voipbin-install cert clean-staging` once the apply is complete.
