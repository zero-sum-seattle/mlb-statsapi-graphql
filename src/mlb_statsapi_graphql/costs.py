"""Field declarations shared by the schema and its upstream cost estimator."""

from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass
from typing import Any

Identity = Callable[[Hashable | None, Mapping[str, Any]], Hashable | None]


@dataclass(frozen=True)
class Cost:
    """None identities are unknown; request and result identities are independent.

    max_items bounds each list layer. Omit it unless a finite maximum is
    guaranteed by the resolver's contract. It never limits execution itself.
    """

    weight: int
    request: Identity = lambda parent, args: None
    result: Identity = lambda parent, args: None
    max_items: int | None = None

    def __post_init__(self) -> None:
        if self.weight < 0 or (self.max_items is not None and self.max_items < 0):
            raise ValueError("Costs and cardinality bounds must be nonnegative")


def priced(weight: int, **kwargs: Any) -> dict[str, Cost]:
    return {"cost": Cost(weight, **kwargs)}


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
