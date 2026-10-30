<!--
Keep the three sections. The runner starts processes, so a change to a guard, a cap, a mount, or the
allow-list is a security change and needs the measurement filled in.
-->

## Behavior change

What does the code do now that it did not do before, stated as behavior and not as a diff.

## Measurement

The command you ran and the output it produced. For a guard, show the refusal it now gives and the
state it prevents. For a cap, show the bytes. For a job lifecycle, show the job id, the states in
order, and the exit code.

```text
paste it here
```

## Checks

- [ ] `python -m py_compile server.py smoke.py`
- [ ] `docker build -t ergon-pr-test .`
- [ ] `ERGON_URL=http://127.0.0.1:8000 ERGON_TOKEN=<token> python3 smoke.py` reports 0 failed
- [ ] Every startup guard still exits 2 with a `refusing to start` line
- [ ] No `.env`, state volume, workspace content, token, or private host path in the diff
- [ ] A new refusal is written in policy words that name the setting, not interpreter text
- [ ] `server.py` still imports nothing outside the standard library
