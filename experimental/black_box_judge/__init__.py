"""Black-box judge (experimental, default off): a forge-proof verdict for the serializable-I/O
interface. Not wired into the pipeline; see judge.py and docs/PRESERVED_BEHAVIORS.md."""

from experimental.black_box_judge.judge import (
    BlackBoxCase,
    BlackBoxError,
    BlackBoxVerdict,
    run_black_box_judge,
)

__all__ = ["BlackBoxCase", "BlackBoxError", "BlackBoxVerdict", "run_black_box_judge"]
