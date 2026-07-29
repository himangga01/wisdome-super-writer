from __future__ import annotations

from .models import OperationalControl


GLOBAL_KILL_SWITCH_KEY = "global_kill_switch"


def is_external_write_blocked(*, using: str = "default") -> bool:
    """Fail closed until an explicit disabled control row permits writes."""

    enabled = (
        OperationalControl.objects.using(using)
        .filter(key=GLOBAL_KILL_SWITCH_KEY)
        .values_list("enabled", flat=True)
        .first()
    )
    return enabled is None or enabled is True
