"""JSON Schema for VoIPBin installer config.yaml validation."""

CONFIG_SCHEMA = {
    "type": "object",
    "required": ["gcp_project_id", "region", "domain"],
    "properties": {
        "gcp_project_id": {
            "type": "string",
            "minLength": 6,
            "description": "GCP project ID",
        },
        "region": {
            "type": "string",
            "description": "GCP region for deployment",
        },
        "zone": {
            "type": "string",
            "description": "GCP zone (derived from region)",
        },
        "gke_type": {
            "type": "string",
            "enum": ["zonal", "regional"],
            "description": "GKE cluster type",
        },
        "tls_strategy": {
            "type": "string",
            "enum": ["self-signed", "byoc"],
            "description": "TLS certificate strategy",
        },
        "image_tag_strategy": {
            "type": "string",
            "enum": ["latest", "pinned"],
            "description": "Docker image tag strategy",
        },
        "domain": {
            "type": "string",
            "pattern": r"^[a-z0-9][a-z0-9.\-]+[a-z0-9]$",
            "description": "Base domain for VoIPBin services",
        },
        "dns_mode": {
            "type": "string",
            "enum": ["auto", "manual"],
            "description": "DNS management mode",
        },
        "gke_machine_type": {
            "type": "string",
        },
        "gke_node_count": {
            "type": "integer",
            "minimum": 1,
        },
        "vm_machine_type": {
            "type": "string",
        },
        "kamailio_count": {
            "type": "integer",
            "minimum": 1,
        },
        "rtpengine_count": {
            "type": "integer",
            "minimum": 1,
        },
        "installer_version": {
            "type": "string",
        },
        "init_timestamp": {
            "type": "string",
        },
    },
    "additionalProperties": False,
}
