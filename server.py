"""Ergon: a job runner exposed as an MCP server over Streamable HTTP, standard library only.

Why this exists. The command tool a session uses caps near 30 seconds and starts a fresh shell every
call, so watching a 40-minute sweep costs one round trip per peek, and a long job cannot outlive the
call that started it. This server takes the long-running part off the session's hands: start a job, get
a handle, poll the handle, read the tail, kill it. The handle outlives the tool call, the shell, and
the session. Six tools, and nothing else.

Safety posture, deliberately narrow. A bearer token is required, and with no ERGON_TOKEN the
server refuses to start rather than running open. There is no shell: argv[0] must appear in
ERGON_ALLOW, so the allow-list is the capability boundary. cwd resolves inside WORKSPACE and is
refused on escape. Each job writes a sidecar, so a restart shows it as lost, not invisible.

Transport: JSON-RPC 2.0 over POST /mcp, answering with a single application/json body, which the
Streamable HTTP transport allows in place of an SSE stream. GET /health needs no token, so a container
healthcheck works.
"""
from __future__ import annotations

import json
import calendar
import os
import math
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROTOCOL_VERSION = "2025-03-26"
# Measured 2026-10-29: the self-reported version sat at 0.1.1 while the image was 0.1.9, so a
# client reading serverInfo got a number naming no build. It now comes from ERGON_VERSION, and
# compose derives that from ERGON_TAG so one value tracks the image.
SERVER_INFO = {"name": "ergon", "version": os.environ.get("ERGON_VERSION", "0.0.0-unset")}
TAIL_CAP = 4096
STATE = Path(os.environ.get("ERGON_STATE", "/state"))
WORKSPACE = Path(os.environ.get("ERGON_WORKSPACE", "/workspace"))
TOKEN = os.environ.get("ERGON_TOKEN", "")
ALLOW = {item.strip() for item in os.environ.get("ERGON_ALLOW", "").split(",") if item.strip()}
MAX_JOBS = int(os.environ.get("ERGON_MAX_JOBS", "16"))
LOG_CAP = int(os.environ.get("ERGON_LOG_CAP_MB", "64")) * 1_048_576
REAP_SECONDS = float(os.environ.get("ERGON_REAP_SECONDS", "2.0"))
# A runner is a command tool, not storage: finished job records and their logs are deleted after a
# window, and anything that must outlast it belongs in a store rather than in the state directory.
# The window is enforced by the service, not remembered by an operator, because the disk bound is
# what makes the policy real on a small host.
RETAIN_HOURS = float(os.environ.get("ERGON_RETAIN_HOURS", "72"))
# A time window bounds time, not bytes: a script that starts and finishes 1,000 jobs in one hour
# leaves 1,000 logs all inside the window. The free-space floor is the byte bound that window
# cannot provide, so a start is refused before the state volume is crowded.
MIN_FREE_MB = int(float(os.environ.get("ERGON_MIN_FREE_MB", "512")))
# The ceiling a per-job log_cap_mb request may not exceed. ERGON_LOG_CAP_MB stays the default for
# a job that asks for nothing, so ordinary jobs keep the deployment value while one long build can
# ask for more, bounded by what the deployment sets here and not by what a caller wants.
LOG_CAP_MAX = int(float(os.environ.get("ERGON_LOG_CAP_MAX_MB", "64"))) * 1_048_576
# The floor a prune request may not go under, so a token alone cannot prune younger than the
# retention policy. Measured: `prune_jobs(older_than_hours=1)` is a working wipe of everything the
# service recorded in the last hour, which is why the request is bounded.
PRUNE_MIN_HOURS = float(os.environ.get("ERGON_PRUNE_MIN_HOURS", "72"))
FINISHED_STATES = ("passed", "failed", "killed", "terminated", "capped", "lost")
# The id shape from start_job: 20261029T212249Z-tiny5-a85eac. Matching the shape, not a wildcard,
# is what keeps the sweep off every other file in the state directory.
JOB_FILE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9._-]{1,64}-[0-9a-f]{6}$")
PASS_ENV = [item.strip() for item in
            os.environ.get("ERGON_PASS_ENV", "PATH,HOME,LANG").split(",")]
DENY_ENV = {"LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONSTARTUP", "PATH", "ENV",
            "BASH_ENV", "LD_AUDIT", "DYLD_INSERT_LIBRARIES"}
JOBS: dict = {}
LOCK = threading.Lock()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def job_log(job_id: str) -> Path:
    return STATE / f"{job_id}.log"


def persist(record: dict) -> None:
    path = STATE / f"{record['job_id']}.json"
    path.write_text(json.dumps({key: value for key, value in record.items() if key != "handle"},
                               indent=2) + "\n", encoding="utf-8")
    # 0600: the record stores argv, and a token placed in a command line is at rest in this file.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def restore() -> int:
    """Load sidecars from a previous run. Their processes are gone, so each reads as lost."""
    count = 0
    STATE.mkdir(parents=True, exist_ok=True)
    for path in sorted(STATE.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("state") in ("running", "starting"):
            record["state"] = "lost"
            record["note"] = ("the server restarted while this job ran; the process is no longer "
                              "tracked and its log remains on disk")
        record["handle"] = None
        JOBS[record["job_id"]] = record
        count += 1
    return count


def resolve_cwd(cwd: str | None) -> Path:
    """The workspace directory a job runs in, never outside it.

    Measured by the gap attack: with the workspace unreadable, Path.resolve() raised
    PermissionError out of start_job, which is an interpreter message naming no policy. An
    unusable workspace is a refusal with a reason, so the OSError is turned into one.
    """
    try:
        root = WORKSPACE.resolve()
        target = (root / cwd).resolve() if cwd else root
    except OSError as exc:
        raise ValueError(f"refused: the workspace {WORKSPACE} cannot be read ({exc}); "
                         f"check its ownership and mode before starting jobs")
    if target != root and root not in target.parents:
        raise ValueError(f"cwd escapes the workspace root: {cwd!r} under {root}")
    return target


def _seconds(record: dict) -> float:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        start = time.strptime(record.get("started_at", ""), fmt)
        stop = time.strptime(record.get("ended_at") or now_iso(), fmt)
        return max(0.0, time.mktime(stop) - time.mktime(start))
    except (ValueError, OverflowError):
        return 0.0
def _note_pump_failure(job_id: str, reason: str) -> None:
    """Record why output stopped, so a pump crash is visible in the job, not only in a thread."""
    with LOCK:
        record = JOBS.get(job_id)
        if record is None:
            return
        record["note"] = (record.get("note") or "") + f" pump stopped: {reason}"
    persist(record)


def _pump(job_id: str, process, stream, log_path: Path, cap: int = LOG_CAP) -> None:
    """Copy the child's output to the log and stop the process group at the byte cap.

    A cap checked on a timer tick is not a cap: measured 2026-10-29, a flood job wrote 32,400,819
    bytes to /state and exited 0 before the reaper looked, so the record said passed and the disk
    paid for it. Counting the bytes as they arrive is the only place the bound exists.
    """
    written = 0
    capped = False
    try:
        while True:
            # read1, not read: BufferedReader.read(n) blocks until n bytes arrive, which for a
            # quiet job means its output appears in the log only at exit. Measured on 0.1.2:
            # poll reported an empty last_line and tail_job 0 bytes while the job had printed.
            chunk = stream.read1(65536)
            if not chunk:
                break
            written += len(chunk)
            if not capped and written >= cap:
                capped = True
                # The cap is a disk bound, so it has to stop the writes, not only the process. The
                # pipe can already hold tens of megabytes, and draining it to the log after the stop
                # charges the disk for everything the flood produced. Measured on 0.1.4: with a 1 MB
                # cap the job's log reached 32,401,298 bytes and /state grew to 32 MB, which took the
                # container down mid-probe. Truncate at the cap, then drain and discard.
                with LOCK:
                    record = JOBS.get(job_id)
                if record is not None:
                    with LOCK:
                        record["note"] = (f"output reached the {cap} byte cap "
                                          f"(log_cap_mb={cap // 1048576} for this job, ceiling "
                                          f"{LOG_CAP_MAX // 1048576}); the log was truncated "
                                          f"there and the process group was stopped")
                        record["state"] = "capped"
                    # stop_process waits for the group, so it must not run holding LOCK: a locked
                    # stop serialises the whole server for as long as the job takes to die.
                    state = stop_process(record)
                    with LOCK:
                        record["state"] = state
                    persist(record)
            if capped:
                continue
            with log_path.open("ab") as sink:
                sink.write(chunk)
                if capped:
                    sink.truncate(cap)
                    sink.flush()
    except (OSError, ValueError) as exc:
        _note_pump_failure(job_id, repr(exc))
    except Exception as exc:  # noqa: BLE001 - a pump thread must never take the server with it
        _note_pump_failure(job_id, repr(exc))
    finally:
        try:
            stream.close()
        except OSError:
            pass


def start_job(name: str, argv: list, cwd: str | None = None, env: dict | None = None,
              log_cap_mb=None) -> dict:
    if not isinstance(argv, list) or not argv or not all(isinstance(part, str) for part in argv):
        raise ValueError("argv must be a non-empty list of strings, never a shell string")
    program = argv[0]
    if program not in ALLOW:
        raise ValueError(f"{program!r} is not in ERGON_ALLOW "
                         f"({', '.join(sorted(ALLOW)) or 'the list is empty'}); argv[0] is a bare "
                         f"name resolved from the server's own PATH, never a path you supply")
    resolved = shutil.which(program)
    if not resolved:
        raise ValueError(f"{program!r} is allow-listed but not on the server's PATH")
    try:
        cap_mb = LOG_CAP // 1048576 if log_cap_mb is None else float(log_cap_mb)
    except (TypeError, ValueError):
        raise ValueError(f"refused: log_cap_mb must be a number of megabytes, not {log_cap_mb!r}")
    if not math.isfinite(cap_mb) or cap_mb < 1.0 or cap_mb * 1048576 > LOG_CAP_MAX:
        raise ValueError(f"refused: log_cap_mb must be between 1 and "
                         f"{LOG_CAP_MAX // 1048576} megabytes, not {log_cap_mb!r}")
    cap_bytes = int(cap_mb * 1048576)
    workdir = resolve_cwd(cwd)
    child_env = {key: os.environ[key] for key in PASS_ENV if key in os.environ}
    for key, value in (env or {}).items():
        if not isinstance(value, str):
            raise ValueError(f"env {key!r} must be a string")
        if str(key).upper() in DENY_ENV:
            raise ValueError(f"env {key!r} is refused: it steers how the allow-listed program is "
                             f"found or what it loads, which would move the capability boundary")
        child_env[str(key)] = str(value)
    with LOCK:
        live = sum(1 for record in JOBS.values() if record.get("state") == "running")
        if live >= MAX_JOBS:
            raise ValueError(f"refused: {live} jobs are already running and ERGON_MAX_JOBS is "
                             f"{MAX_JOBS}; poll or kill before starting more")
        # Measured: a first attempt put this inside the max-jobs branch, so the floor only applied
        # once the job count was at its limit. It belongs at the lock level, before any job starts.
        spare = free_devices()
        if any(value is None for value in spare.values()):
            raise ValueError(f"refused: the server cannot read free space on {sorted(spare)}, so "
                             "it will not start a job it cannot measure")
        worst = min(spare.items(), key=lambda pair: pair[1])
        if worst[1] < MIN_FREE_MB:
            raise ValueError(f"refused: {worst[1]} MB free on the {worst[0]} filesystem and "
                             f"ERGON_MIN_FREE_MB is {MIN_FREE_MB}; run prune_jobs, then start "
                             f"again (per-device: {spare})")
        slug = re.sub(r"[^A-Za-z0-9._-]", "-", name or "job")[:48]
        # A random suffix, because a second-resolution id plus a repeated name collided on
        # 2026-10-29 and swallowed six probe calls behind "job id already exists".
        job_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{slug}-{secrets.token_hex(3)}"
        log = job_log(job_id)
        log.write_bytes(b"")
        try:
            os.chmod(log, 0o600)
        except OSError:
            pass
        # Measured by the gap attack: with the workspace unreadable, Popen raised PermissionError
        # out of start_job, an interpreter message naming no policy. The workspace can be present
        # and statable yet unusable, so the OS error becomes a refusal with the reason.
        try:
            process = subprocess.Popen([resolved] + list(argv[1:]), cwd=str(workdir),
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       env=child_env, start_new_session=True)
        except OSError as exc:
            raise ValueError(f"refused: the job could not be started in {workdir} ({exc}); "
                             f"check the workspace mode and ownership")
        record = {"job_id": job_id, "name": name or "job", "argv": list(argv),
                  "program": resolved, "cwd": str(workdir), "state": "running",
                  "pid": process.pid, "pgid": os.getpgid(process.pid), "started_at": now_iso(),
                  "ended_at": None, "exit_code": None, "log": str(log),
                  "log_cap_mb": int(cap_mb), "handle": process}
        JOBS[job_id] = record
    threading.Thread(target=_pump, args=(job_id, process, process.stdout, log, cap_bytes),
                     daemon=True).start()
    persist(record)
    return {key: value for key, value in record.items() if key != "handle"}


def stop_process(record: dict) -> str:
    """Signal the job's whole process group, then the group again with SIGKILL.

    Without a session of its own, terminate() reaches only the direct child, so a job that shells
    out, and above all a `docker run` job, leaves its work running on the host behind a dead CLI.
    """
    handle = record.get("handle")
    if handle is None:
        return record["state"]
    pid, group = record.get("pid"), record.get("pgid")
    target = group if group is not None else pid
    for sig, rounds in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 3.0)):
        try:
            os.killpg(target, sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                (handle.terminate() if sig == signal.SIGTERM else handle.kill())
            except OSError:
                pass
        deadline = time.monotonic() + rounds
        while time.monotonic() < deadline and handle.poll() is None:
            time.sleep(0.2)
        if handle.poll() is not None:
            break
    state = record["state"]
    if handle.poll() is None:
        state = "lost"
    elif state not in ("killed", "terminated", "capped", "lost"):
        state = "terminated" if sig == signal.SIGTERM else "killed"
    record["exit_code"] = handle.poll()
    record["ended_at"] = record.get("ended_at") or now_iso()
    return state


def poll_job(job_id: str) -> dict:
    with LOCK:
        record = JOBS.get(job_id)
    if not record:
        raise ValueError(f"no job {job_id!r}; list_jobs answers for the ids this server knows")
    handle = record.get("handle")
    if handle is not None:
        code = handle.poll()
        if code is not None:
            record["exit_code"] = code
            # A job this server stopped keeps its own terminal cause. Reading 143/-15 as "failed"
            # would rewrite a kill as a crash, which is the difference between an operator act and
            # a defect. Measured in smoke.ps1: poll after kill returned failed exit=-15.
            if record["state"] not in ("killed", "terminated", "capped", "lost"):
                record["state"] = "passed" if code == 0 else "failed"
            record["ended_at"] = record.get("ended_at") or now_iso()
            handle.wait()
            persist(record)
    public = {key: value for key, value in record.items() if key != "handle"}
    public["seconds"] = round(_seconds(record), 1)
    public["last_line"] = last_line(job_id)
    return public


def last_line(job_id: str) -> str:
    path = job_log(job_id)
    if not path.is_file():
        return ""
    lines = [line for line in path.read_bytes()[-4096:].decode("utf-8", "replace").splitlines()
             if line.strip()]
    return lines[-1][:300] if lines else ""


def tail_job(job_id: str, limit: int = 2048) -> dict:
    with LOCK:
        record = JOBS.get(job_id)
    if not record:
        raise ValueError(f"no job {job_id!r}")
    path = job_log(job_id)
    limit = max(1, min(int(limit), TAIL_CAP))
    if not path.is_file():
        return {"job_id": job_id, "bytes": 0, "text": "", "note": "no log written yet"}
    data = path.read_bytes()[-limit:]
    return {"job_id": job_id, "bytes": len(data), "text": data.decode("utf-8", "replace")}


def kill_job(job_id: str) -> dict:
    with LOCK:
        record = JOBS.get(job_id)
    if not record:
        raise ValueError(f"no job {job_id!r}")
    record["state"] = stop_process(record)
    persist(record)
    return {key: value for key, value in record.items() if key != "handle"}


def reap_adopted() -> int:
    """Reap orphaned grandchildren reparented to PID 1, which is this process.

    A killed job's spawned child is reparented to the server. Popen never knew it, so nothing reaps
    it, and it stays a zombie for the container's whole life: measured 2026-10-29 on 0.1.5, a group
    kill left the child as a zombie and a later /proc scan counted it as a live `sleep 300`.
    Reaping by explicit pid, never waitpid(-1), because a blanket reap steals the exit status that
    Popen.poll() needs for a tracked child.
    """
    mine = os.getpid()
    tracked = set()
    with LOCK:
        for record in JOBS.values():
            handle = record.get("handle")
            if handle is not None:
                tracked.add(handle.pid)
    swept = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in tracked or pid == mine:
            continue
        try:
            with open(f"/proc/{pid}/stat") as stream:
                stat = stream.read()
            state = stat[stat.rfind(")") + 1:].split()[0]
            with open(f"/proc/{pid}/stat") as stream:
                fields = stream.read().split()
            ppid = int(fields[3])
        except (OSError, IndexError, ValueError):
            continue
        if ppid == mine and state == "Z":
            try:
                os.waitpid(pid, os.WNOHANG)
                swept += 1
            except ChildProcessError:
                continue
            except OSError:
                continue
    return swept


def reap_once() -> int:
    """Reap finished jobs and enforce the log cap, so an unpolled job cannot pile up.

    Measured on 2026-10-29: 8 short jobs that were never polled left 39 zombie processes in the
    container, because reaping happened only inside poll_job. A service that exists to be polled
    occasionally must not depend on being polled to stay clean.
    """
    reaped = 0
    with LOCK:
        snapshot = [record for record in JOBS.values() if record.get("handle") is not None]
    for record in snapshot:
        handle = record["handle"]
        code = handle.poll()
        if code is not None:
            with LOCK:
                record["exit_code"] = code
                if record["state"] not in ("killed", "terminated", "capped", "lost"):
                    record["state"] = "passed" if code == 0 else "failed"
                record["ended_at"] = record.get("ended_at") or now_iso()
            persist(record)
            reaped += 1
            continue
    return reaped + reap_adopted()


def free_mb(path=None) -> int | None:
    """Megabytes free on the filesystem holding `path` (default: job state), or None."""
    try:
        return shutil.disk_usage(str(path if path is not None else STATE)).free // (1024 * 1024)
    except OSError:
        return None


def free_devices() -> dict:
    """Free megabytes for every filesystem a job can write to.

    Measured 2026-10-29: `free_mb()` read the state volume only, while jobs also write to the
    workspace, and a deployment is free to put those on different devices. The floor has to cover
    each of them, so the guard asks about all of them and refuses on the worst.
    """
    return {label: free_mb(base) for label, base in (("state", STATE), ("workspace", WORKSPACE))}


def clock_epoch(text):
    """A record's clock as a unix timestamp, or None when it cannot be read as the server writes it.

    Measured 2026-10-29: the sweep compared `ended_at` to the cutoff as text, so an empty `ended_at`
    on a four-second-old record sorted before the cutoff and the record was deleted, and a
    space-separated clock on the cutoff day sorted before the cutoff's 'T' for the same reason. Text
    ordering is only safe on the exact shape the server writes, so parse it and judge nothing that
    will not parse.
    """
    if not isinstance(text, str):
        return None
    try:
        return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, OverflowError, TypeError):
        return None


def sweep_state(older_than_hours=None, dry_run: bool = False) -> dict:
    """Delete, or report, finished job records and logs past a window, driven by files on disk.

    One code path serves the reaper's automatic sweep and the prune_jobs tool, so what a dry run
    reports is what the real run removes. Disk-driven, not index-driven: a restart restores the
    index, so the files are the durable truth, and a check that backdates a file and watches it
    disappear can run against a live service. Only job-id-shaped names are considered, which is what
    keeps the sweep off every other file in the directory.
    """
    try:
        hours = RETAIN_HOURS if older_than_hours is None else float(older_than_hours)
    except (TypeError, ValueError):
        # Measured: a string argument reached float() and answered with the interpreter's own
        # "could not convert string to float" text, which names no policy.
        raise ValueError(f"refused: older_than_hours must be a number of hours, not "
                         f"{older_than_hours!r}")
    if not math.isfinite(hours) or hours < 1.0:
        raise ValueError(f"refused: the retention window must be a finite number of hours of at "
                         f"least 1, not {older_than_hours!r}")
    if hours < PRUNE_MIN_HOURS:
        raise ValueError(f"refused: the window {hours:g}h is under the governed floor "
                         f"ERGON_PRUNE_MIN_HOURS={PRUNE_MIN_HOURS:g}h")
    cutoff = time.time() - hours * 3600
    if cutoff < 0:
        # Measured: 1e308 hours produced a cutoff of -infinity, deleted nothing, and reported
        # "cutoff: unrepresentable". Deleting nothing is the safe direction, and the sweep is not
        # the place for a clock that cannot be named, so the request is refused with a bound.
        raise ValueError(f"refused: the window {hours:g}h puts the cutoff before the server clock "
                         f"can name it; the practical ceiling is about "
                         f"{int(time.time() // 3600):,} hours")
    cutoff_text = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff))
    victims, freed, files, undatable = [], 0, 0, 0
    for path in sorted(STATE.glob("*.json")):
        stem = path.name[:-5]
        if not JOB_FILE.match(stem) or path.is_symlink():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if record.get("state") not in FINISHED_STATES:
            continue
        ended = clock_epoch(record.get("ended_at"))
        if ended is None:
            undatable += 1
            continue
        if ended >= cutoff:
            continue
        targets = [item for item in (path, job_log(stem))
                   if item.is_file() and not item.is_symlink()]
        size = sum(item.stat().st_size for item in targets)
        victims.append((stem, targets))
        freed += size
        files += len(targets)
    if not dry_run:
        for stem, targets in victims:
            with LOCK:
                JOBS.pop(stem, None)
            for target in targets:
                try:
                    target.unlink()
                except OSError:
                    pass
    return {"cutoff": cutoff_text, "older_than_hours": hours, "records": len(victims),
            "files": files, "bytes_freed": freed, "dry_run": dry_run,
            "kept_undatable": undatable, "prune_min_hours": PRUNE_MIN_HOURS,
            "free_mb_after": free_mb()}


def prune_once() -> int:
    """The automatic sweep: the reaper calls it every tick and main() calls it at startup."""
    result = sweep_state()
    if result["files"]:
        sys.stderr.write(f"{now_iso()} retention: removed {result['files']} file(s), "
                         f"{result['bytes_freed']} bytes, for jobs ended before "
                         f"{result['cutoff']} (ERGON_RETAIN_HOURS={RETAIN_HOURS:g})\n")
    return result["records"]


def reaper(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            reap_once()
            prune_once()
        except Exception as exc:
            sys.stderr.write(f"{now_iso()} reaper fault: {type(exc).__name__}: {exc}\n")
        stop.wait(REAP_SECONDS)


def list_jobs(state: str | None = None) -> list:
    with LOCK:
        records = [dict(record) for record in JOBS.values()]
    out = [{key: value for key, value in record.items() if key != "handle"} for record in records]
    out.sort(key=lambda item: item.get("started_at", ""), reverse=True)
    if state:
        out = [item for item in out if item.get("state") == state]
    return out


def running_count() -> int:
    with LOCK:
        return sum(1 for record in JOBS.values() if record.get("state") == "running")

import sys  # needed by main(), kept with the HTTP layer it serves


TOOLS = [
    {"name": "start_job",
     "description": "Start a long-running job and return its id immediately. argv is a list, never a "
                    "shell string, and argv[0] must be in the server's ERGON_ALLOW list. The job "
                    "keeps running after this call returns; poll it by id.",
     "inputSchema": {"type": "object",
                     "properties": {"name": {"type": "string"},
                                    "argv": {"type": "array", "items": {"type": "string"},
                                             "minItems": 1},
                                    "cwd": {"type": "string",
                                            "description": "path inside the workspace root"},
                                    "env": {"type": "object",
                                            "additionalProperties": {"type": "string"}},
                                    "log_cap_mb": {"type": "number", "minimum": 1,
                                                   "description": "Output bound for this job, in megabytes. Default ERGON_LOG_CAP_MB; the ceiling is ERGON_LOG_CAP_MAX_MB. Reaching it stops the job's process group and marks the record capped."}},
                     "required": ["argv"]}},
    {"name": "poll_job",
     "description": "State, exit code, elapsed seconds and the log's last line for one job id.",
     "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}},
                     "required": ["job_id"]}},
    {"name": "tail_job", "description": "The end of a job's log, capped at 4096 bytes.",
     "inputSchema": {"type": "object",
                     "properties": {"job_id": {"type": "string"},
                                    "limit": {"type": "integer", "minimum": 1, "maximum": 4096}},
                     "required": ["job_id"]}},
    {"name": "list_jobs",
     "description": "Every job this server knows, newest first. Optional state filter: running, "
                    "passed, failed, killed, terminated, lost.",
     "inputSchema": {"type": "object", "properties": {"state": {"type": "string"}}}},
    {"name": "kill_job", "description": "Terminate a job, then kill it after five seconds.",
     "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}},
                     "required": ["job_id"]}},
    {"name": "prune_jobs",
     "description": "Delete finished job records and logs older than a window in hours (default "
                    "ERGON_RETAIN_HOURS) and report the counts and bytes. dry_run=true reports "
                    "and deletes nothing. Running jobs and files outside the job-id shape are "
                    "never touched: Ergon is a command tool, not storage.",
     "inputSchema": {"type": "object",
                     "properties": {"older_than_hours": {"type": "number", "minimum": 1},
                                    "dry_run": {"type": "boolean", "default": False}}}},
]


REQUIRED_ID = {"poll_job", "tail_job", "kill_job"}


def call_tool(name: str, args: dict):
    if name in REQUIRED_ID and "job_id" not in args:
        # Measured: args["job_id"] raised KeyError and the refusal read "KeyError: 'job_id'",
        # interpreter text that names no policy. Required arguments are checked in policy words.
        raise ValueError(f"refused: {name} requires job_id")
    if name == "start_job":
        return start_job(args.get("name", "job"), args.get("argv") or [], args.get("cwd"),
                         args.get("env"), args.get("log_cap_mb"))
    if name == "poll_job":
        return poll_job(args["job_id"])
    if name == "tail_job":
        return tail_job(args["job_id"], args.get("limit", 2048))
    if name == "list_jobs":
        jobs = list_jobs(args.get("state"))
        return {"count": len(jobs), "jobs": jobs}
    if name == "prune_jobs":
        return sweep_state(args.get("older_than_hours"), bool(args.get("dry_run", False)))
    if name == "kill_job":
        return kill_job(args["job_id"])
    raise ValueError(f"unknown tool {name!r}")


def rpc(request: dict):
    if "id" not in request:
        return None
    rid, method = request["id"], request.get("method")
    params = request.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                           "serverInfo": SERVER_INFO}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        try:
            body = json.dumps(call_tool(params.get("name", ""), params.get("arguments") or {}))
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text", "text": body}], "isError": False}}
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text",
                                            "text": f"{type(exc).__name__}: {exc}"}],
                               "isError": True}}
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}

def _reject_constant(name: str) -> float:
    """Refuse NaN, Infinity and -Infinity, which Python accepts and JSON does not define."""
    raise ValueError(f"{name} is not a valid JSON number")


class Handler(BaseHTTPRequestHandler):
    server_version = "ergon/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{now_iso()} {self.address_string()} {fmt % args}\n")

    def _send(self, code: int, payload: str):
        data = payload.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            self._send(200, json.dumps({"ok": True, "jobs_running": running_count(),
                                        "at": now_iso(),
                                        "retention_hours": RETAIN_HOURS,
                                        "min_free_mb": MIN_FREE_MB,
                                        "free_mb": free_mb(),
                                        "free_by_device": free_devices(),
                                        "log_cap_mb": LOG_CAP // 1048576,
                                        "log_cap_max_mb": LOG_CAP_MAX // 1048576,
                                        "max_jobs": MAX_JOBS,
                                        "version": SERVER_INFO["version"]}))
        else:
            self._send(405, json.dumps({"error": "this server answers POST /mcp and GET /health"}))

    def do_POST(self):
        if self.path.split("?")[0] != "/mcp":
            self._send(404, json.dumps({"error": "no such endpoint"}))
            return
        supplied = self.headers.get("Authorization", "")
        if not supplied.startswith("Bearer ") or not secrets.compare_digest(
                supplied[7:].strip(), TOKEN):
            self._send(401, json.dumps({"error": "unauthorized"}))
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > 65536:
            self._send(413, json.dumps({"error": "request body over 65536 bytes"}))
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8", "replace"),
                                 parse_constant=_reject_constant)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            self._send(400, json.dumps({"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32700,
                                                  "message": f"unreadable request body: {exc}"}}))
            return
        if not isinstance(payload, (dict, list)):
            self._send(400, json.dumps({"error": f"request body is {type(payload).__name__}, "
                                                 "not a JSON-RPC object or batch"}))
            return
        if isinstance(payload, list):
            answers = [rpc(item) if isinstance(item, dict) else
                       {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32600,
                                  "message": f"batch element is {type(item).__name__}, "
                                             f"not an object"}}
                       for item in payload]
            self._send(200, json.dumps([a for a in answers if a]))
            return
        answer = rpc(payload)
        if answer is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send(200, json.dumps(answer))


def main() -> int:
    if not TOKEN:
        print("refusing to start: ERGON_TOKEN is empty, so the server would be open",
              file=sys.stderr)
        return 2
    if not ALLOW:
        print("refusing to start: ERGON_ALLOW is empty, so no job could ever be allowed",
              file=sys.stderr)
        return 2
    if not (RETAIN_HOURS >= 1.0):
        # The comparison form rejects NaN, a lesson measured in the watcher audit: a NaN window
        # passed the floor test and expired at once. A retention of 0 would delete every record.
        print("refusing to start: ERGON_RETAIN_HOURS must be at least 1 hour", file=sys.stderr)
        return 2
    if not (MIN_FREE_MB >= 0):
        print("refusing to start: ERGON_MIN_FREE_MB must be 0 or more", file=sys.stderr)
        return 2
    if LOG_CAP_MAX < LOG_CAP:
        # A ceiling below the default would mean the deployment ships jobs that are already
        # over the bound they are allowed, so the pair is refused rather than guessed at.
        print(f"refusing to start: ERGON_LOG_CAP_MB={LOG_CAP // 1048576} exceeds "
              f"ERGON_LOG_CAP_MAX_MB={LOG_CAP_MAX // 1048576}", file=sys.stderr)
        return 2
    if not math.isfinite(PRUNE_MIN_HOURS) or PRUNE_MIN_HOURS < 1.0:
        print("refusing to start: ERGON_PRUNE_MIN_HOURS must be a finite number of hours of at "
              "least 1", file=sys.stderr)
        return 2
    STATE.mkdir(parents=True, exist_ok=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    loaded = restore()
    pruned = prune_once()
    port = int(os.environ.get("ERGON_PORT", "8000"))
    stop = threading.Event()
    threading.Thread(target=reaper, args=(stop,), daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"ergon listening on :{port}; allow={sorted(ALLOW)}; state={STATE}; "
          f"restored={loaded} job record(s); max_jobs={MAX_JOBS}; "
          f"log_cap={LOG_CAP // 1048576}MB; reap={REAP_SECONDS}s; "
          f"retention={RETAIN_HOURS:g}h; pruned_at_start={pruned}; "
          f"min_free={MIN_FREE_MB}MB; free={free_mb()}MB", flush=True)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
