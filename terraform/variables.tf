variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region for resources"
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "GCP zone for zonal resources"
  type        = string
  default     = "us-central1-a"
}

variable "gke_type" {
  description = "GKE cluster type: zonal or regional"
  type        = string
  default     = "zonal"

  validation {
    condition     = contains(["zonal", "regional"], var.gke_type)
    error_message = "gke_type must be either 'zonal' or 'regional'."
  }
}

variable "domain" {
  description = "Primary domain name for the VoIPBin deployment"
  type        = string
}

variable "dns_mode" {
  description = "DNS management mode: 'auto' creates Cloud DNS zone, 'manual' skips DNS resources"
  type        = string
  default     = "auto"

  validation {
    condition     = contains(["auto", "manual"], var.dns_mode)
    error_message = "dns_mode must be either 'auto' or 'manual'."
  }
}

variable "tls_strategy" {
  description = "TLS certificate strategy: 'letsencrypt' or 'self-signed'"
  type        = string
  default     = "letsencrypt"

  validation {
    condition     = contains(["letsencrypt", "self-signed"], var.tls_strategy)
    error_message = "tls_strategy must be either 'letsencrypt' or 'self-signed'."
  }
}

variable "gke_machine_type" {
  description = "Machine type for GKE node pool"
  type        = string
  default     = "n1-standard-2"
}

variable "gke_node_count" {
  description = "Number of nodes per zone in GKE node pool"
  type        = number
  default     = 2
}

variable "vm_machine_type" {
  description = "Machine type for Kamailio and RTPEngine VMs"
  type        = string
  default     = "f1-micro"
}

variable "kamailio_count" {
  description = "Number of Kamailio VM instances"
  type        = number
  default     = 2
}

variable "rtpengine_count" {
  description = "Number of RTPEngine VM instances"
  type        = number
  default     = 2
}

variable "env" {
  description = "Environment label applied to all resources"
  type        = string
  default     = "voipbin"
}
