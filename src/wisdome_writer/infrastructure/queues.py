CELERY_QUEUE_NAMES = (
    "outbox.dispatch",
    "source.check",
    "source.change",
    "collect.housing",
    "collect.semiconductor",
    "extract.fanout",
    "extract.document",
    "extract.generic",
    "extract.ocr.paddle",
    "editorial",
    "publish.media.wordpress",
    "publish.wordpress",
    "publish.blogger",
    "reconcile",
    "maintenance",
)

STATIC_EVENT_QUEUE_NAMES = frozenset(CELERY_QUEUE_NAMES)
