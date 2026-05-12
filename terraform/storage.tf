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

# Recordings bucket — versioned, regional, uniform IAM (PR-G).
resource "google_storage_bucket" "recordings" {
  name     = "${var.env}-voipbin-recordings"
  location = var.region
  project  = var.project_id

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  depends_on = [time_sleep.api_propagation]
}

# Tmp bucket — non-versioned, 7-day lifecycle delete (PR-G).
resource "google_storage_bucket" "tmp" {
  name     = "${var.env}-voipbin-tmp"
  location = var.region
  project  = var.project_id

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true

  versioning {
    enabled = false
  }

  lifecycle_rule {
    condition {
      age = 7
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
