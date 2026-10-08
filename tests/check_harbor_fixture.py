"""Checks what `karotte harbor validate` made of the Harbor fixture's tasks.

The honest task must validate: its solution scores 1 and an empty submission 0.
Every attack's "solution" must score 0, graded normally (verifier status ok),
so it fails validation.
"""

import json
import sys

validation = json.loads(open(sys.argv[1]).read())
expected_attacks = {
    "fixture-forge-reward",
    "fixture-forge-report-late",
    "fixture-reach-out",
}
problems = []

honest = validation.get("fixture-honest-add")
if not honest or not honest["passed"]:
    problems.append(f"fixture-honest-add did not validate: {honest}")
for task_id in sorted(expected_attacks):
    result = validation.get(task_id)
    oracle = (result or {}).get("oracle", {})
    if result is None:
        problems.append(f"{task_id} was not run")
    elif (
        result["passed"]
        or oracle.get("score") != 0
        or oracle.get("verifier_status") != "ok"
    ):
        problems.append(f"{task_id}'s attack was not graded 0: {oracle}")

if problems:
    print("\n".join(problems))
    sys.exit(1)
print("The honest task validated and every attack scored 0.")
