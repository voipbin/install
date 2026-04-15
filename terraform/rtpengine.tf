# RTPEngine service account
resource "google_service_account" "sa_rtpengine" {
  account_id   = "sa-${var.env}-rtpengine"
  display_name = "RTPEngine VM Service Account"
}

resource "google_project_iam_member" "sa_rtpengine_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.sa_rtpengine.email}"
}

resource "google_project_iam_member" "sa_rtpengine_metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.sa_rtpengine.email}"
}

# Static external IPs for RTP media (one per instance)
resource "google_compute_address" "rtpengine" {
  count = var.rtpengine_count

  name         = "external-ip-rtpengine-${var.env}-${var.zone}-${count.index}"
  region       = var.region
  address_type = "EXTERNAL"
}

# RTPEngine VM instances
resource "google_compute_instance" "rtpengine" {
  count = var.rtpengine_count

  name         = "instance-rtpengine-${var.env}-${var.zone}-${count.index}"
  machine_type = var.vm_machine_type
  zone         = var.zone

  tags = ["rtpengine"]

  labels = {
    service = "rtpengine"
    env     = var.env
  }

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = 30
      type  = "pd-standard"
    }
  }

  # Static external IP for RTP media traffic
  network_interface {
    subnetwork = google_compute_subnetwork.voipbin_main.id

    access_config {
      nat_ip = google_compute_address.rtpengine[count.index].address
    }
  }

  service_account {
    email = google_service_account.sa_rtpengine.email
    scopes = [
      "https://www.googleapis.com/auth/logging.write",
      "https://www.googleapis.com/auth/monitoring.write",
    ]
  }

  metadata = {
    enable-oslogin = "TRUE"
  }

  allow_stopping_for_update = true

  depends_on = [time_sleep.api_propagation]
}
