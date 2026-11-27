"""Read-only Tk window entrypoint for BTC Paper Lab V3 Research Center."""
from __future__ import annotations


def open_research_center(parent):
    """Delegate directly to Quant Strategy Research Center V3."""
    from scripts.research_v3_window import open_research_center_v3
    return open_research_center_v3(parent)
