from adapters.sources.http import PublicHtmlAdapter


class ApplyHomeAdapter(PublicHtmlAdapter):
    """Approved public ApplyHome notice-list adapter.

    Detail endpoint changes are intentionally handled by registry snapshot configuration,
    not by browser-session automation.
    """


class LhApplyAdapter(PublicHtmlAdapter):
    """Approved public LH notice-list adapter."""


__all__ = ["ApplyHomeAdapter", "LhApplyAdapter"]
