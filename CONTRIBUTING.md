# Contributing

## Run it first

```bash
cp .env.example .env          # then set ERGON_TOKEN
docker compose up -d --build
curl -s localhost:8000/health
ERGON_TOKEN=<the token from .env> python3 smoke.py
```

`smoke.py` is standard library only, so it runs from any host that can reach the port. Every change
should leave it passing.

## Rules the code keeps

* **Standard library only in `server.py`.** No dependency install in the image means nothing to fetch,
  nothing to audit, and nothing to update on the hot path.
* **No new tool without a policy.** Each tool is a capability. A proposal for a seventh tool should say
  what it refuses, and a refusal should be written in policy words that name the setting, not in
  interpreter text. `KeyError: 'job_id'` is a bug report; `refused: poll_job requires job_id` is a
  policy.
* **Every guard has a measurement beside it.** Where the code refuses a startup configuration, the
  comment names the state it prevents, in the form the code was caught in. Several of these rules came
  from a probe that showed a working wipe, not from a theory.
* **Bounds are per job, and the total is bounded by the disk.** `ERGON_LOG_CAP_MB` bounds one log, and
  `ERGON_MIN_FREE_MB` bounds the volume. A change to one should say what happens to the other.
* **A lost process is reported as lost.** After a restart the sidecars restore as `lost`. A record that
  disappears silently is a worse failure than one that says it cannot account for the process.

## Pull requests

Describe the behavior change, the measurement that shows it, and the check that pins it. Keep the commit
message in the imperative and name the subsystem (`fix(start_job): refuse a cap above the ceiling`).
Do not commit `.env`, the state volume, or the workspace.
