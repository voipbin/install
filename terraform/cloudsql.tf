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
      ipv4_enabled       = false
      private_network    = google_compute_network.voipbin.id
      ssl_mode           = "ENCRYPTED_ONLY"
      allocated_ip_range = google_compute_global_address.cloudsql_peering.name
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

  depends_on = [
    time_sleep.api_propagation,
    google_service_networking_connection.voipbin,
  ]
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

# -- Postgres (PR-D1) -------------------------------------------------
# Postgres admin password. Distinct from MySQL random_password.cloudsql_password
# so credentials can be rotated independently.
resource "random_password" "cloudsql_postgres_password" {
  length  = 24
  special = true
}

# Cloud SQL Postgres instance. Mirrors the MySQL instance shape and shares the
# same VPC peering range allocation. PR-D2 will add per-app databases and users.
resource "google_sql_database_instance" "voipbin_postgres" {
  name                = "${var.env}-postgres"
  database_version    = "POSTGRES_17"
  region              = var.region
  deletion_protection = true

  settings {
    tier              = "db-f1-micro"
    disk_size         = 10
    disk_type         = "PD_SSD"
    availability_type = var.gke_type == "regional" ? "REGIONAL" : "ZONAL"
    disk_autoresize   = true

    ip_configuration {
      ipv4_enabled       = false
      private_network    = google_compute_network.voipbin.id
      ssl_mode           = "ENCRYPTED_ONLY"
      allocated_ip_range = google_compute_global_address.cloudsql_peering.name
    }

    backup_configuration {
      enabled    = true
      start_time = "03:30" # UTC. Offset from MySQL's 03:00 to avoid IO overlap.
      # Postgres does not support binary_log_enabled (MySQL-only field).
      # point_in_time_recovery_enabled stays at provider default (false): dev
      # tier does not need WAL archive cost and daily backups suffice.
      backup_retention_settings {
        retained_backups = 3 # Dev tier. Halves backup billing vs default 7.
        retention_unit   = "COUNT"
      }
    }

    maintenance_window {
      day  = 7
      hour = 5 # UTC. Offset from MySQL's 04 to avoid concurrent maintenance.
    }
  }

  depends_on = [
    time_sleep.api_propagation,
    google_service_networking_connection.voipbin,
  ]
}

# Built-in postgres admin user. PR-D2 will add per-app users alongside this.
resource "google_sql_user" "voipbin_postgres" {
  name     = "postgres"
  instance = google_sql_database_instance.voipbin_postgres.name
  password = random_password.cloudsql_postgres_password.result
}
