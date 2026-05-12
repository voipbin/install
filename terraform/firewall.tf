# Kamailio SIP — allow external SIP/TLS/WSS traffic
resource "google_compute_firewall" "fw_kamailio_sip" {
  name    = "${var.env}-fw-kamailio-sip"
  network = google_compute_network.voipbin.id

  allow {
    protocol = "tcp"
    ports    = ["443", "5060", "5061"]
  }

  allow {
    protocol = "udp"
    ports    = ["5060"]
  }

  target_tags   = ["kamailio"]
  source_ranges = ["0.0.0.0/0"]
}

# RTPEngine RTP — allow media from Kamailio IPs and GKE pod CIDR
resource "google_compute_firewall" "fw_rtpengine_rtp" {
  name    = "${var.env}-fw-rtpengine-rtp"
  network = google_compute_network.voipbin.id

  allow {
    protocol = "udp"
    ports    = ["20000-30000"]
  }

  target_tags = ["rtpengine"]

  # Kamailio internal IPs + GKE pod CIDR
  source_ranges = concat(
    google_compute_instance.kamailio[*].network_interface[0].network_ip,
    [google_compute_subnetwork.voipbin_main.secondary_ip_range[0].ip_cidr_range],
  )
}

# RTPEngine control — allow from Kamailio tag
resource "google_compute_firewall" "fw_rtpengine_control" {
  name    = "${var.env}-fw-rtpengine-control"
  network = google_compute_network.voipbin.id

  allow {
    protocol = "tcp"
    ports    = ["22222"]
  }

  allow {
    protocol = "udp"
    ports    = ["22222"]
  }

  target_tags = ["rtpengine"]
  source_tags = ["kamailio"]
}

# VM SSH — allow operator SSH ingress to Kamailio and RTPEngine VMs.
# Authentication is enforced by OS Login (publickey-only + IAM-bound POSIX
# usernames). 0.0.0.0/0 is intentional: OS Login + publickey-only renders
# IP-level allowlisting redundant for the threat model VoIPBin targets
# (self-hosted SMB/dev). Operators wanting tighter ingress can override
# source_ranges via a custom terraform overlay.
resource "google_compute_firewall" "fw_vm_ssh" {
  name    = "${var.env}-fw-vm-ssh"
  network = google_compute_network.voipbin.id

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  target_tags   = ["kamailio", "rtpengine"]
  source_ranges = ["0.0.0.0/0"]
}

# GKE internal — allow GKE pods to reach Redis and RabbitMQ
resource "google_compute_firewall" "fw_gke_internal" {
  name    = "${var.env}-fw-gke-internal"
  network = google_compute_network.voipbin.id

  allow {
    protocol = "tcp"
    ports    = ["6379", "5672"]
  }

  source_ranges = [google_compute_subnetwork.voipbin_main.secondary_ip_range[0].ip_cidr_range]
}

# VM to infra — allow Kamailio and RTPEngine VMs to reach Redis and RabbitMQ
resource "google_compute_firewall" "fw_vm_to_infra" {
  name    = "${var.env}-fw-vm-to-infra"
  network = google_compute_network.voipbin.id

  allow {
    protocol = "tcp"
    ports    = ["6379", "5672"]
  }

  source_tags = ["kamailio", "rtpengine"]
}

# Health checks — allow GCP health check probes to Kamailio
resource "google_compute_firewall" "fw_healthcheck" {
  name    = "${var.env}-fw-healthcheck"
  network = google_compute_network.voipbin.id

  allow {
    protocol = "tcp"
    ports    = ["5060"]
  }

  target_tags   = ["kamailio"]
  source_ranges = ["35.191.0.0/16", "130.211.0.0/22"]
}

# Internal — allow all TCP/UDP within the subnet
resource "google_compute_firewall" "fw_allow_internal" {
  name    = "${var.env}-fw-allow-internal"
  network = google_compute_network.voipbin.id

  allow {
    protocol = "tcp"
  }

  allow {
    protocol = "udp"
  }

  source_ranges = [google_compute_subnetwork.voipbin_main.ip_cidr_range]
}
