"""Client-safe GraphQL errors shared by the resolvers."""

from graphql import GraphQLError

UPSTREAM_ERROR_MESSAGE = "The MLB service is currently unavailable. Please try again later."


def upstream_error() -> GraphQLError:
    return GraphQLError(UPSTREAM_ERROR_MESSAGE, extensions={"code": "UPSTREAM_ERROR"})


def require_positive_id(id: int) -> None:
    """Reject IDs that cannot exist upstream before any request is made."""
    if id <= 0:
        raise GraphQLError(
            "Argument 'id' must be a positive integer.",
            extensions={"code": "BAD_USER_INPUT"},
        )


def should_mask_error(error: GraphQLError) -> bool:
    """Mask unexpected exceptions; keep deliberate GraphQL and validation errors.

    Resolver exceptions arrive wrapped in a GraphQLError whose original_error is
    the raised exception. Query validation errors have no original_error.
    """
    original = error.original_error
    return original is not None and not isinstance(original, GraphQLError)


def require_season(season: int) -> None:
    """Validate structure, without promising historical coverage."""
    if not 1000 <= season <= 9999:
        raise GraphQLError(
            "Argument 'season' must be a four-digit integer between 1000 and 9999.",
            extensions={"code": "BAD_USER_INPUT"},
        )
