"""PACT Runner: wakes headless Claude Code for tasks delegated to a runner agent.

The board cannot push, so the Runner polls it, claims a task with a run reserved from its budget,
runs ``claude -p`` in a git worktree of its own, and reports back with the turns it spent.
"""
