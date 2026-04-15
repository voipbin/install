# Cloud DNS managed zone — only created when dns_mode is "auto"
resource "google_dns_managed_zone" "voipbin" {
  count = var.dns_mode == "auto" ? 1 : 0

  name        = "${var.env}-dns-zone"
  dns_name    = "${var.domain}."
  description = "DNS zone for ${var.domain}"

  depends_on = [time_sleep.api_propagation]
}

# DNS A record: api.<domain>
resource "google_dns_record_set" "api" {
  count = var.dns_mode == "auto" ? 1 : 0

  name         = "api.${var.domain}."
  type         = "A"
  ttl          = 300
  managed_zone = google_dns_managed_zone.voipbin[0].name
  rrdatas      = [google_compute_address.kamailio_lb_external.address]
}

# DNS A record: admin.<domain>
resource "google_dns_record_set" "admin" {
  count = var.dns_mode == "auto" ? 1 : 0

  name         = "admin.${var.domain}."
  type         = "A"
  ttl          = 300
  managed_zone = google_dns_managed_zone.voipbin[0].name
  rrdatas      = [google_compute_address.kamailio_lb_external.address]
}

# DNS A record: talk.<domain>
resource "google_dns_record_set" "talk" {
  count = var.dns_mode == "auto" ? 1 : 0

  name         = "talk.${var.domain}."
  type         = "A"
  ttl          = 300
  managed_zone = google_dns_managed_zone.voipbin[0].name
  rrdatas      = [google_compute_address.kamailio_lb_external.address]
}

# DNS A record: meet.<domain>
resource "google_dns_record_set" "meet" {
  count = var.dns_mode == "auto" ? 1 : 0

  name         = "meet.${var.domain}."
  type         = "A"
  ttl          = 300
  managed_zone = google_dns_managed_zone.voipbin[0].name
  rrdatas      = [google_compute_address.kamailio_lb_external.address]
}

# DNS A record: sip.<domain>
resource "google_dns_record_set" "sip" {
  count = var.dns_mode == "auto" ? 1 : 0

  name         = "sip.${var.domain}."
  type         = "A"
  ttl          = 300
  managed_zone = google_dns_managed_zone.voipbin[0].name
  rrdatas      = [google_compute_address.kamailio_lb_external.address]
}
