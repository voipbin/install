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
