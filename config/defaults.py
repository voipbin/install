"""Default values and option lists for VoIPBin installer."""

REGIONS = [
    {"id": "us-central1", "name": "us-central1 (Iowa, USA)", "note": "Cheapest, free tier eligible"},
    {"id": "us-east1", "name": "us-east1 (S. Carolina, USA)", "note": "Free tier eligible"},
    {"id": "us-west1", "name": "us-west1 (Oregon, USA)", "note": "Free tier eligible"},
    {"id": "europe-west1", "name": "europe-west1 (Belgium, EU)", "note": "Cheapest EU, GDPR"},
    {"id": "europe-west4", "name": "europe-west4 (Netherlands, EU)", "note": "GDPR"},
    {"id": "asia-east1", "name": "asia-east1 (Taiwan, Asia)", "note": "Cheapest Asia"},
    {"id": "custom", "name": "Custom region", "note": "Enter manually"},
]

GKE_TYPES = [
    {"id": "zonal", "name": "Zonal", "note": "$0/mo control plane, less resilient"},
    {"id": "regional", "name": "Regional", "note": "~$73/mo control plane, HA"},
]

TLS_STRATEGIES = [
    {
        "id": "self-signed",
        "name": "Self-signed (installer-managed)",
        "note": "Fresh install ready out of the box; replace before production",
    },
    {
        "id": "byoc",
        "name": "Bring Your Own Cert",
        "note": "Operator provides cert/key via voipbin-secret + voipbin-tls Secrets",
    },
]

IMAGE_TAG_STRATEGIES = [
    {"id": "latest", "name": "latest", "note": "Always newest, simplest"},
    {"id": "pinned", "name": "Pinned SHA", "note": "Reproducible, from versions.yaml"},
]

DNS_MODES = [
    {"id": "auto", "name": "Yes — auto-create Cloud DNS zone", "note": "Delegate NS to GCP"},
    {"id": "manual", "name": "No — manual DNS", "note": "Installer shows required records"},
]

# Default infrastructure sizing
DEFAULT_GKE_MACHINE_TYPE = "n1-standard-2"
DEFAULT_GKE_NODE_COUNT = 2
DEFAULT_VM_MACHINE_TYPE = "f1-micro"
DEFAULT_KAMAILIO_COUNT = 1
DEFAULT_RTPENGINE_COUNT = 1
