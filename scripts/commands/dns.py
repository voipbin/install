"""The 'dns' command: DNS and domain configuration guide."""

import scripts.display as _display


_SUBDOMAINS = ["api", "admin", "talk", "meet", "sip"]
_EXAMPLE_DOMAIN = "example.com"
_EXAMPLE_IP = "1.2.3.4"


def print_dns_section1() -> None:
    """Section 1: DNS A records guide."""
    _display.print_header("DNS Records")
    _display.console.print("  [dim]" + "─" * 50 + "[/dim]")
    _display.console.print()
    _display.console.print("  VoIPBin requires the following DNS A records at your registrar.")
    _display.console.print("  All subdomains point to the same load balancer IP.")
    _display.console.print()
    for sub in _SUBDOMAINS:
        fqdn = f"{sub}.{_EXAMPLE_DOMAIN}"
        _display.console.print(f"    [bold]{fqdn:<28}[/bold] A    {_EXAMPLE_IP}")
    _display.console.print()
    _display.console.print("  For [bold]auto[/bold] DNS mode: delegate your domain to the GCP nameservers")
    _display.console.print("  printed after apply completes. GCP then manages the A records.")
    _display.console.print()
    _display.console.print("  DNS propagation can take up to 48 hours.")
    _display.console.print("  Once complete, run: [bold]voipbin-install verify[/bold]")
    _display.console.print()


def print_dns_section2() -> None:
    """Section 2: Kubernetes ConfigMap guide."""
    _display.print_header("Kubernetes — voipbin-config (namespace: bin-manager)")
    _display.console.print("  [dim]" + "─" * 50 + "[/dim]")
    _display.console.print()
    _display.console.print("  The following domain value is set in the ConfigMap during deployment:")
    _display.console.print()
    _display.console.print(f"    [bold]DOMAIN[/bold]    {_EXAMPLE_DOMAIN}")
    _display.console.print()
    _display.console.print("  If your backend services also need [bold]DOMAIN_NAME_TRUNK[/bold] or")
    _display.console.print(f"  [bold]DOMAIN_NAME_EXTENSION[/bold], add them to [dim]k8s/backend/configmap.yaml[/dim]")
    _display.console.print("  before running apply. Audit which services consume these values first.")
    _display.console.print()
