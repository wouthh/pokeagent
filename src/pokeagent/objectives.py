"""Success checks: the condition grammar, judging a run between sessions, and settling the agent's claims.

A condition is `{"obs": name, "op": "==" | "!=" | ">=" | ">" | "<=" | "<", "value": number or bool}`, or
`{"all": [...]}`, `{"any": [...]}` or `{"not": condition}` over those.

Authoritative checks (the user's `--success`, or the adapter's check for the default objective) complete the run. An
agent's proposed check, and an agent's claim without any check, only pause the run until the user confirms or rejects.
"""

import operator

from pokeagent.memory import Memory

OPS = {"==": operator.eq, "!=": operator.ne, ">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt}


def validate(condition, names) -> None:
    """Raise ValueError unless `condition` follows the grammar over observation `names`."""
    if not isinstance(condition, dict) or len(condition) not in (1, 3):
        raise ValueError(f"not a condition: {condition!r}")
    if "obs" in condition:
        if condition["obs"] not in names:
            raise ValueError(f"unknown observation {condition['obs']!r}; known: {', '.join(names)}")
        if condition.get("op") not in OPS:
            raise ValueError(f"op must be one of {' '.join(OPS)}")
        if not isinstance(condition.get("value"), int | float | bool):
            raise ValueError("value must be a number or a boolean")
    elif "not" in condition:
        validate(condition["not"], names)
    elif key := next((k for k in ("all", "any") if k in condition), None):
        if not isinstance(condition[key], list) or not condition[key]:
            raise ValueError(f"{key} needs a non-empty list of conditions")
        for part in condition[key]:
            validate(part, names)
    else:
        raise ValueError(f"not a condition: {condition!r}")


def holds(condition: dict, observations: dict) -> bool:
    if "obs" in condition:
        return OPS[condition["op"]](observations[condition["obs"]], condition["value"])
    if "not" in condition:
        return not holds(condition["not"], observations)
    if "all" in condition:
        return all(holds(c, observations) for c in condition["all"])
    return any(holds(c, observations) for c in condition["any"])


def describe(condition: dict) -> str:
    if "obs" in condition:
        return f"{condition['obs']} {condition['op']} {condition['value']}"
    if "not" in condition:
        return f"not ({describe(condition['not'])})"
    parts, joiner = (condition["all"], " and ") if "all" in condition else (condition["any"], " or ")
    return joiner.join(f"({describe(c)})" for c in parts)


def judge(memory: Memory, observations: dict) -> str | None:
    """Between sessions: 'complete' when an authoritative check holds, 'claimed' when the agent's latest proposed check
    holds, else None. Power-on RAM is noise, so nothing is judged before a game is in progress."""
    if not observations["in_game"]:
        return None
    for check in memory.checks("active"):
        if holds(check["condition"], observations):
            reason = f"{check['source']} check #{check['id']} passed: {describe(check['condition'])}"
            memory.set_status("complete", reason)
            memory.event("complete", {"check": check["id"], "observations": observations})
            return "complete"
    if (proposed := memory.checks("proposed")) and holds(proposed[-1]["condition"], observations):
        check = proposed[-1]
        claim(memory, f"the agent's proposed check #{check['id']} passed: {describe(check['condition'])}", check["id"])
        return "claimed"
    return None


def claim(memory: Memory, reason: str, check: int | None = None) -> None:
    memory.set_status("claimed", reason)
    memory.event("claim", {"reason": reason, "check": check})


def confirm(memory: Memory) -> str:
    status, reason = memory.status()
    if status != "claimed":
        raise ValueError(f"run {memory.run} is {status}, not waiting for a confirmation")
    memory.set_status("complete", f"confirmed by the user: {reason}")
    memory.event("claim_confirmed", {"reason": reason})
    return reason


def reject(memory: Memory, why: str) -> str:
    """Resume playing: the claim's check (if any) is rejected so it cannot pause the run again, and the next brief
    tells the agent why."""
    status, reason = memory.status()
    if status != "claimed":
        raise ValueError(f"run {memory.run} is {status}, not waiting for a confirmation")
    if check := memory.events("claim", limit=1)[0]["data"]["check"]:
        memory.settle_check(check, "rejected")
    memory.set_status("running")
    memory.event("claim_rejected", {"claim": reason, "why": why})
    return reason


def approve_check(memory: Memory) -> dict:
    """Make the agent's latest proposed check authoritative (as a user check)."""
    status, _ = memory.status()
    if status != "running":
        raise ValueError(f"run {memory.run} is {status}; settle it with confirm or reject first")
    proposed = memory.checks("proposed")
    if not proposed:
        raise ValueError(f"run {memory.run} has no proposed check")
    check = proposed[-1]
    memory.settle_check(check["id"], "approved")
    memory.add_check(check["condition"], "user", f"approved agent check #{check['id']}")
    return check["condition"]
