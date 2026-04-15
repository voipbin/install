# KMS key ring for SOPS secret encryption
resource "google_kms_key_ring" "voipbin_sops" {
  name     = "${var.env}-sops"
  location = "global"

  depends_on = [time_sleep.api_propagation]

  lifecycle {
    prevent_destroy = true
  }
}

# KMS key for SOPS encrypt/decrypt — 90-day rotation
resource "google_kms_crypto_key" "voipbin_sops_key" {
  name            = "${var.env}-sops-key"
  key_ring        = google_kms_key_ring.voipbin_sops.id
  purpose         = "ENCRYPT_DECRYPT"
  rotation_period = "7776000s" # 90 days

  lifecycle {
    prevent_destroy = true
  }
}
