"""PR-Q: group_vars must live next to the inventory.

GAP-43 discovered in dogfood 10 ansible_run: the common role failed at
'node_exporter_version is undefined' even though
ansible/group_vars/all.yml declared it. Ansible auto-loads group_vars
ONLY from one of:
  - the directory adjacent to the playbook (ansible/playbooks/group_vars/)
  - the directory adjacent to the inventory file/dir
    (ansible/inventory/group_vars/)
Plain ansible/group_vars/ is NOT consulted when ansible-playbook is
invoked with --inventory inventory/gcp_inventory.py.

PR-Q moves the directory so Ansible's auto-discovery picks it up.

These tests pin the layout so a future refactor cannot regress.
"""

from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
ANSIBLE_DIR = REPO / "ansible"


class TestGroupVarsLayout:
    def test_group_vars_lives_next_to_inventory(self):
        target = ANSIBLE_DIR / "inventory" / "group_vars"
        assert target.is_dir(), (
            f"group_vars must live at {target} so Ansible's auto-discovery "
            "picks it up alongside the dynamic inventory. See PR-Q / GAP-43."
        )

    def test_group_vars_does_not_live_at_ansible_root(self):
        stale = ANSIBLE_DIR / "group_vars"
        assert not stale.exists(), (
            f"{stale} is the layout that broke dogfood 10 — Ansible does not "
            "load group_vars from this path when the inventory is in a "
            "subdirectory."
        )

    def test_all_yml_present(self):
        assert (ANSIBLE_DIR / "inventory" / "group_vars" / "all.yml").is_file()

    def test_kamailio_yml_present(self):
        assert (ANSIBLE_DIR / "inventory" / "group_vars" / "kamailio.yml").is_file()

    def test_rtpengine_yml_present(self):
        assert (ANSIBLE_DIR / "inventory" / "group_vars" / "rtpengine.yml").is_file()

    def test_node_exporter_version_declared(self):
        """The specific variable whose absence caused GAP-43 must be present
        in the relocated all.yml so the regression cannot recur."""
        import yaml
        data = yaml.safe_load(
            (ANSIBLE_DIR / "inventory" / "group_vars" / "all.yml").read_text()
        )
        assert isinstance(data, dict)
        assert "node_exporter_version" in data, (
            "all.yml must still declare node_exporter_version after the "
            "move; otherwise the common role's node_exporter download task "
            "regresses to GAP-43."
        )
