"""Request-scoped DataLoaders over the shared AsyncMlb client.

A new Loaders instance is created for every GraphQL request (see context.py),
so its caches live exactly as long as one operation. Within that operation,
loading the same key twice returns the same pending future, so repeated or
aliased selections share one upstream call.

This is caching and deduplication, not backend batching: the upstream methods
used here fetch one entity per HTTP request, so a batch of distinct keys is
still one logical lookup per key, admitted through the shared semaphore.
True backend batching remains outside this milestone.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from mlbstatsapi import AsyncMlb
from mlbstatsapi.exceptions import TheMlbStatsApiException
from mlbstatsapi.models.people import Person, Player
from mlbstatsapi.models.teams import Team
from pydantic import ValidationError
from strawberry.dataloader import DataLoader

from .upstream import RequestLookups

logger = logging.getLogger(__name__)

# Library failures that resolvers report as UPSTREAM_ERROR.
UPSTREAM_EXCEPTIONS = (TheMlbStatsApiException, ValidationError)


async def _fetch_each[K, V](
    keys: list[K], fetch: Callable[[K], Awaitable[V]], what: str
) -> list[V | BaseException]:
    """Fetch every key concurrently, one upstream call per key.

    Exceptions are returned in place of values, so DataLoader fails only the
    futures for those keys: one failed ID never affects another. The failed
    future stays cached as an exception for the rest of the request, so a
    repeated selection sees the same error rather than an empty result.
    """
    results = await asyncio.gather(*(fetch(key) for key in keys), return_exceptions=True)
    for key, result in zip(keys, results):
        if isinstance(result, UPSTREAM_EXCEPTIONS):
            logger.warning("MLB %s lookup failed for %s", what, key, exc_info=result)
    return results


class Loaders:
    def __init__(self, mlb: AsyncMlb, lookups: RequestLookups) -> None:
        self.team: DataLoader[int, Team | None] = DataLoader(
            lambda ids: _fetch_each(
                ids, lambda id: lookups.run("team", lambda: mlb.get_team(id)), "team"
            )
        )
        self.person: DataLoader[int, Person | None] = DataLoader(
            lambda ids: _fetch_each(
                ids, lambda id: lookups.run("person", lambda: mlb.get_person(id)), "player"
            )
        )
        self.active_roster: DataLoader[int, list[Player]] = DataLoader(
            lambda ids: _fetch_each(
                ids, lambda id: lookups.run(
                    "roster", lambda: mlb.get_team_roster(id, rosterType="active")
                ), "roster"
            )
        )

        self.team_stats: DataLoader[tuple[int, int], dict] = DataLoader(
            lambda keys: _fetch_each(
                keys, lambda key: lookups.run(
                    "team_stats", lambda: mlb.get_team_stats(
                        key[0], stats=["season"], groups=["hitting", "pitching"],
                        season=key[1], gameType="R",
                    )
                ), "team stats"
            )
        )
