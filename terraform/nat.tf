# Cloud Router for NAT
resource "google_compute_router" "voipbin" {
  name    = "${var.env}-router"
  region  = var.region
  network = google_compute_network.voipbin.id
}

# Static IP for Cloud NAT
resource "google_compute_address" "nat_ip" {
  name         = "${var.env}-nat-ip"
  region       = var.region
  address_type = "EXTERNAL"
}

# Cloud NAT — manual IP, applied to voipbin-main subnet
resource "google_compute_router_nat" "voipbin" {
  name   = "${var.env}-nat"
  router = google_compute_router.voipbin.name
  region = google_compute_router.voipbin.region

  nat_ip_allocate_option = "MANUAL_ONLY"
  nat_ips                = [google_compute_address.nat_ip.self_link]

  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  subnetwork {
    name                    = google_compute_subnetwork.voipbin_main.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}
