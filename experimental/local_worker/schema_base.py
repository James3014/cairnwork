"""Shared pieces of the episode line schema (#88): the error, the type table convention and the
object check (exact keys, types with a nullable flag, finite and non-negative numbers)."""

from __future__ import annotations

import math
from typing import Any

STR, INT, FLOAT, BOOL, DICT, LIST = str, int, (int, float), bool, dict, list
PATH = (str, dict)  # a base path, or {sha256, bytes, ext} (redact.public_path)
SIGNED = ("exit_code", "seed")  # a signal exit is negative; a seed is any integer


class SchemaError(ValueError):
    """A line that does not match the frozen schema is never appended."""


def check(obj: Any, schema: dict[str, tuple[Any, bool]], where: str) -> None:
    """Exact key set; each value of its type or, where nullable, null; no bool for a number;
    every number finite, and non-negative unless its key is in SIGNED."""
    if not isinstance(obj, dict):
        raise SchemaError(f"{where} is not an object")
    if set(obj) != set(schema):
        extra, missing = sorted(set(obj) - set(schema)), sorted(set(schema) - set(obj))
        raise SchemaError(f"{where}: unexpected keys {extra}, missing keys {missing}")
    for key, (typ, nullable) in schema.items():
        value = obj[key]
        if value is None:
            if not nullable:
                raise SchemaError(f"{where}.{key} must not be null")
            continue
        bad_bool = isinstance(value, bool) and typ is not BOOL
        if bad_bool or not isinstance(value, typ):
            raise SchemaError(f"{where}.{key} has type {type(value).__name__}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value) or (value < 0 and key not in SIGNED):
                raise SchemaError(f"{where}.{key} must be finite and non-negative")
