from pokeagent.games.pokemon_gen1 import Gen1
from pokeagent.stuck import STEP, Ladder, brief, describe


def state(x=0, exp=0, map_id=1):
    return {"map_id": map_id, "x": x, "y": 0, "badges": 0, "dex_owned": 0, "party": [{"level": 5, "exp": exp}]}


def test_a_looping_agent_climbs_the_ladder_and_progress_resets_it():
    ladder = Ladder(Gen1)
    climbs = {}
    for i in range(5 * STEP + 3):
        if level := ladder.observe(state(x=i % 2), 1):
            climbs[level] = ladder.since
    assert climbs == {1: STEP, 2: 2 * STEP, 3: 3 * STEP, 4: 4 * STEP, 5: 5 * STEP} and ladder.level == 5
    assert ladder.observe(state(x=2), 1) == 0 and (ladder.level, ladder.since) == (0, 0)  # a new tile


def test_a_grinding_agent_on_the_same_tiles_does_not():
    ladder = Ladder(Gen1)
    for i in range(5 * STEP):
        assert ladder.observe(state(x=i % 2, exp=100 * i), 1) == 0
    assert ladder.level == 0


def test_notices_say_what_happens():
    assert "holds" not in describe(1, STEP) and "reflect(analysis, plan)" in describe(2, 2 * STEP)
    assert "pauses for the user" in describe(5, 5 * STEP)
    assert "level 3 of 5" in brief(3, 3 * STEP) and "4 goes back" in brief(3, 3 * STEP)
