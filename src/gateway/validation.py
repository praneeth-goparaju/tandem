"""Small fail-closed validator for the schema features used by Brain tools."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

_SUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "additionalProperties",
        "anyOf",
        "description",
        "enum",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "pattern",
        "properties",
        "required",
        "type",
        "uniqueItems",
    }
)
_KNOWN_TYPES = frozenset({"array", "boolean", "integer", "null", "number", "object", "string"})


class SchemaValidationError(ValueError):
    """A tool argument does not satisfy its published input schema."""


def validate_arguments(arguments: object, schema: Mapping[str, Any]) -> None:
    _reject_nonfinite(arguments, path="$", depth=0)
    validate_schema(schema)
    _validate(arguments, schema, path="$", depth=0)


def validate_schema(schema: Mapping[str, Any]) -> None:
    """Validate the complete schema subset before relying on it for a call."""
    _validate_schema(schema, path="$schema", depth=0)


def _validate_schema(schema: object, *, path: str, depth: int) -> None:
    if depth > 32:
        raise SchemaValidationError("schema nesting exceeds the supported limit")
    if not isinstance(schema, Mapping):
        raise SchemaValidationError(f"{path} must be an object")

    unknown = sorted(
        str(key) for key in schema if not isinstance(key, str) or key not in _SUPPORTED_SCHEMA_KEYS
    )
    if unknown:
        raise SchemaValidationError(f"{path} uses unsupported keyword: {', '.join(unknown)}")

    declared_type = schema.get("type")
    if declared_type is not None:
        declared_types = declared_type if isinstance(declared_type, list) else [declared_type]
        if (
            not declared_types
            or any(not isinstance(item, str) or item not in _KNOWN_TYPES for item in declared_types)
            or len(set(declared_types)) != len(declared_types)
        ):
            raise SchemaValidationError(f"{path}.type is invalid")

    description = schema.get("description")
    if description is not None and not isinstance(description, str):
        raise SchemaValidationError(f"{path}.description must be a string")

    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, Mapping):
            raise SchemaValidationError(f"{path}.properties must be an object")
        for name, child in properties.items():
            if not isinstance(name, str):
                raise SchemaValidationError(f"{path}.properties has a non-string name")
            _validate_schema(child, path=f"{path}.properties.{name}", depth=depth + 1)

    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list)
        or any(not isinstance(item, str) for item in required)
        or len(set(required)) != len(required)
    ):
        raise SchemaValidationError(f"{path}.required is invalid")

    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool):
        _validate_schema(
            additional,
            path=f"{path}.additionalProperties",
            depth=depth + 1,
        )

    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list):
            raise SchemaValidationError(f"{path}.enum must be an array")
        _reject_nonfinite(enum, path=f"{path}.enum", depth=depth + 1)

    any_of = schema.get("anyOf")
    if any_of is not None:
        if not isinstance(any_of, list) or not any_of:
            raise SchemaValidationError(f"{path}.anyOf must be a non-empty array")
        for index, child in enumerate(any_of):
            _validate_schema(child, path=f"{path}.anyOf[{index}]", depth=depth + 1)

    items = schema.get("items")
    if items is not None:
        _validate_schema(items, path=f"{path}.items", depth=depth + 1)

    for minimum_name, maximum_name in (
        ("minLength", "maxLength"),
        ("minItems", "maxItems"),
    ):
        minimum = schema.get(minimum_name)
        maximum = schema.get(maximum_name)
        for name, value in ((minimum_name, minimum), (maximum_name, maximum)):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise SchemaValidationError(f"{path}.{name} must be a non-negative integer")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise SchemaValidationError(f"{path}.{minimum_name} exceeds {maximum_name}")

    for name in ("minimum", "maximum"):
        value = schema.get(name)
        if value is not None and (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise SchemaValidationError(f"{path}.{name} must be a finite number")
    minimum_number = schema.get("minimum")
    maximum_number = schema.get("maximum")
    if (
        minimum_number is not None
        and maximum_number is not None
        and minimum_number > maximum_number
    ):
        raise SchemaValidationError(f"{path}.minimum exceeds maximum")

    pattern = schema.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            raise SchemaValidationError(f"{path}.pattern must be a string")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise SchemaValidationError(f"{path}.pattern is invalid") from exc

    unique_items = schema.get("uniqueItems")
    if unique_items is not None and not isinstance(unique_items, bool):
        raise SchemaValidationError(f"{path}.uniqueItems must be a boolean")


def _reject_nonfinite(instance: object, *, path: str, depth: int) -> None:
    if depth > 64:
        raise SchemaValidationError("argument nesting exceeds the supported limit")
    if isinstance(instance, float) and not math.isfinite(instance):
        raise SchemaValidationError(f"{path} must not contain NaN or infinity")
    if isinstance(instance, Mapping):
        for name, value in instance.items():
            _reject_nonfinite(value, path=f"{path}.{name}", depth=depth + 1)
    elif isinstance(instance, list):
        for index, value in enumerate(instance):
            _reject_nonfinite(value, path=f"{path}[{index}]", depth=depth + 1)


def _validate(instance: object, schema: Mapping[str, Any], *, path: str, depth: int) -> None:
    if depth > 32:
        raise SchemaValidationError("schema nesting exceeds the supported limit")

    variants = schema.get("anyOf")
    if variants is not None:
        if not isinstance(variants, list) or not variants:
            raise SchemaValidationError("published schema has an invalid anyOf")
        if not any(_matches(instance, variant, path=path, depth=depth + 1) for variant in variants):
            raise SchemaValidationError(f"{path} does not match any allowed shape")

    expected = schema.get("type")
    if expected is not None and not _has_type(instance, expected):
        raise SchemaValidationError(f"{path} must be {_type_label(expected)}")

    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list):
            raise SchemaValidationError("published schema has an invalid enum")
        if instance not in enum:
            raise SchemaValidationError(f"{path} must be one of the published enum values")

    if isinstance(instance, Mapping):
        _validate_object(instance, schema, path=path, depth=depth)
    elif isinstance(instance, list):
        _validate_array(instance, schema, path=path, depth=depth)
    elif isinstance(instance, str):
        _validate_string(instance, schema, path=path)
    elif isinstance(instance, (int, float)) and not isinstance(instance, bool):
        _validate_number(instance, schema, path=path)


def _matches(instance: object, schema: object, *, path: str, depth: int) -> bool:
    if not isinstance(schema, Mapping):
        return False
    try:
        _validate(instance, schema, path=path, depth=depth)
    except SchemaValidationError:
        return False
    return True


def _validate_object(
    instance: Mapping[object, object],
    schema: Mapping[str, Any],
    *,
    path: str,
    depth: int,
) -> None:
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise SchemaValidationError("published schema has an invalid required list")
    for name in required:
        if name not in instance:
            raise SchemaValidationError(f"{path}.{name} is required")

    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise SchemaValidationError("published schema has invalid properties")
    for name, value in instance.items():
        child_schema = properties.get(name)
        if child_schema is None:
            additional = schema.get("additionalProperties", True)
            if additional is False:
                raise SchemaValidationError(f"{path}.{name} is not an allowed property")
            if additional is True:
                continue
            if not isinstance(additional, Mapping):
                raise SchemaValidationError("published schema has invalid additionalProperties")
            _validate(value, additional, path=f"{path}.{name}", depth=depth + 1)
            continue
        if not isinstance(name, str) or not isinstance(child_schema, Mapping):
            raise SchemaValidationError("published schema has an invalid property")
        _validate(value, child_schema, path=f"{path}.{name}", depth=depth + 1)


def _validate_array(
    instance: list[object],
    schema: Mapping[str, Any],
    *,
    path: str,
    depth: int,
) -> None:
    minimum = schema.get("minItems")
    maximum = schema.get("maxItems")
    if isinstance(minimum, int) and len(instance) < minimum:
        raise SchemaValidationError(f"{path} has fewer than the published minItems")
    if isinstance(maximum, int) and len(instance) > maximum:
        raise SchemaValidationError(f"{path} has more than the published maxItems")
    if schema.get("uniqueItems") is True:
        canonical = [json.dumps(value, sort_keys=True, separators=(",", ":")) for value in instance]
        if len(canonical) != len(set(canonical)):
            raise SchemaValidationError(f"{path} must contain unique items")
    items = schema.get("items")
    if items is None:
        return
    if not isinstance(items, Mapping):
        raise SchemaValidationError("published schema has invalid array items")
    for index, value in enumerate(instance):
        _validate(value, items, path=f"{path}[{index}]", depth=depth + 1)


def _validate_string(instance: str, schema: Mapping[str, Any], *, path: str) -> None:
    minimum = schema.get("minLength")
    maximum = schema.get("maxLength")
    if isinstance(minimum, int) and len(instance) < minimum:
        raise SchemaValidationError(f"{path} is shorter than the published minLength")
    if isinstance(maximum, int) and len(instance) > maximum:
        raise SchemaValidationError(f"{path} is longer than the published maxLength")
    pattern = schema.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            raise SchemaValidationError("published schema has an invalid pattern")
        try:
            matched = re.search(pattern, instance)
        except re.error as exc:
            raise SchemaValidationError("published schema has an invalid pattern") from exc
        if matched is None:
            raise SchemaValidationError(f"{path} does not match the published pattern")


def _validate_number(instance: int | float, schema: Mapping[str, Any], *, path: str) -> None:
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if isinstance(minimum, (int, float)) and instance < minimum:
        raise SchemaValidationError(f"{path} is below the published minimum")
    if isinstance(maximum, (int, float)) and instance > maximum:
        raise SchemaValidationError(f"{path} is above the published maximum")


def _has_type(instance: object, expected: object) -> bool:
    if isinstance(expected, list):
        return any(_has_type(instance, item) for item in expected)
    checks = {
        "object": lambda value: isinstance(value, Mapping),
        "array": lambda value: isinstance(value, list),
        "string": lambda value: isinstance(value, str),
        "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": lambda value: isinstance(value, bool),
        "null": lambda value: value is None,
    }
    return isinstance(expected, str) and expected in checks and checks[expected](instance)


def _type_label(expected: object) -> str:
    if isinstance(expected, list) and all(isinstance(item, str) for item in expected):
        return " or ".join(expected)
    if isinstance(expected, str) and re.fullmatch(r"[a-z]+", expected):
        return expected
    return "the type declared by the published schema"
