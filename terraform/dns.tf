# Cloud DNS managed zone — only created when dns_mode is "auto"
resource "google_dns_managed_zone" "voipbin" {
  count = var.dns_mode == "auto" ? 1 : 0

  name        = "${var.env}-dns-zone"
  dns_name    = "${var.domain}."
  description = "DNS zone for ${var.domain}"

  depends_on = [time_sleep.api_propagation]
}

# DNS A record: api.<domain> → api-manager per-service external IP
resource "google_dns_record_set" "api" {
  count = var.dns_mode == "auto" ? 1 : 0

  name         = "api.${var.domain}."
  type         = "A"
  ttl          = 300
  managed_zone = google_dns_managed_zone.voipbin[0].name
  rrdatas      = [google_compute_address.external_service["api-manager"].address]
}

# DNS A record: admin.<domain> → admin per-service external IP
resource "google_dns_record_set" "admin" {
  count = var.dns_mode == "auto" ? 1 : 0

  name         = "admin.${var.domain}."
  type         = "A"
  ttl          = 300
  managed_zone = google_dns_managed_zone.voipbin[0].name
  rrdatas      = [google_compute_address.external_service["admin"].address]
}

# DNS A record: talk.<domain> → talk per-service external IP
resource "google_dns_record_set" "talk" {
  count = var.dns_mode == "auto" ? 1 : 0

  name         = "talk.${var.domain}."
  type         = "A"
  ttl          = 300
  managed_zone = google_dns_managed_zone.voipbin[0].name
  rrdatas      = [google_compute_address.external_service["talk"].address]
}

# DNS A record: meet.<domain> → meet per-service external IP
resource "google_dns_record_set" "meet" {
  count = var.dns_mode == "auto" ? 1 : 0

  name         = "meet.${var.domain}."
  type         = "A"
  ttl          = 300
  managed_zone = google_dns_managed_zone.voipbin[0].name
  rrdatas      = [google_compute_address.external_service["meet"].address]
}

# DNS A record: hook.<domain> (webhook delivery edge — CPO decision #6)
resource "google_dns_record_set" "hook" {
  count = var.dns_mode == "auto" ? 1 : 0

  name         = "hook.${var.domain}."
  type         = "A"
  ttl          = 300
  managed_zone = google_dns_managed_zone.voipbin[0].name
  rrdatas      = [google_compute_address.external_service["hook-manager"].address]
}

# DNS A record: sip.<domain> — SIP edge (Kamailio external LB); UNCHANGED
resource "google_dns_record_set" "sip" {
  count = var.dns_mode == "auto" ? 1 : 0

  name         = "sip.${var.domain}."
  type         = "A"
  ttl          = 300
  managed_zone = google_dns_managed_zone.voipbin[0].name
  rrdatas      = [google_compute_address.kamailio_lb_external.address]
}
