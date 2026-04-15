# Google Cloud provider configuration
# Uses Application Default Credentials (ADC) — no hardcoded credentials
provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}
