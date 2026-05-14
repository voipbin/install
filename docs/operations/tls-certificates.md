# TLS Certificates

This document covers the full TLS certificate lifecycle for VoIPBin
self-hosted deployments: how to obtain certificates, inject them, renew
them, and troubleshoot common failures.

---

## Two independent TLS systems

VoIPBin uses TLS in two separate contexts, each with its own configuration:

| System | Config key | Default | Consumers |
|--------|-----------|---------|-----------|
| Frontend (admin, talk, meet, api, hook) | `tls_strategy` | `self-signed` (hyphen) | nginx TLS sidecars, Go api/hook binaries |
| Kamailio SIP proxy | `cert_mode` | `self_signed` (underscore) | SIP/TLS, SIPS, WebRTC DTLS |

These are managed independently. Replacing a frontend cert does not affect
Kamailio, and vice versa.

---

## Obtaining certificates

### Let's Encrypt (Certbot)

[Certbot](https://certbot.eff.org/) is the recommended tool for obtaining
free, publicly trusted certificates from Let's Encrypt.

**Prerequisite:** All domains must resolve (via DNS A records) to the machine
running the Certbot standalone challenge before you run the command. Create
DNS records and wait for propagation first — see
[DNS Records](../../README.md#dns-records).

**Standalone HTTP challenge** (requires port 80 open on the Certbot host):

```bash
certbot certonly --standalone \
  -d api.example.com \
  -d hook.example.com \
  -d admin.example.com \
  -d talk.example.com \
  -d meet.example.com
```

Certificates are written to `/etc/letsencrypt/live/api.example.com/`.

**DNS challenge** (no port 80 required; works with any DNS provider):

```bash
certbot certonly --manual \
  --preferred-challenges dns \
  -d "*.example.com" \
  -d example.com
# Follow the prompts to add TXT records at your registrar.
```

### Commercial or self-managed CA

Purchase or issue a certificate from any CA. You need:
- `fullchain.pem` — leaf certificate plus any intermediate CA certificates
- `privkey.pem` — private key (unencrypted PEM)

---

## Injecting frontend certificates

### Post-install replacement (already running `self-signed`)

See [Production Cert Replacement](../../README.md#production-cert-replacement)
in the main README for the full procedure. Summary:

```bash
# 1. Replace voipbin-secret SSL keys (api-manager, hook-manager)
kubectl -n bin-manager patch secret voipbin-secret \
  --type=merge \
  -p "{\"data\":{\"SSL_CERT_BASE64\":\"$(base64 -w0 fullchain.pem)\",\"SSL_PRIVKEY_BASE64\":\"$(base64 -w0 privkey.pem)\"}}"

# 2. Replace bin-manager voipbin-tls Secret
kubectl -n bin-manager create secret tls voipbin-tls \
  --cert=fullchain.pem --key=privkey.pem \
  --dry-run=client -o yaml | kubectl apply -f -

# 3. Replace square-manager voipbin-tls Secret
kubectl -n square-manager create secret tls voipbin-tls \
  --cert=fullchain.pem --key=privkey.pem \
  --dry-run=client -o yaml | kubectl apply -f -

# 4. Roll consumers
kubectl -n bin-manager rollout restart deployment/api-manager deployment/hook-manager
kubectl -n square-manager rollout restart deployment/admin deployment/talk deployment/meet

# 5. Verify
./voipbin-install verify --check=tls_cert_is_production
```

### First install with real cert (BYOC mode)

See [BYOC Mode](../../README.md#byoc-mode) in the main README.

---

## Kamailio certificate

Kamailio's TLS cert is managed separately from the frontend cert via `cert_mode`.

### Self-signed (default)

The installer generates a private CA and issues leaf certificates for
`sip.<domain>` and `registrar.<domain>`. SIP clients and WebRTC browsers
must trust this CA.

**Export the CA certificate:**

```bash
./voipbin-install cert export-ca --out ca.pem        # PEM format
./voipbin-install cert export-ca --der --out ca.der  # DER format
```

Install `ca.pem` as a trusted CA on all SIP clients, WebRTC browsers, and
any service that connects to Kamailio over TLS.

### Manual (CA-issued cert)

To use a publicly trusted cert for Kamailio:

1. Issue or obtain a cert for `sip.<domain>` and `registrar.<domain>`.

2. Create the directory structure:
   ```
   /path/to/kamailio-certs/
     sip.example.com/
       fullchain.pem
       privkey.pem
     registrar.example.com/
       fullchain.pem
       privkey.pem
   ```

3. Set in `config.yaml`:
   ```yaml
   cert_mode: manual
   cert_manual_dir: /path/to/kamailio-certs
   ```

4. Run `./voipbin-install apply` — the `cert_provision` stage will validate
   and load the certs.

---

## Renewal

Let's Encrypt certificates expire after 90 days. The installer does **not**
automate renewal.

**Renewal procedure:**

1. Renew with Certbot: `certbot renew`
2. Copy the new `fullchain.pem` and `privkey.pem` from
   `/etc/letsencrypt/live/<domain>/`
3. Re-run the injection procedure (see above for frontend; re-run
   `./voipbin-install apply` for Kamailio manual mode)
4. Verify: `./voipbin-install verify --check=tls_cert_is_production`

For Kamailio `self_signed` mode, the installer automatically reissues certs
when they are within 30 days of expiry on each `apply` run.

---

## Troubleshooting

### Inspect frontend cert in cluster

```bash
# View voipbin-tls Secret in bin-manager
kubectl -n bin-manager get secret voipbin-tls -o jsonpath='{.data.tls\.crt}' \
  | base64 -d | openssl x509 -noout -subject -issuer -dates

# View voipbin-tls Secret in square-manager
kubectl -n square-manager get secret voipbin-tls -o jsonpath='{.data.tls\.crt}' \
  | base64 -d | openssl x509 -noout -subject -issuer -dates
```

### Check Kamailio cert state

```bash
./voipbin-install cert status
./voipbin-install cert status --json
```

### cert_provision failed with "cert_mode=acme is not supported"

Set `cert_mode` back to `self_signed` or `manual` in `config.yaml`. ACME
is not supported by this installer. Obtain a cert externally and use
`cert_mode: manual`.

### Browsers reject the certificate

If using `tls_strategy: self-signed`, browsers will show a warning because
the cert is not issued by a public CA. Either:
- Accept the warning manually (development only)
- Replace with a public CA cert (see [Post-install replacement](#post-install-replacement-already-running-self-signed) above)

### SIP clients reject Kamailio TLS

If `cert_mode: self_signed`, export and install the CA cert:

```bash
./voipbin-install cert export-ca --out ca.pem
# Install ca.pem as a trusted CA on your SIP client.
```
