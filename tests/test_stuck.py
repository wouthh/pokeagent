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


def fight(hp, pos=0):
    return {**state(), "battle": {"enemy_hp": hp, "enemy_pos": pos}}


def test_damage_dealt_in_a_battle_is_progress_but_healing_back_is_not():
    ladder = Ladder(Gen1)
    for i in range(5 * STEP):  # attrition: the enemy's HP keeps falling
        assert ladder.observe(fight(500 - i), 1) == 0
    ladder = Ladder(Gen1)
    ladder.observe(fight(90), 1)
    climbs = [ladder.observe(fight(100 - 10 * (i % 2)), 1) for i in range(STEP + 1)]  # heals back, never lower
    assert climbs.count(1) == 1 and ladder.level == 1


def test_the_enemys_next_pokemon_counts_and_the_battle_memory_resets():
    ladder = Ladder(Gen1)
    ladder.observe(fight(0, pos=0), 1)
    for _ in range(STEP - 1):
        ladder.observe(fight(0, pos=0), 1)
    assert ladder.observe(fight(50, pos=1), 1) == 0 and ladder.since == 0
    ladder.observe(state(), 1)  # the battle ended
    assert ladder.foe is None
    ladder.observe(fight(90, pos=1), 1)
    assert ladder.foe == (1, 90) and ladder.since == 2  # a new battle starts from scratch; meeting it is not progress


def test_meeting_wild_pokemon_and_fleeing_is_not_progress():
    ladder = Ladder(Gen1)
    ladder.observe(state(), 1)
    climbs = [ladder.observe(fight(40) if i % 2 else state(), 1) for i in range(STEP)]
    assert climbs[-1] == 1 and ladder.level == 1
