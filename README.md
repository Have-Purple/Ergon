# Ergon

**A job runner for AI agents.** Start a long command, get a handle back in milliseconds, and check on it later.

Ergon runs commands on an agent's behalf in the background and keeps them alive after the tool call that started them has returned. The agent reaches it over the Model Context Protocol (MCP), the plug-in standard that lets a model call outside tools.

## What It Is

A session's command tool usually caps at around 30 seconds and starts a fresh shell on every call. Two things follow. A task that takes longer than the cap cannot be started at all, and nothing the agent starts can outlive the call that started it.

Ergon moves the long-running part off the agent's hands. The agent asks for a job, the runner starts the process and returns a durable identifier, and the agent keeps working. Later it polls the state, reads the tail of the log, and kills the job if needed. The identifier survives the tool call, the shell, and the session.

The shape is a laundromat. You hand over a load, you get a ticket number, the machine runs while you do something else, and you check the ticket rather than standing at the door.

## Why a Job Runner, and Not a Shell

| | Shell command slot | Ergon |
|---|---|---|
| Runtime ceiling | Caps near 30 seconds, so a 40-minute sweep cannot start | No wall limit on the job itself; the process runs to completion |
| Lifetime | The process dies with the call that started it | The job and its log outlive the call, the shell, and the session |
| Cost of a status check | A new shell and a full round trip per peek | One small call returning state, exit code, elapsed time, last log line |
| Quoting | A shell string must survive every quoting layer between the model and the process | An argv list, so there is no quoting layer to get wrong |
| Blast radius | Runs as the user, with anything on the machine reachable | Only programs on a written allow-list may start |
| Runaway output | Truncated in the middle, and floods the model's context | Bounded per job by a byte cap, recorded as `capped` with the cause named |
| Concurrency | Unbounded; parallel jobs thrash the machine | Refuses past a configured job limit, and names the limit in the refusal |
| Cleanup | An interrupted command leaves child processes behind | Kills the whole process group, so children die with the job |
| Audit trail | Whatever the transcript happened to capture | A durable record: argv, cwd, pid, start and end, exit code, log path |

The mechanism that matters is not speed. A long task becomes a handle the model can carry across many steps. In a shell slot the agent must fit a job inside one call, so it splits work into guesses about size, and a long sweep becomes impossible or is replaced by a truncated sample.

## The Tools

| Tool | What it does |
|---|---|
| `start_job` | Starts a job from an argv list, an optional working directory inside the workspace, an optional environment map, an optional name, and an optional per-job log cap. Returns the job id immediately. |
| `poll_job` | Returns the state, exit code, elapsed seconds, and the last line of the log for one job. |
| `tail_job` | Returns the end of a job's log, under a small byte limit. |
| `list_jobs` | Returns every known job, newest first, with an optional state filter. |
| `kill_job` | Requests termination, then kills the process group if it survives a short grace period. |
| `prune_jobs` | Deletes finished records and logs older than a window, and reports the counts and bytes freed. Takes a dry-run flag. |

Read tools are the ones an agent should be allowed to call without asking. Starting and killing are writes, and belong behind approval.

## Job States

| State | Meaning |
|---|---|
| `running` | Started and not finished |
| `passed` | Exited 0 |
| `failed` | Exited non-zero |
| `capped` | Output reached the job's byte cap; the log was truncated there and the process group was stopped |
| `killed` | Stopped by `kill_job` |
| `terminated` | Stopped on request, having exited on the signal |
| `lost` | The runner can no longer account for the process, as after a service restart |

A log that stays empty for a long stretch is normal for a program that buffers its output and prints at the end. Check the container's CPU and the process's open files before concluding a job has hung.

## Guardrails

* **Allow-list.** `argv[0]` must appear in `ERGON_ALLOW`. A request naming a program off the list is refused before anything starts, so a generic shell is not reachable through the runner.
* **No shell string.** argv is a list, never a command line. Piping, redirection, and a shell wrapper are unavailable by construction.
* **Workspace confinement.** The working directory must resolve inside the mounted workspace, and a request that escapes it is refused.
* **Declared mounts.** Mount the working tree read-write and any data set the job should only read. A write to a read-only mount fails inside the job, which is the point.
* **Bounded output.** Each job writes to its own log under a byte cap. A program that prints forever stops at the cap and is marked `capped` instead of filling the disk.
* **Bounded concurrency.** `ERGON_MAX_JOBS` caps processes in flight, and the refusal names the limit and the remedy.
* **Disk guard.** A minimum-free-space setting refuses to start work that would crowd the state volume.
* **Process-group kill.** Every job runs in its own process group, so a kill removes spawned children. Count the group's members in `/proc` before and after to see it.
* **Token auth.** A bearer token is required and compared in constant time, and the server refuses to start when the token is empty, so it cannot come up open. A health endpoint answers without a token and reveals only liveness and the running-job count.
* **Retention.** Finished records and logs are removed by age, and the prune reports the bytes freed. The runner is a command tool, not storage.

## Configuration

| Variable | Purpose |
|---|---|
| `ERGON_TOKEN` | Bearer token for every request. Required; an empty value is a startup refusal. |
| `ERGON_ALLOW` | Programs an agent may start. This is the security boundary. |
| `ERGON_PASS_ENV` | Environment variables passed through to jobs. Everything else is withheld. |
| `ERGON_PORT` | Port the server binds inside the container. |
| `ERGON_HOST_PORT` | Port published on the host. Compose only; the server binds `ERGON_PORT`. |
| `ERGON_WORKSPACE` | Host directory mounted at `/workspace`, the only place a job may run. |
| `ERGON_STATE` | Container directory for job records and logs, default `/state`. Records store argv at mode `0600`, so keep that volume private. |
| `ERGON_MAX_JOBS` | Concurrent jobs allowed before a start is refused. |
| `ERGON_LOG_CAP_MB` | Default per-job output cap. |
| `ERGON_LOG_CAP_MAX_MB` | Ceiling a caller may request for one job. |
| `ERGON_REAP_SECONDS` | Interval between reaps of finished processes. |
| `ERGON_RETAIN_HOURS` | Age after which finished records and logs are deleted. |
| `ERGON_PRUNE_MIN_HOURS` | The youngest window a `prune_jobs` request may ask for. |
| `ERGON_MIN_FREE_MB` | Free space required on the state volume before a job starts. |
| `ERGON_TAG` | Image tag to build and run; also the version the server reports. |

Two mounts carry the rest: a workspace the runner can enter, and a state volume for job records and logs. Publish the port only on the interfaces that need it, because the token is the only thing between a caller and the allow-list.

## Repository Contents

| File | What it is |
|---|---|
| `server.py` | The server. Standard library only: six tools over JSON-RPC 2.0 and Streamable HTTP. |
| `Dockerfile` | A slim Python base, a non-root user, no dependency install, a healthcheck. |
| `docker-compose.yml` | The deployment: port, token, allow-list, limits, state volume, workspace mount. |
| `.env.example` | The settings to copy into `.env`. `.env` holds the secret and is never committed. |
| `smoke.py` | A standard-library client that checks health, the token, the handshake, the six tools, one real job, and the refusals. |
| `.gitignore` | Keeps `.env`, the state volume, the workspace, and logs out of the repository. |
| `SECURITY.md` | The boundary, how to report a vulnerability, and the behavior treated as a regression. |
| `CONTRIBUTING.md` | How to run it, and the rules the code keeps. |
| `.github/workflows/ci.yml` | Two jobs on every push and pull request: startup guards, and the smoke run over compose. |
| `.github/ISSUE_TEMPLATE/` | A bug report form that asks for the promise being broken, and a feature form that asks for the refusal and the cost. |
| `.github/PULL_REQUEST_TEMPLATE.md` | Behavior, measurement, and the checks a change must leave passing. |

The CI workflow needs no secrets: it starts the service on the runner's loopback with a literal
throwaway token, so the same two jobs reproduce locally with the commands in its steps.


## Quick Start

Deploy the service:

```bash
mkdir -p ~/ergon/workspace && cd ~/ergon
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # paste the value into .env
docker compose up -d --build
curl -s localhost:8000/health
ERGON_TOKEN=<the token from .env> python3 smoke.py
```

The last line is the repository's own smoke client: eleven checks over health, the bearer token, the
handshake, the six advertised tools, one real job from start to its logged output, and the refusals for
a program off the allow-list, an escaping `cwd`, and an argv written as a shell string.

Register it with an MCP-capable client:

```json
{
  "mcpServers": {
    "ergon": {
      "type": "streamableHttp",
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer REPLACE_WITH_ACTUAL_TOKEN" },
      "autoApprove": ["poll_job", "tail_job", "list_jobs"]
    }
  }
}
```

Start a job:

```text
start_job
  name: build
  argv: ["python3", "tools/long_scan.py", "--all"]
  cwd: /workspace
```

Then, at whatever pace the task needs:

```text
poll_job  20260101T120000Z-build-1a2b3c
tail_job  20260101T120000Z-build-1a2b3c
kill_job  20260101T120000Z-build-1a2b3c
```

## Design Notes

* **The server is small.** Standard library only, six tools over JSON-RPC 2.0 and Streamable HTTP, one container image, a non-root user, a healthcheck.
* **The handle is the product.** The job id is what the model keeps across turns, and the process, the log, and the record all outlive the call that created them.
* **Refusals are recorded.** A disallowed program, an escaping working directory, and an over-limit start each answer with a reason naming the setting, so the caller corrects the request rather than guessing.
* **State is inspectable.** Every job keeps its argv, cwd, pid, timestamps, exit code, cap, and log path, which turns "what did the agent run" into a query.
* **The response is one JSON body.** The endpoint answers `POST /mcp` with a single `application/json` body, which the Streamable HTTP transport allows in place of an SSE stream.

## Limitations

* **POSIX only.** Stopping a process group uses `os.killpg`, so the service does not run natively on Windows. Run it in a container there.
* **Narrow on purpose.** It runs listed programs. It is not a general terminal, and adding a shell to the allow-list removes the confinement the design depends on.
* **One host.** A job runs where the service runs, and it sees that machine's mounts.
* **Not a policy engine.** A passed job is a process that exited 0. Deciding what a result means for a project stays outside the runner.
* **Output is capped.** A capped log is a truncated log, and the job note says so. A program that must emit everything needs a larger cap or a file.

## The Name

*Ergon* (Greek ἔργον) means work, or the thing that is done. It is the root of *energy* (en- plus ergon, work being done), *ergonomics*, and *synergy*, and it names the cgs unit of work, the erg. Aristotle builds his human-function argument on the word: in the *Nicomachean Ethics* the good for a thing is tied to its characteristic *ergon*, and his term for actuality, *energeia*, is built from it. A runner whose entire job is to hold work on your behalf carries the name well.

