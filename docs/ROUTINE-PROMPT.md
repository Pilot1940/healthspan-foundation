RUN MODE — you are EXECUTING already-built code, not modifying it.
DO NOT: explore the codebase, read or edit any source file, create branches or PRs, run tests,
or "understand the architecture." If you start reading .py files, STOP — that is wrong.
Do exactly this:
1. Run:  python -m monitor.inbox_drain --once
2. Report the JSON summary line it prints.
3. If it raises an error, paste the full traceback and STOP. Do not attempt to fix anything.
That is the entire job.
