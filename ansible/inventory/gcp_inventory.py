#!/usr/bin/env python3
"""Dynamic Ansible inventory from Terraform outputs.

Reads Terraform state from ../terraform/ and generates inventory with
groups: kamailio, rtpengine.

Each host gets IAP tunnel SSH args so Ansible can reach private VMs
without a public IP.

Usage:
  # List inventory (Ansible calls this automatically)
  ./gcp_inventory.py --list

  # Show host variables
  ./gcp_inventory.py --host <hostname>
"""

import argparse
import json
import os
import subprocess
import sys


def get_terraform_outputs(terraform_dir: str) -> dict:
    """Run 'terraform output -json' and return parsed outputs."""
    try:
        result = subprocess.run(
            ["terraform", "output", "-json"],
            cwd=terraform_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        print(
            f"Error running terraform output: {e.stderr}",
            file=sys.stderr,
        )
        return {}
    except FileNotFoundError:
        print(
            "terraform not found in PATH",
            file=sys.stderr,
        )
        return {}
    except json.JSONDecodeError:
        print(
            "Failed to parse terraform output as JSON",
            file=sys.stderr,
        )
        return {}


def extract_value(outputs: dict, key: str, default="") -> str:
    """Extract a value from Terraform outputs dict.

    Terraform output -json returns: {"key": {"value": ..., "type": ...}}
    """
    entry = outputs.get(key, {})
    if isinstance(entry, dict) and "value" in entry:
        return entry["value"]
    return default


def build_iap_ssh_args(project_id: str, zone: str) -> str:
    """Build SSH ProxyCommand for IAP tunnel."""
    return (
        "-o StrictHostKeyChecking=no "
        "-o UserKnownHostsFile=/dev/null "
        f'-o ProxyCommand="gcloud compute start-iap-tunnel %h %p '
        f"--listen-on-stdin "
        f"--project={project_id} "
        f'--zone={zone}"'
    )


def build_inventory(outputs: dict) -> dict:
    """Build Ansible inventory dict from Terraform outputs."""
    project_id = extract_value(outputs, "project_id", "")
    region = extract_value(outputs, "region", "us-central1")
    zone = extract_value(outputs, "zone", f"{region}-a")

    # Extract VM instance names/IPs from Terraform outputs
    # Expected outputs: kamailio_instance_names, kamailio_internal_ips,
    #                   rtpengine_instance_names, rtpengine_internal_ips
    kamailio_names = extract_value(outputs, "kamailio_instance_names", [])
    kamailio_ips = extract_value(outputs, "kamailio_internal_ips", [])
    rtpengine_names = extract_value(outputs, "rtpengine_instance_names", [])
    rtpengine_ips = extract_value(outputs, "rtpengine_internal_ips", [])

    # Extract LB IPs and other shared outputs
    kamailio_external_lb_ip = extract_value(
        outputs, "kamailio_external_lb_ip", ""
    )
    kamailio_internal_lb_ip = extract_value(
        outputs, "kamailio_internal_lb_ip", ""
    )
    rtpengine_lb_ip = extract_value(outputs, "rtpengine_lb_ip", "")
    redis_lb_ip = extract_value(outputs, "redis_lb_ip", "")
    rabbitmq_lb_ip = extract_value(outputs, "rabbitmq_lb_ip", "")
    asterisk_call_lb_ip = extract_value(
        outputs, "asterisk_call_lb_ip", ""
    )
    asterisk_registrar_lb_ip = extract_value(
        outputs, "asterisk_registrar_lb_ip", ""
    )
    asterisk_conference_lb_ip = extract_value(
        outputs, "asterisk_conference_lb_ip", ""
    )

    iap_ssh_args = build_iap_ssh_args(project_id, zone)

    # Build hostvars
    hostvars = {}
    kamailio_hosts = []
    rtpengine_hosts = []

    for i, name in enumerate(kamailio_names):
        ip = kamailio_ips[i] if i < len(kamailio_ips) else ""
        hostvars[name] = {
            "ansible_host": name,
            "ansible_user": os.environ.get("ANSIBLE_USER", "sa_ansible"),
            "ansible_ssh_common_args": iap_ssh_args,
            "internal_ip": ip,
            "gcp_project_id": project_id,
            "gcp_zone": zone,
            "gcp_region": region,
        }
        kamailio_hosts.append(name)

    for i, name in enumerate(rtpengine_names):
        ip = rtpengine_ips[i] if i < len(rtpengine_ips) else ""
        hostvars[name] = {
            "ansible_host": name,
            "ansible_user": os.environ.get("ANSIBLE_USER", "sa_ansible"),
            "ansible_ssh_common_args": iap_ssh_args,
            "internal_ip": ip,
            "gcp_project_id": project_id,
            "gcp_zone": zone,
            "gcp_region": region,
        }
        rtpengine_hosts.append(name)

    inventory = {
        "_meta": {"hostvars": hostvars},
        "all": {
            "vars": {
                "gcp_project_id": project_id,
                "region": region,
                "zone": zone,
                "kamailio_external_lb_ip": kamailio_external_lb_ip,
                "kamailio_internal_lb_ip": kamailio_internal_lb_ip,
                "rtpengine_lb_ip": rtpengine_lb_ip,
                "redis_lb_ip": redis_lb_ip,
                "rabbitmq_lb_ip": rabbitmq_lb_ip,
                "asterisk_call_lb_ip": asterisk_call_lb_ip,
                "asterisk_registrar_lb_ip": asterisk_registrar_lb_ip,
                "asterisk_conference_lb_ip": asterisk_conference_lb_ip,
            },
            "children": ["kamailio", "rtpengine"],
        },
        "kamailio": {"hosts": kamailio_hosts},
        "rtpengine": {"hosts": rtpengine_hosts},
    }

    return inventory


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dynamic Ansible inventory from Terraform outputs"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all hosts and groups",
    )
    parser.add_argument(
        "--host",
        type=str,
        help="Show variables for a specific host",
    )
    args = parser.parse_args()

    # Locate terraform directory relative to this script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    terraform_dir = os.environ.get(
        "TERRAFORM_DIR",
        os.path.join(script_dir, "..", "..", "terraform"),
    )
    terraform_dir = os.path.normpath(terraform_dir)

    outputs = get_terraform_outputs(terraform_dir)
    inventory = build_inventory(outputs)

    if args.host:
        hostvars = inventory.get("_meta", {}).get("hostvars", {})
        host_data = hostvars.get(args.host, {})
        print(json.dumps(host_data, indent=2))
    else:
        # Default: --list
        print(json.dumps(inventory, indent=2))


if __name__ == "__main__":
    main()
