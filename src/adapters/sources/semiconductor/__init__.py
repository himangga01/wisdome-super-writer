from .adapters import (
    KrxKindAdapter,
    MotirAdapter,
    SamsungNewsroomAdapter,
    SiaLatestAdapter,
    SkHynixNewsroomAdapter,
)

ADAPTERS = {
    "semiconductor_motir": MotirAdapter,
    "semiconductor_krx_kind": KrxKindAdapter,
    "semiconductor_samsung_newsroom": SamsungNewsroomAdapter,
    "semiconductor_skhynix_newsroom": SkHynixNewsroomAdapter,
    "semiconductor_sia_latest": SiaLatestAdapter,
}

__all__ = [
    "ADAPTERS",
    "KrxKindAdapter",
    "MotirAdapter",
    "SamsungNewsroomAdapter",
    "SiaLatestAdapter",
    "SkHynixNewsroomAdapter",
]
