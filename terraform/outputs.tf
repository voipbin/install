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

output "cloudsql_ip" {
  description = "Cloud SQL instance public IP address"
  value       = google_sql_database_instance.voipbin.public_ip_address
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
