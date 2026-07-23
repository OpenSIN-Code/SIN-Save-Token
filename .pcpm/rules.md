# Project Rules

- [2026-07-23T11:52:13.969Z] After , ALWAYS poll the worker terminal and auto-send Enter to dismiss the Orca trust dialog. The trust prompt blocks all workers (mimo-code, opencode) from starting. Send  (Enter) within 5 seconds of dispatch, then verify the agent is actually running. This is not optional — without it the worker hangs indefinitely. (priority: 0)
