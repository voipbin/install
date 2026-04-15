# Remote state stored in GCS.
# The bucket name is passed via partial backend configuration:
#   terraform init -backend-config="bucket=<BUCKET_NAME>"
terraform {
  backend "gcs" {
    prefix = "terraform/state"
  }
}
