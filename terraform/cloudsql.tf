# Random password for Cloud SQL user
resource "random_password" "cloudsql_password" {
  length  = 24
  special = true
}

# Cloud SQL MySQL instance
resource "google_sql_database_instance" "voipbin" {
  name                = "${var.env}-mysql"
  database_version    = "MYSQL_8_0"
  region              = var.region
  deletion_protection = true

  settings {
    tier              = "db-f1-micro"
    disk_size         = 10
    disk_type         = "PD_SSD"
    availability_type = var.gke_type == "regional" ? "REGIONAL" : "ZONAL"
    disk_autoresize   = true

    ip_configuration {
      ipv4_enabled = true
      require_ssl  = true
    }

    backup_configuration {
      enabled            = true
      binary_log_enabled = true
      start_time         = "03:00"
    }

    maintenance_window {
      day  = 7
      hour = 4
    }
  }

  depends_on = [time_sleep.api_propagation]
}

# Database
resource "google_sql_database" "voipbin" {
  name     = "voipbin"
  instance = google_sql_database_instance.voipbin.name
}

# Database user
resource "google_sql_user" "voipbin" {
  name     = "voipbin"
  instance = google_sql_database_instance.voipbin.name
  password = random_password.cloudsql_password.result
}

# Cloud SQL Proxy service account
resource "google_service_account" "sa_cloudsql_proxy" {
  account_id   = "sa-${var.env}-cloudsql-proxy"
  display_name = "Cloud SQL Proxy Service Account"
}

resource "google_project_iam_member" "sa_cloudsql_proxy_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.sa_cloudsql_proxy.email}"
}
