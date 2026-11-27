import sqlite3
import time

import pytest

from pokeagent import objectives
from pokeagent.memory import Memory

META = {
    "objective": "finish the game",
    "rom_sha1": "red",
    "family": "pokemon-gen1",
    "check": {"obs": "badges", "op": ">=", "value": 8},
}
NAMES = ["map_id", "badges"]


def open_memory(tmp_path, run="run-1", **meta) -> Memory:
    memory = Memory(tmp_path / "memory.db", run)
    memory.open_run(META | meta)
    return memory


def test_the_objective_checks_and_events_never_change(tmp_path):
    memory = open_memory(tmp_path)
    memory.event("action", {"name": "press"})
    with pytest.raises(sqlite3.DatabaseError, match="never change"):
        memory.write("UPDATE runs SET objective = 'nothing'")
    assert [c["source"] for c in memory.checks("active")] == ["adapter"]
    for sql in ("UPDATE checks SET condition = '{}'", "UPDATE checks SET status = 'rejected'", "DELETE FROM checks"):
        with pytest.raises(sqlite3.DatabaseError):
            memory.write(sql)
    with pytest.raises(sqlite3.DatabaseError):  # an agent's check is never authoritative
        memory.write(
            "INSERT INTO checks (run, condition, source, status, created) VALUES ('run-1', '{}', 'agent', 'active', 0)"
        )
    for sql in ("UPDATE events SET kind = 'x'", "DELETE FROM events"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            memory.write(sql)
    memory.close()
    reopened = open_memory(tmp_path)  # everything survives the process
    assert reopened.status() == ("running", "") and len(reopened.events("action")) == 1
    with pytest.raises(ValueError, match="no longer matches"):  # run.json edited behind pokeagent's back
        open_memory(tmp_path, objective="something easier")


def test_condition_grammar():
    condition = {"all": [{"obs": "badges", "op": ">=", "value": 2}, {"not": {"obs": "map_id", "op": "==", "value": 1}}]}
    objectives.validate(condition, NAMES)
    assert objectives.holds(condition, {"badges": 2, "map_id": 3})
    assert not objectives.holds(condition, {"badges": 2, "map_id": 1})
    assert objectives.describe(condition) == "(badges >= 2) and (not (map_id == 1))"
    bad = [
        {"obs": "hp", "op": "==", "value": 1},
        {"obs": "badges", "op": "~", "value": 1},
        {"obs": "badges", "op": "==", "value": "8"},
        {"any": []},
        {"all": [{"obs": "badges", "op": "==", "value": 1}], "x": 1},
        [1],
    ]
    for condition in bad:
        with pytest.raises(ValueError):
            objectives.validate(condition, NAMES)


def test_agent_checks_and_claims_only_pause_the_run(tmp_path):
    memory = open_memory(tmp_path, objective="catch a Pikachu", check=None)
    now = {"in_game": True, "badges": 0, "map_id": 1}
    assert objectives.judge(memory, now) is None and memory.checks("active") == []
    check = memory.add_check({"obs": "map_id", "op": "==", "value": 1}, "agent", "the Pikachu map")
    assert objectives.judge(memory, now | {"in_game": False}) is None  # power-on RAM is noise
    assert objectives.judge(memory, now) == "claimed" and memory.status()[0] == "claimed"
    with pytest.raises(ValueError, match="settle it"):
        objectives.approve_check(memory)
    objectives.reject(memory, "no Pikachu in the party")
    assert memory.status()[0] == "running" and [c["id"] for c in memory.checks("rejected")] == [check]
    assert objectives.judge(memory, now) is None  # a rejected check cannot pause the run again
    objectives.claim(memory, "the agent reported the objective complete: caught one")
    assert objectives.confirm(memory).endswith("caught one") and memory.status()[0] == "complete"
    with pytest.raises(ValueError, match="not waiting"):
        objectives.reject(memory, "")


def test_an_approved_check_becomes_authoritative(tmp_path):
    memory = open_memory(tmp_path, objective="earn a badge", check=None)
    memory.add_check({"obs": "badges", "op": ">=", "value": 1}, "agent")
    assert objectives.approve_check(memory) == {"obs": "badges", "op": ">=", "value": 1}
    assert [(c["source"], c["status"]) for c in memory.checks("active")] == [("user", "active")]
    assert objectives.judge(memory, {"in_game": True, "badges": 1, "map_id": 0}) == "complete"


def test_knowledge_scopes_evidence_and_search(tmp_path):
    red = open_memory(tmp_path)
    action = red.event("action", {"name": "walk"})
    web = red.add_knowledge(
        "map", "Viridian Forest exit", "north side", url="https://example.org/guide", evidence=action
    )
    seen = red.add_knowledge("mechanic", "Ledges", "ledges are jumped down only", evidence=action)
    family = red.add_knowledge("strategy", "Forest bugs", "Caterpie is weak to fire", scope="family")
    general = red.add_knowledge("lesson", "Menus", "read the cursor before pressing", scope="general")
    red.add_knowledge("progress", "Parcel", "Oak's parcel delivered", scope="run")
    status = dict(red.rows("SELECT id, status FROM knowledge"))
    assert (status[web], status[seen], status[general]) == ("hypothesis", "confirmed", "hypothesis")
    with pytest.raises(ValueError, match="scope must be run"):
        red.add_knowledge("progress", "Parcel", "delivered")
    with pytest.raises(ValueError, match="evidence"):
        red.set_knowledge_status(web, "confirmed")
    with pytest.raises(ValueError, match="not an action"):
        red.set_knowledge_status(web, "confirmed", evidence=red.event("checkpoint", {}))
    red.set_knowledge_status(web, "confirmed", evidence=action)
    with pytest.raises(sqlite3.DatabaseError, match="never rewritten"):
        red.write("UPDATE knowledge SET content = 'south side'")
    query = "forest exit ledges menus parcel"
    assert {e["topic"] for e in red.search(query, 10)} == {
        "Viridian Forest exit",
        "Ledges",
        "Forest bugs",
        "Menus",
        "Parcel",
    }
    assert red.search('"; DROP TABLE knowledge --') == [] and red.search("") == []

    blue = open_memory(tmp_path, run="run-2", rom_sha1="blue")  # same family, other ROM, other run
    assert {e["id"] for e in blue.search(query, 10)} == {family, general}
    with pytest.raises(ValueError, match="not an action"):  # evidence must come from the run's own game
        blue.set_knowledge_status(general, "confirmed", evidence=action)
    with pytest.raises(ValueError, match="unknown"):
        blue.set_knowledge_status(seen, "refuted")
    newer = blue.add_knowledge("lesson", "Menus", "read the cursor and the menu title", scope="general")
    blue.set_knowledge_status(general, "hypothesis", superseded_by=newer)
    blue.set_knowledge_status(family, "refuted")
    assert [e["id"] for e in blue.search(query, 10)] == [newer]


def test_superseding_needs_a_replacement_as_wide_and_restore_drops_later_progress(tmp_path):
    red = open_memory(tmp_path)
    general = red.add_knowledge("lesson", "Menus", "read the cursor", scope="general")
    narrow = red.add_knowledge("lesson", "Menus", "red only", scope="rom")
    with pytest.raises(ValueError, match="widely scoped"):
        red.set_knowledge_status(general, "hypothesis", superseded_by=narrow)
    red.set_knowledge_status(narrow, "hypothesis", superseded_by=general)

    red.add_knowledge("progress", "Badge", "earned", scope="run")
    red.event("checkpoint", {"file": "1.state", "reason": "test"})
    time.sleep(0.01)
    red.add_knowledge("progress", "Parcel", "delivered", scope="run")
    red.discard_progress_after("1.state")
    assert {e["topic"] for e in red.search("badge parcel", 10)} == {"Badge"}
