"""Deployment pipeline orchestrator with checkpoint/resume for VoIPBin installer."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from scripts.ansible_runner import ansible_check, ansible_run
from scripts.config import InstallerConfig
from scripts.diagnosis import diagnose_stage_failure
from scripts.display import (
    console,
    print_error,
    print_fix,
    print_header,
    print_step,
    print_success,
    print_warning,
)
from scripts.k8s import k8s_apply, k8s_delete, k8s_dry_run
from scripts.terraform import (
    terraform_apply,
    terraform_destroy,
    terraform_init,
    terraform_output,
    terraform_plan,
)
from scripts.terraform_reconcile import imports as _terraform_imports, outputs as _terraform_outputs
from scripts.utils import INSTALLER_DIR


STATE_FILE = INSTALLER_DIR / ".voipbin-state.yaml"

# Ordered stages for apply (PR-R: k8s_apply + reconcile_k8s_outputs now run
# BEFORE ansible_run so kamailio's `.env` can be rendered with k8s LB IPs
# allocated by k8s Services of type=LoadBalancer.)
APPLY_STAGES = (
    "terraform_init",
    "reconcile_imports",
    "terraform_apply",
    "reconcile_outputs",
    "k8s_apply",
    "reconcile_k8s_outputs",
    "ansible_run",
)
# Ordered stages for destroy (reverse)
DESTROY_STAGES = ("k8s_delete", "ansible_cleanup", "terraform_destroy")


# Deprecation message shown when --stage terraform_reconcile is used.
DEPRECATION_MESSAGE_RECONCILE = (
    "⚠  --stage terraform_reconcile is deprecated.\n"
    "   The reconcile stage was split into two:\n"
    "     • reconcile_imports  (BEFORE terraform_apply — imports drifted GCP resources)\n"
    "     • reconcile_outputs  (AFTER  terraform_apply — auto-populates config.yaml)\n"
    "   Running both for backward compatibility. This shim is scheduled for\n"
    "   removal in install-redesign PR-J. Update scripts to use the new names."
)


# ---------------------------------------------------------------------------
# Checkpoint persistence
# ---------------------------------------------------------------------------

def _migrate_legacy_reconcile_state(state: dict[str, Any]) -> dict[str, Any]:
    """Migrate legacy `terraform_reconcile` state key to the split stages.

    PR-A split the single `terraform_reconcile` stage into `reconcile_imports`
    (before apply) and `reconcile_outputs` (after apply). Existing state files
    may contain the legacy key — expand it per the migration table:

        complete → reconcile_imports: complete, reconcile_outputs: pending
        failed   → reconcile_imports: failed,   reconcile_outputs: pending
        running  → reconcile_imports: failed,   reconcile_outputs: pending
        pending  → reconcile_imports: pending,  reconcile_outputs: pending

    The legacy key is deleted after expansion. If both legacy and new keys are
    present (operator hand-edit), the new keys take precedence and legacy is
    dropped. Idempotent — a second call sees no legacy key and is a no-op.
    Unknown stage keys are preserved as-is.
    """
    stages = state.get("stages")
    if not isinstance(stages, dict):
        return state
    if "terraform_reconcile" not in stages:
        return state
    legacy = stages.pop("terraform_reconcile")
    mapping = {
        "complete": ("complete", "pending"),
        "failed":   ("failed",   "pending"),
        "running":  ("failed",   "pending"),
        "pending":  ("pending",  "pending"),
    }
    imports_state, outputs_state = mapping.get(legacy, ("pending", "pending"))
    # New keys take precedence if already present.
    stages.setdefault("reconcile_imports", imports_state)
    stages.setdefault("reconcile_outputs", outputs_state)
    state["stages"] = stages
    return state


def _migrate_pr_r_apply_stages(state: dict[str, Any]) -> dict[str, Any]:
    """Reset ansible_run to pending if reconcile_k8s_outputs absent from state.

    PR-R reordered ansible_run to AFTER k8s_apply + reconcile_k8s_outputs.
    Operators with pre-PR-R state.yaml have ansible_run=complete; without
    this shim, the next apply skips ansible_run entirely and the new k8s
    LB IPs never reach Kamailio. Idempotent: once reconcile_k8s_outputs
    is in state.stages, this shim is a no-op.
    """
    stages = state.get("stages")
    if not isinstance(stages, dict):
        return state  # fresh state, nothing to migrate
    if "reconcile_k8s_outputs" in stages:
        return state  # already migrated
    if stages.get("ansible_run") == "complete":
        stages["ansible_run"] = "pending"
        print_warning(
            "PR-R migration: detected pre-PR-R state.yaml with ansible_run "
            "marked complete. Resetting to pending so it re-runs after the "
            "new k8s_apply + reconcile_k8s_outputs stages."
        )
    return state


def load_state() -> dict[str, Any]:
    """Load deployment state from the checkpoint file."""
    if not STATE_FILE.exists():
        return {}
    with open(STATE_FILE) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        return {}
    data = _migrate_legacy_reconcile_state(data)
    data = _migrate_pr_r_apply_stages(data)
    return data


def save_state(state: dict[str, Any]) -> None:
    """Write deployment state to the checkpoint file."""
    state["timestamp"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        yaml.safe_dump(state, f, default_flow_style=False, sort_keys=False)


def clear_state() -> None:
    """Remove the checkpoint file."""
    if STATE_FILE.exists():
        STATE_FILE.unlink()


def _initial_stages_state() -> dict[str, str]:
    """Return a fresh stages dict with all stages pending."""
    return {stage: "pending" for stage in APPLY_STAGES}


# ---------------------------------------------------------------------------
# Stage runners
# ---------------------------------------------------------------------------

def _run_terraform_init(
    config: InstallerConfig,
    _outputs: dict[str, Any],
    dry_run: bool,
    auto_approve: bool,
) -> bool:
    # terraform init is safe to run even in dry-run — it only configures
    # the backend and downloads providers; no infrastructure is created.
    return terraform_init(config)


def _run_reconcile_imports(
    config: InstallerConfig,
    _outputs: dict[str, Any],
    dry_run: bool,
    auto_approve: bool,
) -> bool:
    if dry_run:
        print_step("[dim]Dry run: skipping Terraform reconcile (imports)[/dim]")
        return True
    return _terraform_imports(config, auto_approve=auto_approve)


def _run_reconcile_outputs(
    config: InstallerConfig,
    tf_outputs: dict[str, Any],
    dry_run: bool,
    auto_approve: bool,
) -> bool:
    if dry_run:
        print_step("[dim]Dry run: skipping Terraform reconcile (outputs)[/dim]")
        return True
    return _terraform_outputs(config, tf_outputs)


def _run_terraform_apply(
    config: InstallerConfig,
    _outputs: dict[str, Any],
    dry_run: bool,
    auto_approve: bool,
) -> bool:
    # PR-D2a destroy-safety gate: prevent accidental loss of the legacy
    # `voipbin` MySQL database (PR-D1 leftover). Skipped on dry_run.
    if not dry_run:
        from scripts.preflight import (
            PreflightError,
            check_legacy_voipbin_destroy_safety,
        )
        try:
            check_legacy_voipbin_destroy_safety(
                config,
                force=getattr(config, "force_destroy_legacy_voipbin", False),
            )
        except PreflightError as exc:
            print_error(str(exc))
            return False
    if dry_run:
        return terraform_plan(config)
    return terraform_apply(config, auto_approve=auto_approve)


def _run_ansible(
    config: InstallerConfig,
    outputs: dict[str, Any],
    dry_run: bool,
    auto_approve: bool,
) -> bool:
    # OS Login preflight: VMs use OS Login for SSH and require a registered
    # SSH key on the operator's profile. Run this check on the live path
    # only (skip during dry_run since dry_run never opens an SSH connection).
    if not dry_run:
        from scripts.preflight import check_oslogin_setup
        err = check_oslogin_setup()
        if err is not None:
            print_error(err)
            return False
    if dry_run:
        # ansible --check requires SSH to existing VMs; skip gracefully
        # when no Terraform outputs are available (infrastructure not yet created)
        if not outputs.get("kamailio_internal_ips"):
            print_step("[dim]Dry run: skipping Ansible (no infrastructure deployed yet)[/dim]")
            print_step("[dim]  ansible-playbook --check requires VMs to be reachable via IAP[/dim]")
            return True
        return ansible_check(config, outputs)
    return ansible_run(config, outputs)


def _run_k8s_apply(
    config: InstallerConfig,
    outputs: dict[str, Any],
    dry_run: bool,
    auto_approve: bool,
) -> bool:
    # PR-E: cloudsql_private_ip preflight runs HERE (after reconcile_outputs
    # has had a chance to populate the field from Terraform output), and
    # BEFORE manifests are rendered so a clean error surfaces instead of a
    # cryptic kubectl validation failure later.
    from scripts.preflight import PreflightError, check_cloudsql_private_ip
    try:
        check_cloudsql_private_ip(config)
    except PreflightError as exc:
        print_error(str(exc))
        return False
    if dry_run:
        return k8s_dry_run(config, outputs)
    return k8s_apply(config, outputs)


def _run_reconcile_k8s_outputs(
    config: InstallerConfig,
    outputs: dict[str, Any],
    dry_run: bool,
    auto_approve: bool,  # unused; uniform signature with other runners
) -> bool:
    """Harvest k8s LoadBalancer externalIPs and merge into outputs.

    PR-R: this stage runs AFTER k8s_apply and BEFORE ansible_run. The
    harvested IPs are merged into the in-memory outputs dict so the very
    next stage (ansible_run via _write_extra_vars) sees them.

    Persistence to state.yaml.k8s_outputs is performed by the caller
    (run_pipeline) after this stage returns, not here, so that the main
    loop's state["stages"] save does not clobber an out-of-band write
    from this function (PR-T2 fix). The merged outputs dict is the
    source of truth in-process; the caller copies the LB IP subset from
    it into state and writes a single consistent save_state call.
    """
    from scripts.k8s import harvest_loadbalancer_ips
    if dry_run:
        print_step("[dim](dry-run) Skipping k8s LB IP harvest[/dim]")
        return True
    lb_ips = harvest_loadbalancer_ips()
    outputs.update(lb_ips)
    # Stash the freshly-harvested subset on the outputs dict under a
    # private sentinel key so the caller knows which keys are LB IPs
    # (vs. terraform output keys also merged into the same dict). The
    # caller pops this before downstream stages see outputs.
    outputs["__pr_t2_harvested_lb_ips__"] = dict(lb_ips)
    return True


# Map stage names to runner functions
STAGE_RUNNERS: dict[
    str,
    Callable[[InstallerConfig, dict[str, Any], bool, bool], bool],
] = {
    "terraform_init": _run_terraform_init,
    "reconcile_imports": _run_reconcile_imports,
    "terraform_apply": _run_terraform_apply,
    "reconcile_outputs": _run_reconcile_outputs,
    "k8s_apply": _run_k8s_apply,
    "reconcile_k8s_outputs": _run_reconcile_k8s_outputs,
    "ansible_run": _run_ansible,
}

STAGE_LABELS: dict[str, str] = {
    "terraform_init": "Terraform Init",
    "reconcile_imports": "Terraform Reconcile (Imports)",
    "terraform_apply": "Terraform Apply",
    "reconcile_outputs": "Terraform Reconcile (Outputs)",
    "k8s_apply": "Kubernetes Apply",
    "reconcile_k8s_outputs": "Reconcile K8s LB IPs",
    "ansible_run": "Ansible Playbook",
}


# ---------------------------------------------------------------------------
# Pipeline execution
# ---------------------------------------------------------------------------

def run_pipeline(
    config: InstallerConfig,
    dry_run: bool = False,
    auto_approve: bool = False,
    only_stage: Optional[str] = None,
) -> bool:
    """Execute the deployment pipeline with checkpoint/resume.

    Returns True if all requested stages succeeded.
    """
    state = load_state()
    stages = dict(state.get("stages", _initial_stages_state()))

    # Determine which stages to run
    if only_stage == "terraform_reconcile":
        print_warning(DEPRECATION_MESSAGE_RECONCILE)
        requested = ["reconcile_imports", "reconcile_outputs"]
    elif only_stage:
        if only_stage not in STAGE_RUNNERS:
            print_error(f"Unknown stage: {only_stage}")
            return False
        requested = [only_stage]
    else:
        requested = list(APPLY_STAGES)

    # Filter to stages not yet complete (for resume) unless explicitly requested
    to_run = []
    for stage_name in requested:
        if stages.get(stage_name) == "complete" and not only_stage:
            print_step(f"[dim]Skipping {STAGE_LABELS.get(stage_name, stage_name)} (already complete)[/dim]")
            continue
        to_run.append(stage_name)

    # Precondition: reconcile_outputs requires a completed terraform_apply,
    # since it reads `terraform output`. Reject standalone runs that would
    # otherwise fail with an opaque empty-outputs error.
    if (
        only_stage == "reconcile_outputs"
        and stages.get("terraform_apply") != "complete"
    ):
        print_error(
            "reconcile_outputs requires terraform_apply to be complete first."
        )
        return False

    if not to_run:
        print_success("All stages already complete. Nothing to do.")
        return True

    # Save initial state
    state["deployment_state"] = "applying"
    state["stages"] = stages
    save_state(state)

    # Collect Terraform outputs after terraform_apply (needed by later stages)
    tf_outputs: dict[str, Any] = {}
    if stages.get("terraform_apply") == "complete" and "terraform_apply" not in to_run:
        tf_outputs = terraform_output(config)

    # PR-R: rehydrate persisted k8s LB IPs from prior reconcile_k8s_outputs
    # runs so subsequent --stage ansible_run invocations see them even when
    # k8s_apply itself isn't being re-run in this CLI call.
    persisted_k8s = state.get("k8s_outputs") or {}
    if isinstance(persisted_k8s, dict):
        for k, v in persisted_k8s.items():
            if isinstance(k, str) and isinstance(v, str):
                tf_outputs.setdefault(k, v)

    for stage_name in to_run:
        label = STAGE_LABELS.get(stage_name, stage_name)
        print_header(f"Stage: {label}")

        stages[stage_name] = "running"
        state["last_stage"] = stage_name
        state["stages"] = stages
        save_state(state)

        runner = STAGE_RUNNERS[stage_name]
        ok = runner(config, tf_outputs, dry_run, auto_approve)

        if not ok:
            stages[stage_name] = "failed"
            state["deployment_state"] = "failed"
            state["stages"] = stages
            save_state(state)
            print_error(f"Stage '{label}' failed. Pipeline halted.")

            hints = diagnose_stage_failure(config, stage_name)
            if hints:
                print_fix("Likely causes", hints)

            print_step("Resume with: [bold]voipbin-install apply[/bold]")
            return False

        stages[stage_name] = "complete"
        state["stages"] = stages

        # PR-T2: reconcile_k8s_outputs stashes its harvest result on the
        # outputs dict under a sentinel key. Merge it into state["k8s_outputs"]
        # HERE (in the same state object the main loop saves) so the
        # subsequent save_state writes a single consistent snapshot. Doing
        # the persist inside the stage function created two writers racing
        # on the same file and the main loop's save clobbered the stage's
        # k8s_outputs write.
        if stage_name == "reconcile_k8s_outputs":
            harvested = tf_outputs.pop("__pr_t2_harvested_lb_ips__", None)
            if isinstance(harvested, dict) and harvested:
                existing = state.get("k8s_outputs") or {}
                if not isinstance(existing, dict):
                    existing = {}
                # MERGE not replace so a partial re-harvest does not delete
                # previously good keys (e.g. GCP flake on one Service in a
                # rerun).
                existing.update(harvested)
                state["k8s_outputs"] = existing

        save_state(state)

        # Refresh Terraform outputs after apply stage
        if stage_name == "terraform_apply" and not dry_run:
            tf_outputs = terraform_output(config)

    state["deployment_state"] = "deployed" if not dry_run else "planned"
    save_state(state)
    return True


def destroy_pipeline(
    config: InstallerConfig,
    auto_approve: bool = False,
) -> bool:
    """Tear down resources in reverse order.

    Returns True if all stages succeeded.
    """
    state = load_state()
    state["deployment_state"] = "destroying"
    save_state(state)

    # Stage 1: K8s delete
    print_header("Stage: Kubernetes Delete")
    k8s_ok = k8s_delete(config)
    if not k8s_ok:
        print_warning("Kubernetes delete had issues; continuing with Terraform destroy")

    # Stage 2: Terraform destroy (covers VMs, Cloud SQL, GKE, networking)
    print_header("Stage: Terraform Destroy")
    tf_ok = terraform_destroy(config, auto_approve=auto_approve)
    if not tf_ok:
        state["deployment_state"] = "destroy_failed"
        save_state(state)
        print_error("Terraform destroy failed. Some resources may remain.")
        return False

    state["deployment_state"] = "destroyed"
    state["stages"] = {stage: "pending" for stage in APPLY_STAGES}
    # PR-R: clear persisted k8s LB IPs so a subsequent apply re-harvests
    # against the rebuilt cluster rather than rehydrating ghosts.
    state["k8s_outputs"] = {}
    save_state(state)
    clear_state()
    return True
