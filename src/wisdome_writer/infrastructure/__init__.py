from .secrets import SecretRef, SecretResolver


def enqueue_event(*args, **kwargs):
    """Import the database-backed outbox lazily after Django's app registry is ready."""
    from .outbox import enqueue_event as _enqueue_event

    return _enqueue_event(*args, **kwargs)

__all__ = ("SecretRef", "SecretResolver", "enqueue_event")
