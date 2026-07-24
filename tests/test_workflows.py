"""Repository workflow contracts."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

# These are the first action majors in use here that declare the Node 24
# runtime. Keep this as a lower bound so routine dependency upgrades do not
# require changing the test, while accidental downgrades to Node 20 fail CI.
NODE24_MINIMUM_MAJORS = {
    "actions/checkout": 5,
    "actions/setup-python": 6,
    "gitleaks/gitleaks-action": 3,
    "docker/setup-buildx-action": 4,
    "docker/build-push-action": 7,
}


def test_workflow_actions_do_not_regress_to_node20_runtimes():
    workflow_text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(WORKFLOWS.glob("*.yml"))
    )
    used_actions = re.findall(r"uses:\s*([^@\s]+)@v(\d+)", workflow_text)

    versions_by_action: dict[str, set[int]] = {}
    for action, version in used_actions:
        versions_by_action.setdefault(action, set()).add(int(version))

    assert NODE24_MINIMUM_MAJORS.keys() <= versions_by_action.keys()
    for action, minimum in NODE24_MINIMUM_MAJORS.items():
        assert min(versions_by_action[action]) >= minimum, (
            f"{action} regressed below its Node 24-compatible major v{minimum}"
        )
