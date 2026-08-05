"""Serialization helpers shared by all domain models.

Domain models are ``@dataclass`` objects that must round-trip cleanly to/from
the JSON files stored in each project folder. The helpers here provide a
consistent, dependency-free way to:

* convert dataclasses (including nested dataclasses, enums, datetimes and
  lists/dicts thereof) to JSON-safe primitives, and
* reconstruct dataclasses from those primitives, coercing enum and datetime
  fields back to their rich types.

Keeping this logic in one place means every model gets identical, well-tested
serialization behaviour without repeating boilerplate.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
from typing import Any, TypeVar, get_args, get_origin, get_type_hints

T = TypeVar("T")

_ISO = "%Y-%m-%dT%H:%M:%S.%f"


def to_primitive(value: Any) -> Any:
    """Recursively convert *value* into JSON-serializable primitives."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_primitive(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): to_primitive(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_primitive(v) for v in value]
    # Fallback: stringify unknown types so serialization never crashes.
    return str(value)


def _coerce(value: Any, target_type: Any) -> Any:
    """Best-effort coercion of a primitive *value* into *target_type*."""
    if value is None:
        return None

    origin = get_origin(target_type)

    # Optional[...] / Union[...] — try the first non-None arg.
    if origin is not None and origin.__name__ == "UnionType" or str(origin) == "typing.Union":
        for arg in get_args(target_type):
            if arg is type(None):
                continue
            try:
                return _coerce(value, arg)
            except Exception:  # noqa: BLE001 - fall through to next union member
                continue
        return value

    # Containers
    if origin in (list, tuple, set):
        (item_type,) = get_args(target_type) or (Any,)
        return [_coerce(v, item_type) for v in value]
    if origin is dict:
        args = get_args(target_type)
        val_type = args[1] if len(args) == 2 else Any
        return {k: _coerce(v, val_type) for k, v in value.items()}

    # Enums
    if isinstance(target_type, type) and issubclass(target_type, enum.Enum):
        try:
            return target_type(value)
        except ValueError:
            # Support our StrEnum.from_value case-insensitive lookup.
            from_value = getattr(target_type, "from_value", None)
            if callable(from_value):
                return from_value(value)
            raise

    # datetime
    if target_type is _dt.datetime and isinstance(value, str):
        return _dt.datetime.fromisoformat(value)

    # Nested dataclass
    if dataclasses.is_dataclass(target_type) and isinstance(value, dict):
        return from_dict(target_type, value)

    return value


def from_dict(cls: type[T], data: dict[str, Any]) -> T:
    """Reconstruct dataclass *cls* from a plain ``dict`` produced by
    :func:`to_primitive`. Unknown keys are ignored; missing keys fall back to
    the dataclass defaults, so stored files survive schema evolution."""
    if not dataclasses.is_dataclass(cls):
        raise TypeError(f"{cls!r} is not a dataclass")

    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue  # use the field default / default_factory
        kwargs[f.name] = _coerce(data[f.name], hints.get(f.name, Any))
    return cls(**kwargs)  # type: ignore[arg-type]


class SerializableMixin:
    """Mixin giving any dataclass ``to_dict`` / ``from_dict`` convenience
    methods with the shared serialization behaviour."""

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls: type[T], data: dict[str, Any]) -> T:  # type: ignore[misc]
        return from_dict(cls, data)
