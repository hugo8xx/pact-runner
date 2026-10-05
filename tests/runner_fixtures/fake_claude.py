"""Stands in for `claude -p` in the Runner tests. FAKE_CLAUDE_CONTROL names a JSON file:
{"mode": ..., "log": path} or {"script": [mode, ...], "log": path} for one mode per call in order.
Every call appends {"argv", "stdin", "api_key", "cwd", "env"} to the log. Mode "waiting" holds until
a file named "release" next to the control file exists, so a test can post subtasks meanwhile."""

import json
import os
import sys
import time

control_path = os.environ["FAKE_CLAUDE_CONTROL"]
control = json.load(open(control_path))
argv = sys.argv[1:]
calls = sum(1 for _ in open(control["log"])) if os.path.exists(control["log"]) else 0
mode = control["script"][calls] if "script" in control else control["mode"]
with open(control["log"], "a") as f:
    entry = {
        "argv": argv,
        "stdin": sys.stdin.read(),
        "api_key": "ANTHROPIC_API_KEY" in os.environ,
        "cwd": os.getcwd(),
        "env": {k: v for k, v in os.environ.items() if k.startswith("PACT_")},
    }
    f.write(json.dumps(entry) + "\n")

resumed = argv[argv.index("--resume") + 1] if "--resume" in argv else None
session = resumed or f"sess-{os.getpid()}"


def emit(event: dict) -> None:
    print(json.dumps({**event, "session_id": session}), flush=True)


emit({"type": "system", "subtype": "init"})
emit(
    {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "rejected" if mode == "quota" else "allowed",
            "resetsAt": int(time.time()) + 7200,
            "unifiedWindows": {"five_hour": {"utilization": 0.2, "resetsAt": int(time.time()) + 7200}},
        },
    }
)

if mode == "sleep":
    time.sleep(60)
if mode == "waiting":
    release = os.path.join(os.path.dirname(control_path), "release")
    while not os.path.exists(release):
        time.sleep(0.05)
if mode == "resume_gone" and resumed:
    emit(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "num_turns": 0,
            "result": f"No conversation found with session ID: {resumed}",
        }
    )
    sys.exit(1)

results = {
    "completed": {"status": "completed", "result": "Fixed it.\n\n## Handoff\n- Done: fixed\n- Repo / branch / PR / commit: test"},
    "no_handoff": {"status": "completed", "result": "Fixed it."},
    "resume_gone": {"status": "completed", "result": "Fixed it.\n\n## Handoff\n- Done: fresh session"},
    "input_required": {"status": "input_required", "result": "", "question": "Which colour?"},
    "defer": {"status": "defer", "result": "Needs a deploy.", "needed_scope": ["deploy.web@project:web"]},
    "waiting": {"status": "waiting", "result": "Handed the docs to a worker."},
    "report": {
        "status": "completed",
        "result": "Brief.\n\n## Handoff\n- Done: brief",
        "report": {"greeting": "Morning", "sections": [{"title": "Progress", "items": [{"text": "R3 done"}]}]},
    },
    "child": {"status": "completed", "result": "Docs written.\n\n## Handoff\n- Done: docs on the worker's branch"},
}
if mode == "quota":
    emit(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "num_turns": 1,
            "result": "Claude usage limit reached. Your limit resets at 5pm.",
        }
    )
elif mode == "max_turns":
    emit({"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 7, "result": ""})
else:
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 3,
            "result": "",
            "structured_output": results[mode],
        }
    )
