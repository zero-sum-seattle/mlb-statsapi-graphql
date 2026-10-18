"""Field declarations shared by the schema and its upstream cost estimator."""

from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass
from typing import Any

Identity = Callable[[Hashable | None, Mapping[str, Any]], Hashable | None]


def _unknown_key(parent_key: Hashable | None, args: Mapping[str, Any]) -> None:
    return None


@dataclass(frozen=True)
class Cost:
    """Identify upstream work separately from the returned object for descendants.

    A team lookup has request_key ("team", id) but result_key id: descendants
    need the team ID to identify their own roster or stats requests. None means
    unknown for either identity.

    max_items bounds each list layer. Omit it unless a finite maximum is
    guaranteed by the resolver's contract. It never limits execution itself.
    """

    weight: int
    request_key: Identity = _unknown_key
    result_key: Identity = _unknown_key
    max_items: int | None = None

    def __post_init__(self) -> None:
        if self.weight < 0 or (self.max_items is not None and self.max_items < 0):
            raise ValueError("Costs and cardinality bounds must be nonnegative")


def priced(
    weight: int,
    *,
    request_key: Identity = _unknown_key,
    result_key: Identity = _unknown_key,
    max_items: int | None = None,
) -> dict[str, Cost]:
    return {"cost": Cost(
        weight, request_key=request_key, result_key=result_key, max_items=max_items,
    )}


def unpriced_resolvers(schema: Any) -> list[str]:
    """Inspect Strawberry definitions exposed by its graphql-core converter."""
    missing = []
    for type_ in schema.type_map.values():
        for name, field in getattr(type_, "fields", {}).items():
            definition = (field.extensions or {}).get("strawberry-definition")
            if (definition is not None and definition.base_resolver is not None
                    and not isinstance(definition.metadata.get("cost"), Cost)):
                missing.append(f"{type_.name}.{name}")
    return missing
