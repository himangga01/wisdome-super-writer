from __future__ import annotations

import hashlib
import platform
import sys
from importlib import metadata
from pathlib import Path
from typing import Any, Mapping

from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
)


ADAPTER_IMPLEMENTATION_MANIFEST_SCHEMA_V1 = (
    "source-adapter-implementation-manifest-v1"
)

_ADAPTER_EXECUTIONS: dict[str, tuple[str, str]] = {
    "semiconductor_motir": (
        "v3",
        "adapters.sources.semiconductor.adapters.MotirAdapter",
    ),
    "semiconductor_krx_kind": (
        "v3",
        "adapters.sources.semiconductor.adapters.KrxKindAdapter",
    ),
    "semiconductor_samsung_newsroom": (
        "v3",
        "adapters.sources.semiconductor.adapters.SamsungNewsroomAdapter",
    ),
    "semiconductor_skhynix_newsroom": (
        "v3",
        "adapters.sources.semiconductor.adapters.SkHynixNewsroomAdapter",
    ),
    "semiconductor_sia_latest": (
        "v3",
        "adapters.sources.semiconductor.adapters.SiaLatestAdapter",
    ),
    "housing_applyhome": (
        "v3",
        "adapters.sources.housing.applyhome.ApplyHomeAdapter",
    ),
    "housing_lh": (
        "v3",
        "adapters.sources.housing.lh.LhApplyAdapter",
    ),
    "open_data_json": (
        "v3",
        "adapters.sources.http.OpenDataJsonAdapter",
    ),
    "public_html": (
        "v3",
        "adapters.sources.http.PublicHtmlAdapter",
    ),
    "rss": (
        "v3",
        "adapters.sources.http.RssAdapter",
    ),
}
_SHARED_IMPLEMENTATION_FILES = (
    "adapters/sources/manifests.py",
    "wisdome_writer/domain/hashing.py",
)
_T012_EXECUTION_BOUNDARY_FILES = (
    "apps/collection/services.py",
    "apps/topics/tasks.py",
    "wisdome_writer/infrastructure/event_routes.py",
    "apps/evidence/tasks.py",
    "wisdome_writer/infrastructure/http_safety.py",
)
_HTTP_IMPLEMENTATION_FILES = (
    *_SHARED_IMPLEMENTATION_FILES,
    "adapters/sources/base.py",
    "adapters/sources/errors.py",
    "adapters/sources/http.py",
    "wisdome_writer/infrastructure/secrets.py",
    *_T012_EXECUTION_BOUNDARY_FILES,
)
_ADAPTER_IMPLEMENTATION_FILES = {
    **{
        key: (
            *_HTTP_IMPLEMENTATION_FILES,
            "adapters/sources/__init__.py",
            "adapters/sources/semiconductor/__init__.py",
            "adapters/sources/semiconductor/common.py",
            "adapters/sources/semiconductor/adapters.py",
        )
        for key in (
            "semiconductor_motir",
            "semiconductor_krx_kind",
            "semiconductor_samsung_newsroom",
            "semiconductor_skhynix_newsroom",
            "semiconductor_sia_latest",
        )
    },
    "housing_applyhome": (
        *_HTTP_IMPLEMENTATION_FILES,
        "adapters/sources/housing/__init__.py",
        "adapters/sources/housing/common.py",
        "adapters/sources/housing/applyhome.py",
    ),
    "housing_lh": (
        *_HTTP_IMPLEMENTATION_FILES,
        "adapters/sources/housing/__init__.py",
        "adapters/sources/housing/common.py",
        "adapters/sources/housing/lh.py",
    ),
    "open_data_json": _HTTP_IMPLEMENTATION_FILES,
    "public_html": _HTTP_IMPLEMENTATION_FILES,
    "rss": _HTTP_IMPLEMENTATION_FILES,
}
_RUNTIME_DEPENDENCIES = (
    "defusedxml",
    "django",
    "httpx",
    "lxml",
    "rfc8785",
)


def adapter_execution_manifest(
    adapter_key: str,
    *,
    access_method: str,
) -> dict[str, Any]:
    try:
        version, implementation = _ADAPTER_EXECUTIONS[adapter_key]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported source adapter: {adapter_key}"
        ) from exc

    # Housing sources using public-page access execute the same currently
    # frozen public HTML implementation.
    if (
        adapter_key in {"housing_applyhome", "housing_lh"}
        and access_method == "public_html"
    ):
        version = "v3"
        implementation = "adapters.sources.http.PublicHtmlAdapter"
        implementation_files = _ADAPTER_IMPLEMENTATION_FILES[
            "public_html"
        ]
    else:
        implementation_files = _ADAPTER_IMPLEMENTATION_FILES[
            adapter_key
        ]
    source_root = Path(__file__).resolve().parents[2]
    return {
        "schemaVersion": ADAPTER_IMPLEMENTATION_MANIFEST_SCHEMA_V1,
        "adapterKey": adapter_key,
        "adapterVersion": version,
        "implementation": implementation,
        "runtime": {
            "implementation": sys.implementation.name,
            "pythonVersion": platform.python_version(),
            "dependencies": [
                {
                    "name": package_name,
                    "version": metadata.version(package_name),
                }
                for package_name in _RUNTIME_DEPENDENCIES
            ],
        },
        "implementationFiles": [
            {
                "path": relative_path,
                "sha256": hashlib.sha256(
                    (source_root / relative_path).read_bytes()
                ).hexdigest(),
            }
            for relative_path in implementation_files
        ],
    }


def adapter_execution_manifest_hash(
    manifest: Mapping[str, Any],
) -> str:
    return canonical_hash(
        dict(manifest),
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )
