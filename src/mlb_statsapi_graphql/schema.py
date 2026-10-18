"""Root queries for teams and individual players, plus resource limits."""

import strawberry
from strawberry.extensions import (
    MaskErrors,
    MaxAliasesLimiter,
    MaxTokensLimiter,
    QueryDepthLimiter,
)

from .complexity import QueryCostLimiter
from .costs import priced
from .errors import should_mask_error
from .resolvers import resolve_player, resolve_team
from .settings import Limits
from .types import Player, Team


@strawberry.type
class Query:
    player: Player | None = strawberry.field(
        resolver=resolve_player, metadata=priced(
            5, request_key=lambda parent, args: ("player", args["id"]),
            result_key=lambda parent, args: args["id"],
        ),
    )
    team: Team | None = strawberry.field(
        resolver=resolve_team, metadata=priced(
            5, request_key=lambda parent, args: ("team", args["id"]),
            result_key=lambda parent, args: args["id"],
        ),
    )


def create_schema(limits: Limits) -> strawberry.Schema:
    # Checks run in request order: the token limit stops the parser, depth and
    # alias limits are validation rules, and the cost limit runs after
    # validation but before any resolver. Rejections are GraphQLErrors without
    # an original exception, so MaskErrors never hides them. Strawberry logs
    # every error with its traceback before MaskErrors replaces unexpected
    # exceptions with a generic message for the client.
    return strawberry.Schema(
        query=Query,
        extensions=[
            lambda: MaxTokensLimiter(max_token_count=limits.max_tokens),
            lambda: QueryDepthLimiter(max_depth=limits.max_depth),
            lambda: MaxAliasesLimiter(max_alias_count=limits.max_aliases),
            lambda: QueryCostLimiter(max_cost=limits.max_cost),
            lambda: MaskErrors(should_mask_error=should_mask_error),
        ],
    )


schema = create_schema(Limits.from_env())
