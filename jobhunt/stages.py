"""Application stages: keys, labels and display order."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Stage:
    key: str
    label: str
    open: bool  # still in progress (shown first on the Applications page)


# Display order on the Applications page: open stages first, most advanced first.
STAGES: tuple[Stage, ...] = (
    Stage("offer", "Offer", open=True),
    Stage("interviewing", "Interviewing", open=True),
    Stage("oa_pending", "OA to do", open=True),
    Stage("oa_done", "OA done", open=True),
    Stage("applied", "Applied", open=True),
    Stage("on_hold", "On hold", open=True),
    Stage("to_apply", "To apply", open=True),
    Stage("rejected", "Rejected", open=False),
    Stage("withdrawn", "Withdrawn", open=False),
)

BY_KEY: dict[str, Stage] = {s.key: s for s in STAGES}
ORDER: dict[str, int] = {s.key: i for i, s in enumerate(STAGES)}

# Stages that mean an application has been sent. Reaching one fills "Applied on" if it is empty.
SENT = frozenset({"applied", "oa_pending", "oa_done", "interviewing", "on_hold", "offer", "rejected", "withdrawn"})

# Stages where a long silence probably means the application was ghosted.
WAITING = frozenset({"applied", "oa_done"})


def is_valid(key: str) -> bool:
    return key in BY_KEY
