# GKE cluster service account
resource "google_service_account" "sa_gke_nodes" {
  account_id   = "sa-${var.env}-gke-nodes"
  display_name = "GKE Node Pool Service Account"
}

resource "google_project_iam_member" "sa_gke_nodes_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.sa_gke_nodes.email}"
}

resource "google_project_iam_member" "sa_gke_nodes_metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.sa_gke_nodes.email}"
}

resource "google_project_iam_member" "sa_gke_nodes_storage_reader" {
  project = var.project_id
  role    = "roles/storage.objectViewer"
  member  = "serviceAccount:${google_service_account.sa_gke_nodes.email}"
}

# GKE cluster
resource "google_container_cluster" "voipbin" {
  provider = google-beta

  name     = "${var.env}-gke-cluster"
  location = var.gke_type == "regional" ? var.region : var.zone
  project  = var.project_id

  deletion_protection = false

  network    = google_compute_network.voipbin.id
  subnetwork = google_compute_subnetwork.voipbin_main.id

  release_channel {
    channel = "REGULAR"
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
    master_ipv4_cidr_block  = "10.32.0.0/28"

    master_global_access_config {
      enabled = true
    }
  }

  # Remove default node pool — use separately managed pool below
  remove_default_node_pool = true
  initial_node_count       = 1

  master_auth {
    client_certificate_config {
      issue_client_certificate = false
    }
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = "${var.env}-pods"
    services_secondary_range_name = "${var.env}-services"
  }

  timeouts {
    create = "30m"
    update = "30m"
    delete = "30m"
  }

  lifecycle {
    ignore_changes = [deletion_protection]
  }

  depends_on = [time_sleep.api_propagation]
}

# GKE node pool
resource "google_container_node_pool" "voipbin" {
  provider = google-beta

  name     = "${var.env}-node-pool"
  location = var.gke_type == "regional" ? var.region : var.zone
  cluster  = google_container_cluster.voipbin.name

  node_count = var.gke_node_count

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    preemptible  = false
    machine_type = var.gke_machine_type
    disk_size_gb = 100
    disk_type    = "pd-balanced"
    image_type   = "COS_CONTAINERD"

    service_account = google_service_account.sa_gke_nodes.email
    oauth_scopes = [
      "https://www.googleapis.com/auth/logging.write",
      "https://www.googleapis.com/auth/monitoring",
      "https://www.googleapis.com/auth/devstorage.read_only",
    ]

    metadata = {
      disable-legacy-endpoints = "true"
    }

    labels = {
      env     = var.env
      service = "gke"
    }

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }
  }

  upgrade_settings {
    max_surge       = 1
    max_unavailable = 0
    strategy        = "SURGE"
  }

  timeouts {
    create = "30m"
    update = "30m"
    delete = "30m"
  }
}
