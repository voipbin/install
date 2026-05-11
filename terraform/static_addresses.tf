###############################################################################
# Regional static IPs for externally exposed Kubernetes Services.
#
# These addresses are referenced by Service annotations in PR #3a via
# kubernetes.io/ingress.global-static-ip-name. They exist in this PR
# only as reservations so the addresses are stable across the
# subsequent Service-type flip.
###############################################################################

locals {
  external_services = toset([
    "api-manager",
    "hook-manager",
    "admin",
    "talk",
    "meet",
  ])
}

resource "google_compute_address" "external_service" {
  for_each = local.external_services

  name         = "${each.key}-static-ip"
  region       = var.region
  address_type = "EXTERNAL"
}
