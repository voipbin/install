# Terraform state bucket (hardened)
resource "google_storage_bucket" "terraform_state" {
  name     = "${var.project_id}-${var.env}-tf-state"
  location = var.region
  project  = var.project_id

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      num_newer_versions = 5
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [time_sleep.api_propagation]
}

# Media storage bucket
resource "google_storage_bucket" "media" {
  name     = "${var.project_id}-${var.env}-media"
  location = var.region
  project  = var.project_id

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = false
  }

  # Lifecycle policy: delete temp files after 90 days
  lifecycle_rule {
    condition {
      age          = 90
      matches_prefix = ["tmp/"]
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [time_sleep.api_propagation]
}
