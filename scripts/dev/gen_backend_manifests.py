"""One-shot generator: rewrites k8s/backend/services/*.yaml from secret_schema.

Run from worktree root:
    python scripts/dev/gen_backend_manifests.py

Idempotent. Preserves Service blocks by reading existing files and substituting
only the Deployment block's env / ports sections.

For simplicity we fully regenerate the Deployment block from schema +
existing per-service Service-block override list, then append the matching
Service block extracted from current file (since Services are heterogeneous).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.secret_schema import BIN_SERVICE_WIRING  # noqa: E402


REPO = Path(__file__).resolve().parents[2]
SERVICES = REPO / "k8s" / "backend" / "services"


def render_ports(ports: list) -> str:
    lines = []
    for port, name in ports:
        lines.append(f"            - containerPort: {port}")
        lines.append(f"              name: {name}")
    return "\n".join(lines)


def render_env(wiring: dict) -> str:
    lines = []
    # Secret refs (preserve declared order)
    for pod_env, secret_key in wiring.get("secret_env", []):
        lines.append(f"            - name: {pod_env}")
        lines.append( "              valueFrom:")
        lines.append( "                secretKeyRef:")
        lines.append( "                  name: voipbin")
        lines.append(f"                  key: {secret_key}")
    # Field refs
    for pod_env, fp in wiring.get("field_env", []):
        lines.append(f"            - name: {pod_env}")
        lines.append( "              valueFrom:")
        lines.append( "                fieldRef:")
        lines.append(f"                  fieldPath: {fp}")
    # Literals
    for pod_env, val in wiring.get("literal_env", []):
        lines.append(f"            - name: {pod_env}")
        lines.append(f"              value: {yaml_str(val)}")
    return "\n".join(lines)


def yaml_str(v: str) -> str:
    # Always quote to keep ":2112" etc. clean. PLACEHOLDER tokens stay unquoted-safe.
    return '"' + v.replace('"', '\\"') + '"'


def existing_service_block(path: Path) -> str:
    """Return the Service portion of the existing manifest (everything after first `---`)."""
    if not path.exists():
        return ""
    text = path.read_text()
    if "---" not in text:
        return ""
    parts = text.split("---", 1)
    return "---" + parts[1].rstrip() + "\n"


def render_deployment(name: str, wiring: dict) -> str:
    ports = render_ports(wiring["ports"])
    envs = render_env(wiring)
    # First port name is used for liveness/readiness probe.
    probe_port = wiring["ports"][0][0]
    return f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  namespace: bin-manager
  labels:
    app: {name}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {name}
  template:
    metadata:
      labels:
        app: {name}
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/path: "/metrics"
        prometheus.io/port: "2112"
    spec:
      containers:
        - name: {name}
          image: voipbin/{name}
          ports:
{ports}
          env:
{envs}
          resources:
            requests:
              cpu: "50m"
              memory: "64Mi"
            limits:
              cpu: "200m"
              memory: "256Mi"
          livenessProbe:
            httpGet:
              path: /health
              port: {probe_port}
            initialDelaySeconds: 15
            periodSeconds: 10
          readinessProbe:
            httpGet:
              path: /health
              port: {probe_port}
            initialDelaySeconds: 5
            periodSeconds: 5
"""


def main() -> int:
    for name, wiring in BIN_SERVICE_WIRING.items():
        path = SERVICES / f"{name}.yaml"
        svc = existing_service_block(path)
        depl = render_deployment(name, wiring)
        # hook-manager Service block needs port 80 added if missing.
        if name == "hook-manager" and svc and "port: 80" not in svc:
            svc = svc.replace(
                "  ports:\n    - name: http\n      port: 80\n      targetPort: 80\n    - name: https",
                "  ports:\n    - name: http\n      port: 80\n      targetPort: 80\n    - name: https",
            )
        path.write_text(depl + (svc if svc else ""))
        print(f"wrote {path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
