"""Rich TUI display helpers for VoIPBin installer."""

from typing import Optional, Sequence

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

console = Console()

BANNER_TEXT = (
    "[bold cyan]VoIPBin[/bold cyan] [dim]Installer[/dim]"
)


def print_banner() -> None:
    console.print()
    console.print(Panel(BANNER_TEXT, expand=False, border_style="cyan"))
    console.print()


def print_check(name: str, version: str, ok: bool, required: str = "") -> None:
    """Print a prerequisite check result line."""
    symbol = "[green]✓[/green]" if ok else "[red]✗[/red]"
    ver_info = f"v{version}" if version else "not found"
    req = f"(>= {required})" if required else ""
    console.print(f"    {symbol} {name} {ver_info} {req}")


def print_step(msg: str) -> None:
    console.print(f"  {msg}")


def print_success(msg: str) -> None:
    console.print(f"    [green]✓[/green] {msg}")


def print_error(msg: str) -> None:
    console.print(f"    [red]✗[/red] {msg}")


def print_warning(msg: str) -> None:
    console.print(f"    [yellow]⚠[/yellow] {msg}")


def print_header(msg: str) -> None:
    console.print(f"\n  [bold]{msg}[/bold]")


def print_result_box(lines: list[str], style: str = "green") -> None:
    """Print a bordered result box."""
    body = "\n".join(f"  {line}" for line in lines)
    console.print(Panel(body, border_style=style, expand=False))


def create_progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=console,
    )


def prompt_choice(
    title: str,
    options: Sequence[dict],
    default: int = 1,
) -> int:
    """Show numbered options and return the 1-based index chosen."""
    console.print(f"\n  [bold]{title}[/bold]")
    for i, opt in enumerate(options, 1):
        name = opt.get("name", opt.get("id", ""))
        note = opt.get("note", "")
        note_str = f" — {note}" if note else ""
        console.print(f"      [{i}] {name}{note_str}")

    while True:
        raw = Prompt.ask(f"    Choice", default=str(default), console=console)
        try:
            idx = int(raw)
            if 1 <= idx <= len(options):
                return idx
        except ValueError:
            pass
        console.print(f"    [red]Please enter a number 1-{len(options)}[/red]")


def prompt_text(
    title: str,
    default: str = "",
    validate_fn=None,
) -> str:
    """Prompt for free-text input with optional validation."""
    while True:
        value = Prompt.ask(f"    {title}", default=default or None, console=console)
        if validate_fn:
            error = validate_fn(value)
            if error:
                console.print(f"    [red]{error}[/red]")
                continue
        return value


def confirm(msg: str, default: bool = False) -> bool:
    return Confirm.ask(f"    {msg}", default=default, console=console)


def print_cost_table(gke_type: str) -> None:
    """Print estimated monthly cost table."""
    table = Table(title="Estimated Monthly Cost", show_header=True, header_style="bold")
    table.add_column("Resource")
    table.add_column("Type")
    table.add_column("Cost/mo", justify="right")

    is_regional = gke_type == "regional"
    gke_ctrl = "~$73" if is_regional else "$0 (free)"
    total = "~$255" if is_regional else "~$182"

    rows = [
        ("GKE Control Plane", "1 cluster", gke_ctrl),
        ("GKE Nodes", "2x n1-standard-2", "~$97"),
        ("Kamailio VMs", "2x f1-micro", "~$12"),
        ("RTPEngine VMs", "2x f1-micro", "~$12"),
        ("Cloud SQL", "db-f1-micro MySQL", "~$13"),
        ("Cloud NAT", "Gateway", "~$10"),
        ("External IPs", "3-4 static", "~$12"),
        ("Load Balancers", "Network LB", "~$20"),
        ("Other", "DNS, GCS, KMS, disks", "~$6"),
    ]
    for r in rows:
        table.add_row(*r)

    table.add_row("[bold]Total[/bold]", "", f"[bold]{total}[/bold]", style="bold")
    console.print(table)
