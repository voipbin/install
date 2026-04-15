# Enable required GCP APIs
locals {
  required_apis = [
    "compute.googleapis.com",
    "container.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "sqladmin.googleapis.com",
    "dns.googleapis.com",
    "cloudkms.googleapis.com",
    "servicenetworking.googleapis.com",
    "storage.googleapis.com",
    "iap.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "certificatemanager.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each = toset(local.required_apis)

  project = var.project_id
  service = each.value

  disable_on_destroy = false
}

# Wait for API enablement to propagate before creating resources
resource "time_sleep" "api_propagation" {
  depends_on = [google_project_service.apis]

  create_duration = "60s"
}
