"""Stands in for `claude -p` in the Runner tests. FAKE_CLAUDE_CONTROL names a JSON file:
{"mode": ..., "log": path}. Every call appends {"argv", "stdin", "api_key", "cwd"} to the log."""

import json
import os
import sys
import time

control = json.load(open(os.environ["FAKE_CLAUDE_CONTROL"]))
mode = control["mode"]
argv = sys.argv[1:]
with open(control["log"], "a") as f:
    f.write(
        json.dumps({"argv": argv, "stdin": sys.stdin.read(), "api_key": "ANTHROPIC_API_KEY" in os.environ, "cwd": os.getcwd()})
        + "\n"
    )

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
