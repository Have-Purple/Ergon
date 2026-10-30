#!/usr/bin/env python3
r"""Smoke test for a running Ergon deployment, standard library only.

Checks health, the bearer token, the MCP handshake, the six advertised tools, one real job from start
to its logged output, and the refusals that carry the security posture. It starts one short-lived job
and deletes nothing.

Run:  ERGON_URL=http://127.0.0.1:8000 ERGON_TOKEN=<token> python3 smoke.py
  or: python3 smoke.py --url http://127.0.0.1:8000 --token <token>
Exit: 0 when every check passed, 1 when any check failed, 2 on a usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

TOOLS = ("start_job", "poll_job", "tail_job", "list_jobs", "kill_job", "prune_jobs")
passed = 0
failed: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed
    if ok:
        passed += 1
        print(f"PASS {label}")
    else:
        failed.append(label)
        print(f"FAIL {label}{': ' + detail[:300] if detail else ''}")


def post(url: str, token: str, payload: dict):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url.rstrip("/") + "/mcp", data=data, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(request, timeout=20) as answer:
            return answer.status, json.loads(answer.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def call(url: str, token: str, rid: int, tool: str, arguments: dict):
    _, answer = post(url, token, {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                                  "params": {"name": tool, "arguments": arguments}})
    result = answer["result"]
    return result, result["content"][0]["text"]


def main() -> int:
    parser = argparse.ArgumentParser(prog="smoke")
    parser.add_argument("--url", default=os.environ.get("ERGON_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.environ.get("ERGON_TOKEN", ""))
    args = parser.parse_args()
    if not args.token:
        print("usage error: no token. Set ERGON_TOKEN or pass --token.", file=sys.stderr)
        return 2
    url = args.url

    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=10) as answer:
            health = json.loads(answer.read().decode("utf-8", "replace"))
        check("health answers without a token", health.get("ok") is True, json.dumps(health))
    except Exception as exc:
        print(f"health endpoint unreachable at {url}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    status, _ = post(url, "", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    check("a request with no bearer token is refused with 401", status == 401, f"got HTTP {status}")

    _, init = post(url, args.token, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                     "params": {"protocolVersion": "2025-03-26",
                                                "capabilities": {},
                                                "clientInfo": {"name": "smoke",
                                                               "version": "0"}}})
    check("initialize returns a protocol version and the server name",
          "protocolVersion" in init.get("result", {})
          and init["result"].get("serverInfo", {}).get("name") == "ergon", json.dumps(init)[:200])

    _, tools = post(url, args.token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                                      "params": {}})
    names = [tool["name"] for tool in tools.get("result", {}).get("tools", [])]
    check("tools/list advertises exactly the six tools", sorted(names) == sorted(TOOLS),
          json.dumps(names))

    result, text = call(url, args.token, 3, "start_job",
                        {"name": "smoke", "argv": ["python3", "-c", 'print("smoke-ok")']})
    job_id = ""
    try:
        job_id = json.loads(text).get("job_id", "")
    except (ValueError, KeyError):
        pass
    check("start_job returned a job id", bool(job_id), text)

    state, code = "unknown", None
    for _ in range(12):
        if result.get("isError"):
            break
        _, text = call(url, args.token, 4, "poll_job", {"job_id": job_id})
        record = json.loads(text)
        state, code = record.get("state", ""), record.get("exit_code")
        if state in ("passed", "failed", "capped", "killed", "terminated", "lost"):
            break
        time.sleep(1)
    check("the job reached passed with exit 0", state == "passed" and code == 0,
          f"state={state} exit={code}")

    _, text = call(url, args.token, 5, "tail_job", {"job_id": job_id, "limit": 512})
    check("the job log carries the program's own output", "smoke-ok" in text, text[:200])

    _, text = call(url, args.token, 6, "list_jobs", {})
    check("list_jobs reports the job it started", job_id in text, text[:200])

    _, text = call(url, args.token, 7, "start_job",
                   {"argv": ["sh", "-c", "echo not-reachable"]})
    check("a program off the allow-list is refused", "ERGON_ALLOW" in text, text[:200])

    _, text = call(url, args.token, 8, "start_job",
                   {"argv": ["python3", "-c", "print(1)"], "cwd": "../etc"})
    check("a cwd that escapes the workspace is refused", "workspace" in text, text[:200])

    _, text = call(url, args.token, 9, "start_job", {"argv": "python3 -c print(1)"})
    check("an argv given as a shell string is refused", "shell string" in text, text[:200])

    print(f"\n{passed} checks passed, {len(failed)} failed")
    if failed:
        print("FAILED: " + "; ".join(failed))
        return 1
    print("ALL SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

