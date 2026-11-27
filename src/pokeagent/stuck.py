"""The stuck ladder: actions without progress escalate from a notice to a pause of the run.

Progress is the adapter's `progress_score` rising above its best so far: a new tile, map, Pokédex entry, level,
experience or badge counts, so grinding is progress and pacing the same tiles or bumping into a wall is not. Every
`STEP` completed actions without progress climb one level:

1. a notice in the action result, with knowledge that may bear on it;
2. the action tools refuse until the agent calls `reflect`;
3. the session ends, and the next one starts with a recovery brief;
4. the session ends and the run goes back to the first checkpoint saved after the last progress (a new timeline);
5. the session ends and the run pauses for the user.

Progress resets the ladder. It lives in the runner, so it starts over when a run is resumed.
"""

STEP = 40  # actions without progress per level
TOP = 5

CONSEQUENCES = {
    1: "nothing happens yet",
    2: "your action tools now wait until you call reflect(analysis, plan)",
    3: "this session ends now; the next one starts with a recovery brief",
    4: "this session ends now, and the game goes back to the first checkpoint saved after your last progress",
    5: "this session ends now, and the run pauses for the user",
}


class Ladder:
    def __init__(self, adapter):
        self.adapter = adapter
        self.tiles: set[tuple[int, int, int]] = set()
        self.best: int | None = None
        self.since = 0  # completed actions since the last progress

    @property
    def level(self) -> int:
        return min(TOP, self.since // STEP)

    def observe(self, state: dict, maps: int) -> int:
        """Record one completed action that left the game in `state` (in game), with `maps` maps visited on this
        timeline. Returns the level it climbed to, else 0."""
        self.tiles.add((state["map_id"], state["x"], state["y"]))
        score = self.adapter.progress_score(state, maps, len(self.tiles))
        before = self.level
        if self.best is None or score > self.best:
            self.best, self.since = score, 0
            return 0
        self.since += 1
        return self.level if self.level > before else 0


def describe(level: int, since: int) -> str:
    return (
        f"pokeagent: no progress (a new tile, map, level, experience, Pokédex entry or badge) in your last {since}"
        f" actions, stuck level {level} of {TOP}: {CONSEQUENCES[level]}. Step back: research it (knowledge_search,"
        " WebSearch), read memory, build a macro or observation, request development, or try a different approach."
    )


def brief(level: int, since: int) -> str:
    return (
        f"Stuck: no progress in your last {since} actions; the stuck ladder is at level {level} of {TOP}. Every"
        f" {STEP} more actions without progress climb a level: 2 holds your actions until reflect, 3 ends the"
        " session, 4 goes back to the first checkpoint after your last progress, 5 pauses the run. Before acting,"
        " step back: research it (knowledge_search, WebSearch), read memory, build a macro or observation, request"
        " development, or try a different approach."
    )
