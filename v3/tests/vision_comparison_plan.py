"""Canonical blinded plan; disputed historical fixtures are excluded."""

from pathlib import Path

from tests.vision_adjudicated_plan import build_adjudicated_blind_plan
from tests.vision_eval_runner import REPO_ROOT


def build_blind_comparison_plan(root: Path = REPO_ROOT):
    """Build the current frozen plan, not the superseded disputed case04 plan."""
    return build_adjudicated_blind_plan(root)
