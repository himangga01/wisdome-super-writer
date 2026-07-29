from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from email.message import Message
from functools import lru_cache, wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

import yaml
from django.http.request import RawPostDataException
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver
from yaml.tokens import AliasToken, AnchorToken, TagToken

from wisdome_writer.domain.errors import (
    MalformedJson,
    MethodNotAllowed,
    RequestValidationError,
    UnsupportedMediaType,
    ValidationIssue,
)

_CONTRACT_RELATIVE_PATH = Path(
    "specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml"
)
_HTTP_METHODS = frozenset(
    {"delete", "get", "head", "options", "patch", "post", "put", "trace"}
)
_JSON_MEDIA_TYPES = frozenset({"application/json", "application/merge-patch+json"})
_INTEGER_PATTERN = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")
_NUMBER_PATTERN = re.compile(
    r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z"
)
_OPERATION_ID_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,199}\Z")
_FORMAT_CHECKER = FormatChecker()
_SUPPORTED_FORMATS = frozenset(
    {
        "date",
        "date-time",
        "uri",
        "uri-reference",
        "uuid",
    }
)
_SINGLE_SUBSCHEMA_KEYWORDS = frozenset(
    {
        "additionalItems",
        "additionalProperties",
        "contains",
        "contentSchema",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)
_ARRAY_SUBSCHEMA_KEYWORDS = frozenset(
    {
        "allOf",
        "anyOf",
        "oneOf",
        "prefixItems",
    }
)
_MAPPING_SUBSCHEMA_KEYWORDS = frozenset(
    {
        "$defs",
        "definitions",
        "dependentSchemas",
        "patternProperties",
        "properties",
    }
)
_MAX_VALIDATION_ISSUES = 50
_RFC8785_INTEGER_MAX = (2**53) - 1
_RFC8785_INTEGER_MIN = -_RFC8785_INTEGER_MAX

P = ParamSpec("P")
R = TypeVar("R")


class OpenAPIContractError(RuntimeError):
    """The deployed OpenAPI contract is absent, unsafe, or unsupported."""


class _DuplicateJsonMember(ValueError):
    pass


class _NonFiniteJsonNumber(ValueError):
    pass


class _OutOfRangeJsonInteger(ValueError):
    pass


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, Hashable):
            raise ConstructorError(
                "while constructing an OpenAPI mapping",
                node.start_mark,
                "mapping keys must be hashable",
                key_node.start_mark,
            )
        if key in mapping:
            raise ConstructorError(
                "while constructing an OpenAPI mapping",
                node.start_mark,
                "duplicate mapping keys are forbidden",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True, slots=True)
class _Parameter:
    name: str
    location: str
    required: bool
    schema: Mapping[str, Any]
    style: str
    explode: bool


@dataclass(frozen=True, slots=True)
class _CompiledOperation:
    operation_id: str
    method: str
    path_template: str
    query_parameters: tuple[_Parameter, ...]
    path_parameters: tuple[_Parameter, ...]
    query_validator: Draft202012Validator
    path_validator: Draft202012Validator
    body_required: bool
    body_validators: Mapping[str, Draft202012Validator]
    allow_unknown_query_parameters: bool


def _contract_path() -> Path:
    path = Path(__file__).resolve().parents[3] / _CONTRACT_RELATIVE_PATH
    if not path.is_file():
        raise OpenAPIContractError(
            f"OpenAPI contract is missing at repository-relative path "
            f"{_CONTRACT_RELATIVE_PATH.as_posix()}"
        )
    return path


@lru_cache(maxsize=1)
def load_openapi_contract() -> Mapping[str, Any]:
    """Load the one repository-owned contract with PyYAML's safe loader."""

    try:
        text = _contract_path().read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise OpenAPIContractError("OpenAPI contract could not be read as UTF-8") from exc

    try:
        if any(
            isinstance(token, (AliasToken, AnchorToken, TagToken))
            for token in yaml.scan(text)
        ):
            raise OpenAPIContractError(
                "OpenAPI contract cannot contain YAML aliases, anchors, or tags"
            )
        document = yaml.load(text, Loader=_UniqueKeySafeLoader)
    except OpenAPIContractError:
        raise
    except yaml.YAMLError as exc:
        raise OpenAPIContractError("OpenAPI contract is not safe valid YAML") from exc

    if not isinstance(document, Mapping):
        raise OpenAPIContractError("OpenAPI contract root must be an object")
    version = document.get("openapi")
    if not isinstance(version, str) or re.fullmatch(
        r"3\.1\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?",
        version,
    ) is None:
        raise OpenAPIContractError("Only OpenAPI 3.1 contracts are supported")

    _require_local_references(document, document=document)
    return document


def _require_local_references(node: Any, *, document: Mapping[str, Any]) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key in {"$dynamicRef", "$recursiveRef"}:
                raise OpenAPIContractError(f"{key} is not supported")
            if key == "$ref":
                if not isinstance(value, str) or not value.startswith("#/"):
                    raise OpenAPIContractError("Only local JSON Pointer $ref values are supported")
                _resolve_pointer(document, value)
            else:
                _require_local_references(value, document=document)
        return
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
        for value in node:
            _require_local_references(value, document=document)


def _resolve_pointer(document: Mapping[str, Any], reference: str) -> Any:
    if not reference.startswith("#/"):
        raise OpenAPIContractError("Only local JSON Pointer $ref values are supported")

    current: Any = document
    for encoded_token in reference[2:].split("/"):
        if re.search(r"~(?:[^01]|$)", encoded_token):
            raise OpenAPIContractError("OpenAPI $ref contains an invalid JSON Pointer escape")
        token = encoded_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise OpenAPIContractError(f"OpenAPI $ref does not exist: {reference}")
            current = current[token]
            continue
        if isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            if re.fullmatch(r"(?:0|[1-9][0-9]*)", token) is None:
                raise OpenAPIContractError(
                    f"OpenAPI $ref does not exist: {reference}"
                )
            try:
                index = int(token)
                current = current[index]
            except (ValueError, IndexError) as exc:
                raise OpenAPIContractError(
                    f"OpenAPI $ref does not exist: {reference}"
                ) from exc
            continue
        raise OpenAPIContractError(f"OpenAPI $ref does not exist: {reference}")
    return current


def _resolve_reference_object(
    value: Any,
    *,
    document: Mapping[str, Any],
    references: tuple[str, ...] = (),
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OpenAPIContractError("OpenAPI reference target must be an object")
    reference = value.get("$ref")
    if reference is None:
        return value
    if not isinstance(reference, str) or not reference.startswith("#/"):
        raise OpenAPIContractError("Only local JSON Pointer $ref values are supported")
    if reference in references:
        raise OpenAPIContractError(f"Cyclic OpenAPI reference is unsupported: {reference}")

    target = _resolve_reference_object(
        _resolve_pointer(document, reference),
        document=document,
        references=(*references, reference),
    )
    merged = dict(target)
    merged.update({key: item for key, item in value.items() if key != "$ref"})
    return merged


def _resolve_schema(
    value: Any,
    *,
    document: Mapping[str, Any],
    references: tuple[str, ...] = (),
) -> Any:
    if isinstance(value, Mapping):
        reference = value.get("$ref")
        if reference is not None:
            if not isinstance(reference, str) or not reference.startswith("#/"):
                raise OpenAPIContractError(
                    "Only local JSON Pointer $ref values are supported"
                )
            if reference in references:
                raise OpenAPIContractError(
                    f"Cyclic request-schema reference is unsupported: {reference}"
                )
            target = _resolve_schema(
                _resolve_pointer(document, reference),
                document=document,
                references=(*references, reference),
            )
            siblings = {
                key: _resolve_schema(item, document=document, references=references)
                for key, item in value.items()
                if key != "$ref"
            }
            if not siblings:
                return target
            return {"allOf": [target], **siblings}
        return {
            key: _resolve_schema(item, document=document, references=references)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _resolve_schema(item, document=document, references=references)
            for item in value
        ]
    return value


def _require_supported_formats(schema: Mapping[str, Any], *, subject: str) -> None:
    schema_format = schema.get("format")
    if schema_format is not None and (
        not isinstance(schema_format, str)
        or schema_format not in _SUPPORTED_FORMATS
        or schema_format not in _FORMAT_CHECKER.checkers
    ):
        raise OpenAPIContractError(
            f"{subject} uses an unsupported JSON Schema format"
        )

    for keyword in _SINGLE_SUBSCHEMA_KEYWORDS:
        subschema = schema.get(keyword)
        if isinstance(subschema, Mapping):
            _require_supported_formats(subschema, subject=subject)
    for keyword in _ARRAY_SUBSCHEMA_KEYWORDS:
        subschemas = schema.get(keyword)
        if isinstance(subschemas, Sequence) and not isinstance(
            subschemas,
            (str, bytes, bytearray),
        ):
            for subschema in subschemas:
                if isinstance(subschema, Mapping):
                    _require_supported_formats(subschema, subject=subject)
    for keyword in _MAPPING_SUBSCHEMA_KEYWORDS:
        subschemas = schema.get(keyword)
        if isinstance(subschemas, Mapping):
            for subschema in subschemas.values():
                if isinstance(subschema, Mapping):
                    _require_supported_formats(subschema, subject=subject)


def _compile_schema(
    schema: Any,
    *,
    document: Mapping[str, Any],
    subject: str,
) -> tuple[Mapping[str, Any], Draft202012Validator]:
    resolved = _resolve_schema(schema, document=document)
    if not isinstance(resolved, Mapping):
        raise OpenAPIContractError(f"{subject} schema must be an object")
    _require_supported_formats(resolved, subject=subject)
    try:
        Draft202012Validator.check_schema(resolved)
    except SchemaError as exc:
        raise OpenAPIContractError(f"{subject} is not valid JSON Schema 2020-12") from exc
    return resolved, Draft202012Validator(resolved, format_checker=_FORMAT_CHECKER)


def _parameter_schema(
    parameter: Mapping[str, Any],
    *,
    document: Mapping[str, Any],
    operation_id: str,
) -> _Parameter:
    name = parameter.get("name")
    location = parameter.get("in")
    schema = parameter.get("schema")
    if not isinstance(name, str) or not name:
        raise OpenAPIContractError(f"{operation_id} contains a parameter without a name")
    if location not in {"path", "query"}:
        raise OpenAPIContractError(
            f"{operation_id} contains an unsupported request parameter location"
        )
    resolved, _validator = _compile_schema(
        schema,
        document=document,
        subject=f"{operation_id} {location} parameter {name}",
    )

    schema_type = _schema_type(resolved)
    if schema_type == "object":
        raise OpenAPIContractError(
            f"{operation_id} parameter {name} uses unsupported object serialization"
        )
    if schema_type == "array" and not isinstance(resolved.get("items"), Mapping):
        raise OpenAPIContractError(
            f"{operation_id} parameter {name} must declare an array item schema"
        )

    default_style = "simple" if location == "path" else "form"
    style = parameter.get("style", default_style)
    if style != default_style:
        raise OpenAPIContractError(
            f"{operation_id} parameter {name} uses unsupported style {style!r}"
        )
    explode_default = location == "query"
    explode = parameter.get("explode", explode_default)
    if not isinstance(explode, bool):
        raise OpenAPIContractError(
            f"{operation_id} parameter {name} has an invalid explode value"
        )

    required = parameter.get("required", False)
    if not isinstance(required, bool):
        raise OpenAPIContractError(
            f"{operation_id} parameter {name} has an invalid required value"
        )
    if location == "path" and required is not True:
        raise OpenAPIContractError(
            f"{operation_id} path parameter {name} must be required"
        )
    return _Parameter(
        name=name,
        location=location,
        required=required,
        schema=resolved,
        style=style,
        explode=explode,
    )


def _parameters_schema(parameters: Iterable[_Parameter]) -> Mapping[str, Any]:
    parameter_list = tuple(parameters)
    return {
        "type": "object",
        "properties": {
            parameter.name: parameter.schema for parameter in parameter_list
        },
        "required": [
            parameter.name for parameter in parameter_list if parameter.required
        ],
        "additionalProperties": False,
    }


def _merged_parameters(
    path_item: Mapping[str, Any],
    operation: Mapping[str, Any],
    *,
    document: Mapping[str, Any],
    operation_id: str,
) -> tuple[_Parameter, ...]:
    merged: dict[tuple[str, str], _Parameter] = {}
    for owner in (path_item, operation):
        raw_parameters = owner.get("parameters", ())
        if not isinstance(raw_parameters, Sequence) or isinstance(
            raw_parameters, (str, bytes, bytearray)
        ):
            raise OpenAPIContractError(
                f"{operation_id} parameters must be declared as an array"
            )
        owner_keys: set[tuple[str, str]] = set()
        for raw_parameter in raw_parameters:
            resolved = _resolve_reference_object(raw_parameter, document=document)
            location = resolved.get("in")
            if location not in {"path", "query"}:
                raise OpenAPIContractError(
                    f"{operation_id} contains an unsupported request parameter location"
                )
            parameter = _parameter_schema(
                resolved,
                document=document,
                operation_id=operation_id,
            )
            key = (parameter.location, parameter.name)
            if key in owner_keys:
                raise OpenAPIContractError(
                    f"{operation_id} declares parameter {parameter.name!r} more than once"
                )
            owner_keys.add(key)
            merged[key] = parameter
    return tuple(merged.values())


def _request_body(
    operation: Mapping[str, Any],
    *,
    document: Mapping[str, Any],
    operation_id: str,
) -> tuple[bool, Mapping[str, Draft202012Validator]]:
    raw_body = operation.get("requestBody")
    if raw_body is None:
        return False, {}
    body = _resolve_reference_object(raw_body, document=document)
    required = body.get("required", False)
    if not isinstance(required, bool):
        raise OpenAPIContractError(f"{operation_id} requestBody.required must be boolean")
    content = body.get("content")
    if not isinstance(content, Mapping) or not content:
        raise OpenAPIContractError(f"{operation_id} request body has no content schema")

    validators: dict[str, Draft202012Validator] = {}
    for raw_media_type, media in content.items():
        if not isinstance(raw_media_type, str):
            raise OpenAPIContractError(
                f"{operation_id} request body has an invalid media type"
            )
        media_type = raw_media_type.lower()
        if media_type not in _JSON_MEDIA_TYPES:
            raise OpenAPIContractError(
                f"{operation_id} request body media type is unsupported: {raw_media_type}"
            )
        if media_type in validators:
            raise OpenAPIContractError(
                f"{operation_id} request body declares a duplicate media type"
            )
        if not isinstance(media, Mapping) or "schema" not in media:
            raise OpenAPIContractError(
                f"{operation_id} {raw_media_type} request body has no schema"
            )
        _resolved, validator = _compile_schema(
            media["schema"],
            document=document,
            subject=f"{operation_id} {raw_media_type} request body",
        )
        validators[media_type] = validator
    return required, validators


@lru_cache(maxsize=1)
def _operation_index() -> Mapping[str, _CompiledOperation]:
    document = load_openapi_contract()
    paths = document.get("paths")
    if not isinstance(paths, Mapping):
        raise OpenAPIContractError("OpenAPI contract paths must be an object")

    operations: dict[str, _CompiledOperation] = {}
    for path_template, raw_path_item in paths.items():
        if (
            not isinstance(path_template, str)
            or not path_template.startswith("/")
            or len(path_template) > 2048
            or "?" in path_template
            or "#" in path_template
            or any(ord(character) < 32 for character in path_template)
        ):
            raise OpenAPIContractError("OpenAPI paths must be absolute path templates")
        path_item = _resolve_reference_object(raw_path_item, document=document)
        for method, raw_operation in path_item.items():
            normalized_method = str(method).lower()
            if normalized_method not in _HTTP_METHODS:
                continue
            if not isinstance(raw_operation, Mapping):
                raise OpenAPIContractError(
                    f"{normalized_method.upper()} {path_template} must be an object"
                )
            operation_id = raw_operation.get("operationId")
            if (
                not isinstance(operation_id, str)
                or _OPERATION_ID_PATTERN.fullmatch(operation_id) is None
            ):
                raise OpenAPIContractError(
                    f"{normalized_method.upper()} {path_template} has an invalid operationId"
                )
            if operation_id in operations:
                raise OpenAPIContractError(
                    f"Duplicate OpenAPI operationId: {operation_id}"
                )

            parameters = _merged_parameters(
                path_item,
                raw_operation,
                document=document,
                operation_id=operation_id,
            )
            query_parameters = tuple(
                item for item in parameters if item.location == "query"
            )
            path_parameters = tuple(
                item for item in parameters if item.location == "path"
            )
            template_parameters = re.findall(r"{([^{}]+)}", path_template)
            declared_path_parameters = [item.name for item in path_parameters]
            if (
                len(template_parameters) != len(set(template_parameters))
                or set(template_parameters) != set(declared_path_parameters)
            ):
                raise OpenAPIContractError(
                    f"{operation_id} path template and parameters do not match"
                )
            path_parameter_by_name = {
                item.name: item for item in path_parameters
            }
            path_parameters = tuple(
                path_parameter_by_name[name] for name in template_parameters
            )
            query_schema = _parameters_schema(query_parameters)
            path_schema = _parameters_schema(path_parameters)
            _query_resolved, query_validator = _compile_schema(
                query_schema,
                document=document,
                subject=f"{operation_id} query parameters",
            )
            _path_resolved, path_validator = _compile_schema(
                path_schema,
                document=document,
                subject=f"{operation_id} path parameters",
            )
            body_required, body_validators = _request_body(
                raw_operation,
                document=document,
                operation_id=operation_id,
            )
            allow_unknown_query_parameters = raw_operation.get(
                "x-wisdome-allow-unknown-query-parameters",
                False,
            )
            if not isinstance(allow_unknown_query_parameters, bool):
                raise OpenAPIContractError(
                    f"{operation_id} has an invalid unknown-query policy"
                )
            operations[operation_id] = _CompiledOperation(
                operation_id=operation_id,
                method=normalized_method.upper(),
                path_template=path_template,
                query_parameters=query_parameters,
                path_parameters=path_parameters,
                query_validator=query_validator,
                path_validator=path_validator,
                body_required=body_required,
                body_validators=body_validators,
                allow_unknown_query_parameters=allow_unknown_query_parameters,
            )
    if not operations:
        raise OpenAPIContractError("OpenAPI contract does not define any operations")
    return operations


def _schema_type(schema: Mapping[str, Any]) -> str | None:
    schema_type = schema.get("type")
    if isinstance(schema_type, str):
        return schema_type
    if isinstance(schema_type, Sequence) and not isinstance(
        schema_type, (str, bytes, bytearray)
    ):
        non_null = [item for item in schema_type if item != "null"]
        if len(non_null) == 1 and isinstance(non_null[0], str):
            return non_null[0]
    all_of = schema.get("allOf")
    if isinstance(all_of, Sequence) and not isinstance(
        all_of, (str, bytes, bytearray)
    ):
        for item in all_of:
            if isinstance(item, Mapping):
                nested_type = _schema_type(item)
                if nested_type:
                    return nested_type
    return None


def _coerce_scalar(value: Any, schema: Mapping[str, Any]) -> Any:
    schema_type = _schema_type(schema)
    if schema_type in {None, "string"}:
        if isinstance(value, str):
            return value
        return str(value)
    if schema_type == "boolean":
        if type(value) is bool:
            return value
        if value == "true":
            return True
        if value == "false":
            return False
        raise ValueError
    if schema_type == "integer":
        if type(value) is int:
            result = value
        elif not isinstance(value, str) or _INTEGER_PATTERN.fullmatch(value) is None:
            raise ValueError
        else:
            result = int(value)
        if not _RFC8785_INTEGER_MIN <= result <= _RFC8785_INTEGER_MAX:
            raise ValueError
        return result
    if schema_type == "number":
        if type(value) in {int, float} and not isinstance(value, bool):
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError
            if isinstance(value, int) and not (
                _RFC8785_INTEGER_MIN <= value <= _RFC8785_INTEGER_MAX
            ):
                raise ValueError
            return value
        if not isinstance(value, str) or _NUMBER_PATTERN.fullmatch(value) is None:
            raise ValueError
        if _INTEGER_PATTERN.fullmatch(value):
            result = int(value)
            if not _RFC8785_INTEGER_MIN <= result <= _RFC8785_INTEGER_MAX:
                raise ValueError
            return result
        number = float(value)
        if not math.isfinite(number):
            raise ValueError
        return number
    raise ValueError


def _coerce_parameter_values(
    values: Sequence[Any],
    parameter: _Parameter,
) -> Any:
    schema_type = _schema_type(parameter.schema)
    if schema_type != "array":
        if len(values) != 1:
            raise ValueError("duplicate")
        return _coerce_scalar(values[0], parameter.schema)

    item_schema = parameter.schema["items"]
    raw_items: Sequence[Any] = values
    if not parameter.explode and len(values) == 1 and isinstance(values[0], str):
        raw_items = values[0].split(",")
    return [_coerce_scalar(item, item_schema) for item in raw_items]


def _pointer(base: str, parts: Iterable[Any]) -> str:
    encoded = [base]
    for part in parts:
        token = str(part).replace("~", "~0").replace("/", "~1")
        encoded.append(token)
    return "/" + "/".join(encoded)


def _python_parameter_name(value: str) -> str:
    first_pass = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", first_pass).lower()


def _issue(path: str, code: str) -> ValidationIssue:
    return ValidationIssue(path=path, code=code)


def _issues_from_errors(
    errors: Iterable[ValidationError],
    *,
    base: str,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    code_by_validator = {
        "additionalProperties": "additional_property",
        "anyOf": "invalid_choice",
        "const": "invalid_value",
        "contains": "invalid_items",
        "dependentRequired": "required",
        "enum": "invalid_choice",
        "exclusiveMaximum": "out_of_range",
        "exclusiveMinimum": "out_of_range",
        "format": "invalid_format",
        "maxItems": "too_many_items",
        "maxLength": "too_long",
        "maxProperties": "too_many_properties",
        "maximum": "out_of_range",
        "minItems": "too_few_items",
        "minLength": "too_short",
        "minProperties": "too_few_properties",
        "minimum": "out_of_range",
        "multipleOf": "invalid_number",
        "not": "invalid_value",
        "oneOf": "invalid_choice",
        "pattern": "invalid_format",
        "prefixItems": "invalid_items",
        "propertyNames": "invalid_property",
        "type": "invalid_type",
        "unevaluatedItems": "unexpected_item",
        "unevaluatedProperties": "additional_property",
        "uniqueItems": "duplicate_item",
    }
    for error in errors:
        path_parts = tuple(error.absolute_path)
        if error.validator == "required" and isinstance(error.instance, Mapping):
            required = error.validator_value
            if isinstance(required, Sequence) and not isinstance(
                required, (str, bytes, bytearray)
            ):
                for field_name in required:
                    if isinstance(field_name, str) and field_name not in error.instance:
                        issues.append(
                            _issue(_pointer(base, (*path_parts, field_name)), "required")
                        )
                continue
        code = code_by_validator.get(str(error.validator), "invalid")
        issues.append(_issue(_pointer(base, path_parts), code))

    unique = {(item.path, item.code): item for item in issues}
    ordered = sorted(unique.values(), key=lambda item: (item.path, item.code))
    return tuple(ordered[:_MAX_VALIDATION_ISSUES])


def _raise_request_issues(issues: Iterable[ValidationIssue]) -> None:
    unique = {(item.path, item.code): item for item in issues}
    ordered = tuple(
        sorted(unique.values(), key=lambda item: (item.path, item.code))[
            :_MAX_VALIDATION_ISSUES
        ]
    )
    if ordered:
        raise RequestValidationError(errors=ordered)


def _validated_query(request: Any, operation: _CompiledOperation) -> dict[str, Any]:
    parameters = {item.name: item for item in operation.query_parameters}
    issues: list[ValidationIssue] = []
    result: dict[str, Any] = {}
    query = request.GET

    if (
        not operation.allow_unknown_query_parameters
        and any(name not in parameters for name in query.keys())
    ):
        issues.append(_issue("/query", "additional_property"))
    for name, parameter in parameters.items():
        if name not in query:
            continue
        values = query.getlist(name)
        try:
            result[name] = _coerce_parameter_values(values, parameter)
        except ValueError as exc:
            code = "duplicate" if str(exc) == "duplicate" else "invalid_type"
            issues.append(_issue(_pointer("query", (name,)), code))
    _raise_request_issues(issues)
    _raise_request_issues(
        _issues_from_errors(
            operation.query_validator.iter_errors(result),
            base="query",
        )
    )
    return result


def _validated_path(
    request: Any,
    operation: _CompiledOperation,
    view_kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    resolver_match = getattr(request, "resolver_match", None)
    resolver_kwargs = getattr(resolver_match, "kwargs", {}) if resolver_match else {}
    raw_values = dict(resolver_kwargs)
    raw_values.update(view_kwargs)

    issues: list[ValidationIssue] = []
    result: dict[str, Any] = {}
    consumed_names: set[str] = set()
    for parameter in operation.path_parameters:
        candidates = (parameter.name, _python_parameter_name(parameter.name))
        matched_name = next(
            (
                candidate
                for candidate in candidates
                if candidate in raw_values and candidate not in consumed_names
            ),
            None,
        )
        if matched_name is None:
            continue
        consumed_names.add(matched_name)
        try:
            result[parameter.name] = _coerce_parameter_values(
                (raw_values[matched_name],),
                parameter,
            )
        except ValueError:
            issues.append(_issue(_pointer("path", (parameter.name,)), "invalid_type"))

    if any(name not in consumed_names for name in raw_values):
        issues.append(_issue("/path", "additional_property"))
    _raise_request_issues(issues)
    _raise_request_issues(
        _issues_from_errors(
            operation.path_validator.iter_errors(result),
            base="path",
        )
    )
    return result


def _json_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonMember
        result[key] = value
    return result


def _json_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise _NonFiniteJsonNumber
    return result


def _json_integer(value: str) -> int:
    if len(value.removeprefix("-")) > 16:
        raise _OutOfRangeJsonInteger
    try:
        result = int(value)
    except ValueError as exc:
        raise _OutOfRangeJsonInteger from exc
    if not _RFC8785_INTEGER_MIN <= result <= _RFC8785_INTEGER_MAX:
        raise _OutOfRangeJsonInteger
    return result


def _reject_json_constant(_value: str) -> None:
    raise _NonFiniteJsonNumber


def _parse_content_type(request: Any) -> str:
    raw_content_type = request.META.get("CONTENT_TYPE", "")
    if not isinstance(raw_content_type, str) or not raw_content_type.strip():
        raise UnsupportedMediaType

    message = Message()
    message["content-type"] = raw_content_type
    media_type = message.get_content_type().lower()
    parameters = message.get_params(failobj=[])[1:]
    if len(parameters) > 1:
        raise UnsupportedMediaType
    if parameters:
        name, value = parameters[0]
        if str(name).lower() != "charset" or str(value).lower() != "utf-8":
            raise UnsupportedMediaType
    return media_type


def _validated_body(request: Any, operation: _CompiledOperation) -> dict[str, Any]:
    validator: Draft202012Validator | None = None
    if (
        operation.body_validators
        and str(request.META.get("CONTENT_TYPE", "")).strip()
    ):
        media_type = _parse_content_type(request)
        validator = operation.body_validators.get(media_type)
        if validator is None:
            raise UnsupportedMediaType

    try:
        raw_body = request.body
    except RawPostDataException as exc:
        raise UnsupportedMediaType from exc
    if not operation.body_validators:
        if raw_body:
            raise RequestValidationError(
                errors=(_issue("/body", "unexpected"),),
            )
        return {}

    if not raw_body:
        if operation.body_required:
            raise RequestValidationError(
                errors=(_issue("/body", "required"),),
            )
        return {}

    if validator is None:
        media_type = _parse_content_type(request)
        validator = operation.body_validators.get(media_type)
        if validator is None:
            raise UnsupportedMediaType

    try:
        text = raw_body.decode("utf-8", errors="strict")
        body = json.loads(
            text,
            object_pairs_hook=_json_object_pairs,
            parse_constant=_reject_json_constant,
            parse_float=_json_float,
            parse_int=_json_integer,
        )
    except _OutOfRangeJsonInteger as exc:
        raise RequestValidationError(
            errors=(_issue("/body", "out_of_range"),),
        ) from exc
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonMember,
        _NonFiniteJsonNumber,
        RecursionError,
    ) as exc:
        raise MalformedJson from exc

    if not isinstance(body, dict):
        raise RequestValidationError(
            errors=(_issue("/body", "invalid_type"),),
        )
    _raise_request_issues(
        _issues_from_errors(
            validator.iter_errors(body),
            base="body",
        )
    )
    return body


def _validate_and_attach(
    request: Any,
    operation: _CompiledOperation,
    view_kwargs: Mapping[str, Any],
) -> None:
    request_method = str(request.method).upper()
    if request_method != operation.method:
        raise MethodNotAllowed(allowed_methods=(operation.method,))

    path = _validated_path(request, operation, view_kwargs)
    query = _validated_query(request, operation)
    body = _validated_body(request, operation)
    identity_payload = {
        "path": path,
        "query": query,
        "body": body,
    }

    # Deliberately imported only after validation. The OpenAPI layer has no fallback
    # identity algorithm; all callers depend on the shared domain implementation.
    from wisdome_writer.domain.concurrency import canonical_request_hash

    try:
        request_identity = canonical_request_hash(
            operation_id=operation.operation_id,
            path=operation.path_template,
            payload=identity_payload,
        )
    except (OverflowError, TypeError, UnicodeError, ValueError) as exc:
        raise RequestValidationError(
            errors=(_issue("/body", "invalid_value"),),
        ) from exc
    request.openapi_body = body
    request.openapi_query = query
    request.openapi_path = path
    request.openapi_operation_id = operation.operation_id
    request.openapi_request_identity = request_identity


def openapi_operation(operation_id: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Validate a Django view request against one OpenAPI operation."""

    if not isinstance(operation_id, str) or not operation_id:
        raise OpenAPIContractError("OpenAPI operation ID must be a non-empty string")
    operation = _operation_index().get(operation_id)
    if operation is None:
        raise OpenAPIContractError(f"OpenAPI operationId does not exist: {operation_id}")
    return openapi_operations({operation.method: operation_id})


def openapi_operations(
    method_operations: Mapping[str, str],
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Validate a Django view that dispatches multiple explicitly bound methods."""

    if not isinstance(method_operations, Mapping) or not method_operations:
        raise OpenAPIContractError("At least one method-to-operation binding is required")

    bindings: dict[str, _CompiledOperation] = {}
    for raw_method, operation_id in method_operations.items():
        method = str(raw_method).upper()
        if method.lower() not in _HTTP_METHODS:
            raise OpenAPIContractError(f"Unsupported HTTP method binding: {method}")
        if method in bindings:
            raise OpenAPIContractError(f"Duplicate OpenAPI method binding: {method}")
        if not isinstance(operation_id, str) or not operation_id:
            raise OpenAPIContractError(
                f"OpenAPI operation ID for {method} must be a non-empty string"
            )
        operation = _operation_index().get(operation_id)
        if operation is None:
            raise OpenAPIContractError(
                f"OpenAPI operationId does not exist: {operation_id}"
            )
        if operation.method != method:
            raise OpenAPIContractError(
                f"{operation_id} is {operation.method}, not bound method {method}"
            )
        bindings[method] = operation
    allowed_methods = tuple(sorted(bindings))

    def decorator(view: Callable[P, R]) -> Callable[P, R]:
        @wraps(view)
        def wrapped(request: Any, *args: P.args, **kwargs: P.kwargs) -> R:
            operation = bindings.get(str(request.method).upper())
            if operation is None:
                raise MethodNotAllowed(allowed_methods=allowed_methods)
            _validate_and_attach(request, operation, kwargs)
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


# Build and compile every request schema at module import. A missing or invalid
# deployment contract therefore fails closed before an endpoint can serve traffic.
_operation_index()


__all__ = (
    "OpenAPIContractError",
    "load_openapi_contract",
    "openapi_operation",
    "openapi_operations",
)
