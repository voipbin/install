# VPC network — custom mode, NOT auto mode, NOT default
resource "google_compute_network" "voipbin" {
  name                    = "${var.env}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"

  depends_on = [time_sleep.api_propagation]
}

# Primary subnet with secondary ranges for GKE pods and services
resource "google_compute_subnetwork" "voipbin_main" {
  name          = "${var.env}-main"
  network       = google_compute_network.voipbin.id
  region        = var.region
  ip_cidr_range = "10.0.0.0/16"

  secondary_ip_range {
    range_name    = "${var.env}-pods"
    ip_cidr_range = "10.1.0.0/16"
  }

  secondary_ip_range {
    range_name    = "${var.env}-services"
    ip_cidr_range = "10.2.0.0/20"
  }

  private_ip_google_access = true
}

# Reserved IP range for VPC peering with Google managed services.
# Matches production's /20 prefix on google-managed-services-default.
resource "google_compute_global_address" "cloudsql_peering" {
  name          = "${var.env}-cloudsql-peering"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = var.cloudsql_peering_prefix_length
  network       = google_compute_network.voipbin.id

  depends_on = [time_sleep.api_propagation]
}

# Service Networking connection enables VPC peering with Google managed
# services (Cloud SQL Private IP, Memorystore, etc.).
resource "google_service_networking_connection" "voipbin" {
  network                 = google_compute_network.voipbin.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.cloudsql_peering.name]
  deletion_policy         = "ABANDON"
}
