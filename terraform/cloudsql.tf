###############################################################################
# Cloud SQL MySQL — instance, application databases, application users
###############################################################################
# PR-D2a: legacy `voipbin` MySQL database and user destroyed; replaced with
# per-app databases (bin_manager, asterisk) and users (bin-manager, asterisk,
# call-manager, kamailioro). Admin user `root@%` is built-in and managed
# outside terraform via `gcloud sql users set-password root`. See
# docs/operations/cloud-sql-credentials.md.

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
      ssl_mode           = "ALLOW_UNENCRYPTED_AND_ENCRYPTED"
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

# -- MySQL application databases ----------------------------------------------
# Production parity: charset utf8mb3 / collation utf8mb3_general_ci.
# utf8mb3 is deprecated upstream of MySQL 8.4 but matches production; the
# utf8mb4 migration is a coordinated schema PR (see docs/follow-ups.md).

resource "google_sql_database" "voipbin_mysql_bin_manager" {
  name      = "bin_manager"
  instance  = google_sql_database_instance.voipbin.name
  charset   = "utf8mb3"
  collation = "utf8mb3_general_ci"
}

resource "google_sql_database" "voipbin_mysql_asterisk" {
  name      = "asterisk"
  instance  = google_sql_database_instance.voipbin.name
  charset   = "utf8mb3"
  collation = "utf8mb3_general_ci"
}

# -- MySQL random passwords ---------------------------------------------------
# override_special locked to RFC 3986 §3.2.1 userinfo-safe subset:
#   ! * +  → sub-delims (RFC §2.2)
#   - . _ ~ → unreserved (RFC §2.3)
# All 7 chars are also literal-safe inside go-sql-driver/mysql DSN userinfo
# (hand-written parser splits on last `@`, password at first `:`). 69-char
# alphabet (62 alnum + 7 special) × 24 chars ≈ 146 bits entropy.

resource "random_password" "mysql_bin_manager" {
  length           = 24
  special          = true
  override_special = "!*+-._~"
}

resource "random_password" "mysql_asterisk" {
  length           = 24
  special          = true
  override_special = "!*+-._~"
}

resource "random_password" "mysql_call_manager" {
  length           = 24
  special          = true
  override_special = "!*+-._~"
}

resource "random_password" "mysql_kamailioro" {
  length           = 24
  special          = true
  override_special = "!*+-._~"
}

# -- MySQL application users --------------------------------------------------
# User names use hyphens (production parity). bin-manager / asterisk /
# call-manager have host omitted → defaults to `%`. kamailioro is
# network-pinned to the private VPC range; the slash in the host string means
# we do NOT register this user in the reconcile registry (the provider's
# import id format `{project}/{instance}/{host}/{name}` collides with the
# slash and parses ambiguously). `terraform apply` creates the user on first
# run; state persists thereafter.

resource "google_sql_user" "voipbin_mysql_bin_manager" {
  name     = "bin-manager"
  instance = google_sql_database_instance.voipbin.name
  password = random_password.mysql_bin_manager.result
}

resource "google_sql_user" "voipbin_mysql_asterisk" {
  name     = "asterisk"
  instance = google_sql_database_instance.voipbin.name
  password = random_password.mysql_asterisk.result
}

resource "google_sql_user" "voipbin_mysql_call_manager" {
  name     = "call-manager"
  instance = google_sql_database_instance.voipbin.name
  password = random_password.mysql_call_manager.result
}

resource "google_sql_user" "voipbin_mysql_kamailioro" {
  name     = "kamailioro"
  instance = google_sql_database_instance.voipbin.name
  host     = "10.0.0.0/255.0.0.0"
  password = random_password.mysql_kamailioro.result
}

###############################################################################
# Cloud SQL Postgres — instance (PR-D1), application database/user (PR-D2a)
###############################################################################
# Postgres admin password. Distinct from MySQL admin so credentials can be
# rotated independently.
resource "random_password" "cloudsql_postgres_password" {
  length  = 24
  special = true
}

# Cloud SQL Postgres instance. Mirrors the MySQL instance shape and shares the
# same VPC peering range allocation.
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
      ssl_mode           = "ALLOW_UNENCRYPTED_AND_ENCRYPTED"
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

# Built-in postgres admin user.
resource "google_sql_user" "voipbin_postgres" {
  name     = "postgres"
  instance = google_sql_database_instance.voipbin_postgres.name
  password = random_password.cloudsql_postgres_password.result
}

# -- Postgres application database & user (PR-D2a) ----------------------------

resource "random_password" "postgres_bin_manager" {
  length           = 24
  special          = true
  override_special = "!*+-._~"
}

resource "google_sql_database" "voipbin_postgres_bin_manager" {
  name      = "bin_manager"
  instance  = google_sql_database_instance.voipbin_postgres.name
  charset   = "UTF8"
  collation = "en_US.UTF8"
}

resource "google_sql_user" "voipbin_postgres_bin_manager" {
  name     = "bin-manager"
  instance = google_sql_database_instance.voipbin_postgres.name
  password = random_password.postgres_bin_manager.result
}
