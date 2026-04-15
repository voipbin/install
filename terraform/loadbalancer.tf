###############################################################################
# Kamailio External Load Balancer
###############################################################################

# External static IP for Kamailio LB
resource "google_compute_address" "kamailio_lb_external" {
  name         = "${var.env}-kamailio-lb-external"
  region       = var.region
  address_type = "EXTERNAL"
}

# Instance group for Kamailio (used by target pool)
resource "google_compute_instance_group" "kamailio" {
  name        = "${var.env}-ig-kamailio-${var.zone}"
  description = "Instance group for Kamailio"
  zone        = var.zone

  instances = google_compute_instance.kamailio[*].self_link

  named_port {
    name = "sip"
    port = 5060
  }

  named_port {
    name = "tls"
    port = 5061
  }
}

# HTTP health check for external target pool
resource "google_compute_http_health_check" "kamailio_external" {
  name               = "${var.env}-hc-kamailio-external"
  check_interval_sec = 5
  timeout_sec        = 2
  port               = 5060
  request_path       = "/health-check"
}

# Target pool for external LB
resource "google_compute_target_pool" "kamailio" {
  name = "${var.env}-pool-kamailio"

  instances = google_compute_instance.kamailio[*].self_link

  health_checks = [
    google_compute_http_health_check.kamailio_external.name,
  ]
}

# Forwarding rule: UDP 5060 (SIP)
resource "google_compute_forwarding_rule" "kamailio_udp_sip" {
  name                  = "${var.env}-kamailio-fwd-udp-sip"
  region                = var.region
  ip_address            = google_compute_address.kamailio_lb_external.address
  ip_protocol           = "UDP"
  load_balancing_scheme = "EXTERNAL"
  port_range            = "5060"
  target                = google_compute_target_pool.kamailio.self_link
}

# Forwarding rule: TCP 5060-5061 (SIP + TLS)
resource "google_compute_forwarding_rule" "kamailio_tcp_sip" {
  name                  = "${var.env}-kamailio-fwd-tcp-sip"
  region                = var.region
  ip_address            = google_compute_address.kamailio_lb_external.address
  ip_protocol           = "TCP"
  load_balancing_scheme = "EXTERNAL"
  port_range            = "5060-5061"
  target                = google_compute_target_pool.kamailio.self_link
}

# Forwarding rule: TCP 443 (WSS)
resource "google_compute_forwarding_rule" "kamailio_tcp_wss" {
  name                  = "${var.env}-kamailio-fwd-tcp-wss"
  region                = var.region
  ip_address            = google_compute_address.kamailio_lb_external.address
  ip_protocol           = "TCP"
  load_balancing_scheme = "EXTERNAL"
  port_range            = "443"
  target                = google_compute_target_pool.kamailio.self_link
}

###############################################################################
# Kamailio Internal Load Balancer
###############################################################################

# Internal static IP for Kamailio internal LB
resource "google_compute_address" "kamailio_lb_internal" {
  name         = "${var.env}-kamailio-lb-internal"
  region       = var.region
  address_type = "INTERNAL"
  subnetwork   = google_compute_subnetwork.voipbin_main.id
}

# Health check for internal backend service
resource "google_compute_health_check" "kamailio_internal" {
  name               = "${var.env}-hc-kamailio-internal"
  check_interval_sec = 5
  timeout_sec        = 2

  http_health_check {
    port         = 5060
    request_path = "/health-check"
    response     = "OK"
  }
}

# Backend service for internal LB
resource "google_compute_region_backend_service" "kamailio_internal" {
  name          = "${var.env}-bs-kamailio-internal"
  region        = var.region
  health_checks = [google_compute_health_check.kamailio_internal.self_link]
  protocol      = "TCP"

  connection_draining_timeout_sec = 0

  backend {
    group          = google_compute_instance_group.kamailio.self_link
    balancing_mode = "CONNECTION"
  }
}

# Internal forwarding rule (all ports)
resource "google_compute_forwarding_rule" "kamailio_internal" {
  name                  = "${var.env}-kamailio-fwd-internal"
  region                = var.region
  ip_address            = google_compute_address.kamailio_lb_internal.address
  ip_protocol           = "TCP"
  load_balancing_scheme = "INTERNAL"
  all_ports             = true
  backend_service       = google_compute_region_backend_service.kamailio_internal.self_link
  network               = google_compute_network.voipbin.id
  subnetwork            = google_compute_subnetwork.voipbin_main.id
}
