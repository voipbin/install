# Troubleshooting Guide

Common issues and solutions for the VoIPBin installer.


## Terraform Errors

### Quota exceeded

**Symptom**: `Error: googleapi: Error 403: Quota exceeded`

**Solution**:
1. Run `./voipbin-install init --skip-api-enable` to re-check quotas
2. Request quota increases at https://console.cloud.google.com/iam-admin/quotas
3. Common quotas to increase: CPUS_ALL_REGIONS, IN_USE_ADDRESSES, SSD_TOTAL_GB

### Permission denied

**Symptom**: `Error: googleapi: Error 403: Required permission`

**Solution**:
1. Verify your gcloud account: `gcloud auth list`
2. Check project-level IAM: `gcloud projects get-iam-policy PROJECT_ID`
3. Required roles: `roles/editor` or specific roles listed in `config/gcp_iam_roles.yaml`

### State lock

**Symptom**: `Error: Error locking state: Error acquiring the state lock`

**Solution**:
```bash
cd terraform
terraform force-unlock LOCK_ID
```
Only use this if you are certain no other process is running Terraform.

### API not enabled

**Symptom**: `Error: googleapi: Error 403: API not enabled`

**Solution**:
```bash
./voipbin-install init  # Re-run init to enable APIs
# Or manually:
gcloud services enable container.googleapis.com --project PROJECT_ID
```

### Backend initialization failed

**Symptom**: `Error: Failed to get existing workspaces: storage: bucket doesn't exist`

**Solution**: The GCS state bucket must exist before `terraform init`. The apply command creates it automatically. If running Terraform manually:
```bash
gsutil mb -p PROJECT_ID gs://PROJECT_ID-voipbin-tf-state
```


## Ansible Errors

### OS Login SSH connection failed

**Symptom**: `Permission denied (publickey)` from a Kamailio or RTPEngine VM during the Ansible stage.

**Solution**:
1. Run once: `gcloud compute config-ssh` (generates `~/.ssh/google_compute_engine` and uploads the public key to your OS Login profile)
2. Verify your OS Login profile has the key registered: `gcloud compute os-login ssh-keys list`
3. Verify your account holds both `roles/compute.osLogin` and `roles/compute.osAdminLogin` on the project (Ansible needs sudo).
4. Verify port 22 is open on the VM's firewall: `gcloud compute firewall-rules list --filter="name~vm-ssh"`
5. Re-run: `./voipbin-install apply`

### Docker install failed

**Symptom**: `Could not get lock /var/lib/dpkg/lock` or `apt-get failed`

**Solution**: Wait for cloud-init to complete on new VMs:
```bash
gcloud compute ssh VM_NAME -- 'cloud-init status --wait'
```
Then re-run: `./voipbin-install apply --stage ansible`

### Template rendering failed

**Symptom**: `AnsibleUndefinedVariable: 'variable_name' is undefined`

**Solution**: Check that Terraform outputs are available:
```bash
cd terraform && terraform output -json
```
Verify the expected variables exist in the output.


## Kubernetes Errors

### Pod CrashLoopBackOff

**Symptom**: Pods in `CrashLoopBackOff` state

**Diagnosis**:
```bash
kubectl get pods -n bin-manager
kubectl logs POD_NAME -n bin-manager
kubectl describe pod POD_NAME -n bin-manager
```

**Common causes**:
- Cloud SQL Proxy not ready (check `infrastructure` namespace first)
- Redis/RabbitMQ not ready
- Missing ConfigMap or Secret values
- Incorrect environment variables

### ImagePullBackOff

**Symptom**: Pods stuck in `ImagePullBackOff`

**Solution**:
1. Verify image exists: `docker pull voipbin/IMAGE_NAME:TAG`
2. Check image tag in `config/versions.yaml`
3. GKE nodes need internet access (Cloud NAT must be working)

### Insufficient resources

**Symptom**: Pods stuck in `Pending` with `Insufficient cpu/memory`

**Solution**:
```bash
kubectl describe nodes  # Check allocatable resources
kubectl top nodes       # Check current usage
```
Options:
- Increase node count in config.yaml and re-apply
- Use larger machine type (e.g., `n1-standard-4`)

### Network policy blocking traffic

**Symptom**: Services cannot communicate, connection refused/timeout

**Diagnosis**:
```bash
kubectl get networkpolicies -A
kubectl describe networkpolicy POLICY_NAME -n NAMESPACE
```

**Solution**: Check that network policies in `k8s/network-policies/` allow the required traffic paths.


## Cloud SQL Issues

### Connection refused from pods

**Symptom**: Backend services cannot connect to MySQL

**Diagnosis**:
```bash
# Verify the private IP recorded in config.yaml matches the live Cloud SQL instance
voipbin-install verify
gcloud sql instances describe voipbin-mysql --project PROJECT_ID \
  --format='value(ipAddresses[].ipAddress)'

# Test private-IP reachability from inside a backend pod
kubectl run -n bin-manager netcheck --rm -it --image=busybox -- \
  nc -vz <cloudsql_private_ip> 3306
```

**Solution**:
1. Confirm VPC peering between your GKE VPC and the Cloud SQL service-networking VPC is `ACTIVE`
2. Confirm `config.cloudsql_private_ip` matches the live instance private IP
3. Confirm the NetworkPolicy `allow-to-cloudsql-private-ip` is rendered with the correct CIDR (`kubectl get networkpolicy -n bin-manager allow-to-cloudsql-private-ip -o yaml`)
4. See `docs/operations/cloudsql-private-ip.md` for the full guide

### Authentication failed

**Symptom**: `Access denied for user 'root'@'IP'`

**Solution**:
1. Decrypt secrets: `sops --decrypt secrets.yaml`
2. Verify the password matches: `gcloud sql users list --instance voipbin-mysql`
3. Reset if needed: `gcloud sql users set-password root --instance voipbin-mysql --password NEW_PASSWORD`


## DNS Issues

### Domain not resolving

**Symptom**: `dig api.example.com` returns no results

**Solution** (when `dns_mode: auto`):
1. Check Cloud DNS zone: `gcloud dns managed-zones describe voipbin-zone --project PROJECT_ID`
2. Verify name servers: `gcloud dns managed-zones describe voipbin-zone --format="value(nameServers)"`
3. Update your domain registrar's NS records to point to the Cloud DNS name servers
4. Wait for DNS propagation (up to 48 hours for NS changes)

**Solution** (when `dns_mode: manual`):
1. Get the external LB IP from Terraform outputs
2. Create A records with your DNS provider:
   - `api.DOMAIN` → LB IP
   - `admin.DOMAIN` → LB IP
   - `talk.DOMAIN` → LB IP
   - `meet.DOMAIN` → LB IP
   - `sip.DOMAIN` → LB IP


## SIP Registration Issues

### SIP devices cannot register

**Symptom**: SIP phones show "Registration Failed"

**Diagnosis**:
1. Check Kamailio is running:
   ```bash
   gcloud compute ssh kamailio-0 --tunnel-through-iap -- 'docker compose ps'
   ```
2. Check Kamailio logs:
   ```bash
   gcloud compute ssh kamailio-0 --tunnel-through-iap -- 'docker compose logs --tail 100'
   ```
3. Verify external LB is healthy:
   ```bash
   gcloud compute backend-services get-health voipbin-kamailio-backend --region REGION
   ```

### No audio in calls

**Symptom**: Calls connect but no audio

**Diagnosis**:
1. Check RTPEngine is running:
   ```bash
   gcloud compute ssh rtpengine-0 --tunnel-through-iap -- 'docker compose ps'
   ```
2. Verify RTPEngine ports are open: `nc -zuv RTPENGINE_IP 20000`
3. Check firewall allows RTP traffic
4. Verify RTPEngine has the correct external IP configured


## Checking Logs

### GKE pod logs
```bash
kubectl logs DEPLOYMENT_NAME -n NAMESPACE --tail 100
kubectl logs DEPLOYMENT_NAME -n NAMESPACE -f  # follow
kubectl logs DEPLOYMENT_NAME -n NAMESPACE --previous  # crashed pod
```

### Kamailio logs
```bash
gcloud compute ssh kamailio-0 --tunnel-through-iap -- 'docker compose logs --tail 200'
```

### RTPEngine logs
```bash
gcloud compute ssh rtpengine-0 --tunnel-through-iap -- 'docker compose logs --tail 200'
```

### Cloud SQL logs
```bash
gcloud sql instances describe voipbin-mysql --format="value(settings.databaseFlags)"
gcloud logging read "resource.type=cloudsql_database" --limit 50 --project PROJECT_ID
```

### Terraform state
```bash
cd terraform
terraform state list     # List all managed resources
terraform state show RESOURCE_ADDRESS  # Show resource details
```


## Rollback

### Rollback Kubernetes changes
```bash
kubectl rollout undo deployment/DEPLOYMENT_NAME -n NAMESPACE
```

### Rollback to previous Terraform state
```bash
# View state versions
gsutil ls -la gs://PROJECT_ID-voipbin-tf-state/default.tfstate

# Restore a previous version
gsutil cp gs://PROJECT_ID-voipbin-tf-state/default.tfstate#VERSION terraform.tfstate
terraform apply
```

### Full teardown and rebuild
```bash
./voipbin-install destroy
./voipbin-install apply
```
