# Kamailio service account
resource "google_service_account" "sa_kamailio" {
  account_id   = "sa-${var.env}-kamailio"
  display_name = "Kamailio VM Service Account"
}

resource "google_project_iam_member" "sa_kamailio_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.sa_kamailio.email}"
}

resource "google_project_iam_member" "sa_kamailio_metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.sa_kamailio.email}"
}

# Kamailio VM instances
resource "google_compute_instance" "kamailio" {
  count = var.kamailio_count

  name         = "instance-kamailio-${var.env}-${var.zone}-${count.index}"
  machine_type = var.vm_machine_type
  zone         = var.zone

  tags = ["kamailio"]

  labels = {
    service = "kamailio"
    env     = var.env
  }

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = 30
      type  = "pd-standard"
    }
  }

  # No access_config — no public IP. Use LB for external access.
  network_interface {
    subnetwork = google_compute_subnetwork.voipbin_main.id
  }

  service_account {
    email = google_service_account.sa_kamailio.email
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
