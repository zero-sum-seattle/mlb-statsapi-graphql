"""The small public GraphQL model, independent of the upstream model.

Root query resolvers live in resolvers.py, which imports these types. Nested
field resolvers, such as Team.roster, live on their type so this module never
imports resolvers.py.
"""

import strawberry
from strawberry.types import Info

from .context import Context
from .errors import require_season, upstream_error
from .loaders import UPSTREAM_EXCEPTIONS
from .team_stats import season_total


@strawberry.type
class Player:
    id: int
    full_name: str | None


@strawberry.type
class Position:
    abbreviation: str | None


@strawberry.type
class RosterEntry:
    player: Player
    position: Position | None


@strawberry.type
class TeamSeasonStats:
    season: int
    wins: int | None
    losses: int | None
    runs_scored: int | None
    runs_allowed: int | None


@strawberry.type
class Team:
    id: int
    name: str | None
    abbreviation: str | None

    @strawberry.field
    async def stats(self, info: Info[Context, None], season: int) -> TeamSeasonStats | None:
        require_season(season)
        try:
            data = await info.context.loaders.team_stats.load((self.id, season))
        except UPSTREAM_EXCEPTIONS as exc:
            raise upstream_error() from exc
        hitting = season_total(data, "hitting", self.id, season)
        pitching = season_total(data, "pitching", self.id, season)
        result = TeamSeasonStats(
            season=season,
            wins=pitching.wins if pitching is not None else None,
            losses=pitching.losses if pitching is not None else None,
            runs_scored=hitting.runs if hitting is not None else None,
            runs_allowed=pitching.runs if pitching is not None else None,
        )
        if all(value is None for value in (
            result.wins, result.losses, result.runs_scored, result.runs_allowed
        )):
            return None
        return result

    @strawberry.field
    async def roster(self, info: Info[Context, None]) -> list[RosterEntry] | None:
        # Runs only when roster is selected, after the team itself has resolved.
        # Aliased selections of the same team share one loader call.
        try:
            roster = await info.context.loaders.active_roster.load(self.id)
        except UPSTREAM_EXCEPTIONS as exc:
            raise upstream_error() from exc

        # The roster already contains person details; no get_person calls are needed.
        return [
            RosterEntry(
                player=Player(id=person.id, full_name=person.full_name),
                position=(
                    Position(abbreviation=person.primary_position.abbreviation)
                    if person.primary_position is not None else None
                ),
            )
            for person in roster
        ]
