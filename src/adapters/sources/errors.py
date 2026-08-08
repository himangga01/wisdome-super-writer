from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
import re


SourceAccessCategory = Literal[
    "policy",
    "schema",
    "authentication",
    "transient",
    "security",
    "infrastructure",
]
_URL_PATTERN = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)


def _redact_detail(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        try:
            parsed = urlsplit(match.group(0))
            if not parsed.scheme or not parsed.hostname:
                return "<redacted-url>"
            return urlunsplit((parsed.scheme, parsed.netloc.split("@")[-1], parsed.path or "/", "", ""))
        except ValueError:
            return "<redacted-url>"

    return _URL_PATTERN.sub(replace, str(value))


@dataclass(eq=False)
class SourceAccessError(Exception):
    """A durable, persistence-safe source access failure contract."""

    code: str
    category: SourceAccessCategory
    detail: str
    remediation: str
    retryable: bool = False
    retry_after_seconds: int | None = None
    http_status: int | None = None

    @property
    def permanent(self) -> bool:
        return not self.retryable

    def __post_init__(self) -> None:
        self.detail = _redact_detail(self.detail)
        if self.category not in {
            "policy",
            "schema",
            "authentication",
            "transient",
            "security",
            "infrastructure",
        }:
            raise ValueError("Source access category is invalid.")
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise ValueError("Source retry delay must not be negative.")
        if self.http_status is not None and (
            isinstance(self.http_status, bool)
            or not isinstance(self.http_status, int)
            or not 100 <= self.http_status <= 599
        ):
            raise ValueError("Source HTTP status must be between 100 and 599.")
        Exception.__init__(self, self.detail)
