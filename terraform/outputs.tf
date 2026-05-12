###############################################################################
# GKE
###############################################################################

output "gke_cluster_name" {
  description = "Name of the GKE cluster"
  value       = google_container_cluster.voipbin.name
}

output "gke_cluster_endpoint" {
  description = "GKE cluster API endpoint"
  value       = google_container_cluster.voipbin.endpoint
  sensitive   = true
}

output "gke_cluster_ca_certificate" {
  description = "GKE cluster CA certificate (base64-encoded)"
  value       = google_container_cluster.voipbin.master_auth[0].cluster_ca_certificate
  sensitive   = true
}

###############################################################################
# Kamailio
###############################################################################

output "kamailio_instance_names" {
  description = "Names of Kamailio VM instances"
  value       = google_compute_instance.kamailio[*].name
}

output "kamailio_internal_ips" {
  description = "Internal IP addresses of Kamailio VMs"
  value       = google_compute_instance.kamailio[*].network_interface[0].network_ip
}

output "kamailio_external_ips" {
  description = "Ephemeral external IP addresses of Kamailio VMs (used by Ansible over OS Login)"
  # NOTE: a bare splat through the nested access_config block silently
  # evaluates to [null] across some provider versions. The explicit
  # for-expression below forces per-instance evaluation and returns the
  # live nat_ip. See PR-O1 / GAP-41.
  value = [
    for inst in google_compute_instance.kamailio :
    try(inst.network_interface[0].access_config[0].nat_ip, "")
  ]
}

###############################################################################
# RTPEngine
###############################################################################

output "rtpengine_instance_names" {
  description = "Names of RTPEngine VM instances"
  value       = google_compute_instance.rtpengine[*].name
}

output "rtpengine_external_ips" {
  description = "External IP addresses of RTPEngine VMs"
  value       = google_compute_address.rtpengine[*].address
}

###############################################################################
# Cloud SQL
###############################################################################

output "cloudsql_connection_name" {
  description = "Cloud SQL instance connection name (for Cloud SQL Proxy)"
  value       = google_sql_database_instance.voipbin.connection_name
}

output "cloudsql_mysql_private_ip" {
  description = "Cloud SQL MySQL private IP (consumed by reconcile_outputs FIELD_MAP)."
  value       = google_sql_database_instance.voipbin.private_ip_address
}

output "cloudsql_postgres_connection_name" {
  description = "Cloud SQL Postgres instance connection name (for Cloud SQL Proxy). PR-D2 may consume."
  value       = google_sql_database_instance.voipbin_postgres.connection_name
}

output "cloudsql_postgres_private_ip" {
  description = "Cloud SQL Postgres private IP (consumed by reconcile_outputs FIELD_MAP)."
  value       = google_sql_database_instance.voipbin_postgres.private_ip_address
}

###############################################################################
# Cloud SQL application passwords (PR-D2a)
###############################################################################
# Sensitive outputs. Programmatic use ONLY — do NOT run `terraform output -json`
# interactively (the shell history captures the plaintext). PR-D2b will pipe
# these into scripts/k8s.py substitution map directly.

output "cloudsql_mysql_password_bin_manager" {
  description = "Random password for the bin-manager MySQL application user."
  value       = random_password.mysql_bin_manager.result
  sensitive   = true
}

output "cloudsql_mysql_password_asterisk" {
  description = "Random password for the asterisk MySQL application user."
  value       = random_password.mysql_asterisk.result
  sensitive   = true
}

output "cloudsql_mysql_password_call_manager" {
  description = "Random password for the call-manager MySQL application user."
  value       = random_password.mysql_call_manager.result
  sensitive   = true
}

output "cloudsql_mysql_password_kamailioro" {
  description = "Random password for the kamailioro MySQL application user (network-pinned)."
  value       = random_password.mysql_kamailioro.result
  sensitive   = true
}

output "cloudsql_postgres_password_bin_manager" {
  description = "Random password for the bin-manager Postgres application user."
  value       = random_password.postgres_bin_manager.result
  sensitive   = true
}

###############################################################################
# Load Balancers
###############################################################################

output "kamailio_external_lb_ip" {
  description = "External IP of the Kamailio load balancer"
  value       = google_compute_address.kamailio_lb_external.address
}

output "kamailio_internal_lb_ip" {
  description = "Internal IP of the Kamailio load balancer"
  value       = google_compute_address.kamailio_lb_internal.address
}

# Placeholder: Redis internal LB IP will be assigned by K8s Service type LoadBalancer
output "redis_lb_ip" {
  description = "Redis internal LB IP (placeholder — set after K8s deployment)"
  value       = ""
}

# Placeholder: RabbitMQ internal LB IP will be assigned by K8s Service type LoadBalancer
output "rabbitmq_lb_ip" {
  description = "RabbitMQ internal LB IP (placeholder — set after K8s deployment)"
  value       = ""
}

###############################################################################
# KMS
###############################################################################

output "kms_key_id" {
  description = "Full resource ID of the SOPS KMS key"
  value       = google_kms_crypto_key.voipbin_sops_key.id
}

###############################################################################
# DNS
###############################################################################

output "dns_zone_name_servers" {
  description = "Name servers for the DNS zone (only when dns_mode is auto)"
  value       = var.dns_mode == "auto" ? google_dns_managed_zone.voipbin[0].name_servers : []
}

###############################################################################
# Network
###############################################################################

output "vpc_name" {
  description = "Name of the VPC network"
  value       = google_compute_network.voipbin.name
}

output "subnet_name" {
  description = "Name of the primary subnet"
  value       = google_compute_subnetwork.voipbin_main.name
}

output "subnet_cidr" {
  description = "CIDR range of the primary subnet"
  value       = google_compute_subnetwork.voipbin_main.ip_cidr_range
}

output "cloudsql_peering_range_cidr" {
  description = "CIDR of the reserved VPC-peering range. PR-C reconcile_outputs writes this into config.cloudsql_private_ip_cidr."
  value       = "${google_compute_global_address.cloudsql_peering.address}/${google_compute_global_address.cloudsql_peering.prefix_length}"
}

###############################################################################
# Project Info
###############################################################################

output "project_id" {
  description = "GCP project ID"
  value       = var.project_id
}

output "region" {
  description = "GCP region"
  value       = var.region
}

output "zone" {
  description = "GCP zone"
  value       = var.zone
}

###############################################################################
# External Service Static IPs (PR #2 of self-hosting redesign)
###############################################################################

output "api_manager_static_ip_name" {
  description = "Reserved static-IP name for the api-manager Service annotation"
  value       = google_compute_address.external_service["api-manager"].name
}

output "api_manager_static_ip_address" {
  description = "Reserved static-IP address for api-manager"
  value       = google_compute_address.external_service["api-manager"].address
}

output "hook_manager_static_ip_name" {
  description = "Reserved static-IP name for the hook-manager Service annotation"
  value       = google_compute_address.external_service["hook-manager"].name
}

output "hook_manager_static_ip_address" {
  description = "Reserved static-IP address for hook-manager"
  value       = google_compute_address.external_service["hook-manager"].address
}

output "admin_static_ip_name" {
  description = "Reserved static-IP name for the admin Service annotation"
  value       = google_compute_address.external_service["admin"].name
}

output "admin_static_ip_address" {
  description = "Reserved static-IP address for admin"
  value       = google_compute_address.external_service["admin"].address
}

output "talk_static_ip_name" {
  description = "Reserved static-IP name for the talk Service annotation"
  value       = google_compute_address.external_service["talk"].name
}

output "talk_static_ip_address" {
  description = "Reserved static-IP address for talk"
  value       = google_compute_address.external_service["talk"].address
}

output "meet_static_ip_name" {
  description = "Reserved static-IP name for the meet Service annotation"
  value       = google_compute_address.external_service["meet"].name
}

output "meet_static_ip_address" {
  description = "Reserved static-IP address for meet"
  value       = google_compute_address.external_service["meet"].address
}

###############################################################################
# GCS Storage Buckets (PR-G)
###############################################################################

output "recordings_bucket_name" {
  description = "Name of the call-recordings GCS bucket"
  value       = google_storage_bucket.recordings.name
}

output "tmp_bucket_name" {
  description = "Name of the disposable tmp GCS bucket (7-day TTL)"
  value       = google_storage_bucket.tmp.name
}
