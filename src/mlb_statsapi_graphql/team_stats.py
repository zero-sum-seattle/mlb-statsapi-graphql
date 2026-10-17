"""Select season totals from the locked library's parsed Stat/Split models."""

from mlbstatsapi.models.stats.hitting import SimpleHittingSplit
from mlbstatsapi.models.stats.pitching import SimplePitchingSplit
from mlbstatsapi.models.stats.stats import Stat


def season_total(
    data: dict, group: str, team_id: int, season: int
) -> SimpleHittingSplit | SimplePitchingSplit | None:
    block: Stat | None = (data.get(group) or {}).get("season")
    if block is None or block.group != group or block.type != "season":
        return None
    candidates = []
    for split in block.splits or []:
        # Optional metadata need not exist, but supplied metadata must agree.
        if split.season is not None and split.season != str(season):
            continue
        if split.team is not None and split.team.id != team_id:
            continue
        if split.game_type is not None and split.game_type != "R":
            continue
        # Player or position-specific rows are not whole-team totals.
        if split.player is not None or split.position is not None:
            continue
        candidates.append(split.stat)
    # Never pick an arbitrary split or sum potentially overlapping totals.
    return candidates[0] if len(candidates) == 1 else None
