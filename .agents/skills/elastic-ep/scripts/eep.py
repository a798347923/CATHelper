#!/usr/bin/env python3
"""Elastic-EP agent-side forensic and execution helper (stdlib-only).

Subcommands:
  status   Probe fault-tolerance capability and engine status; emit verdict.
  apply    Gate-check (capability -> rank states -> instruction) then dispatch
           retry / scale_down.
  launch   Validate fault-tolerant launch parameters; emit a launch task spec.

Transport (choose one, per subcommand):
  --tool-url URL   Submit/poll through the CAT tool-layer task envelope
                   (default; brain outside the cluster, hands inside).
  --direct         Bypass the tool layer and call the vLLM REST API directly.
                   Debugging only -- violates the brain-outside-cluster rule.
  --json FILE|-    Pure local forensics on a saved status payload, no network
                   (status: verdict only; apply: gate-only dry-run).

Exit codes:
  0  affirmative verdict (FT_ENABLED / SENT / LAUNCH_SPEC_VALID / LAUNCHED)
  1  transport or tool-layer error
  2  usage error (bad/missing arguments)
  3  decision-gate rejection (precondition not met: healthy ranks present,
     dead+retry, launch hard-constraint violation)
  4  capability absent (GET /fault_tolerance/status -> 404)
  5  unknown state (vLLM service unreachable; unreachable != disabled)
  6  instruction execution failed at the server (apply non-200)

See references/decision-rules.md for the decision tables implemented here.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

EXIT_OK = 0
EXIT_TRANSPORT = 1
EXIT_USAGE = 2
EXIT_GATE = 3
EXIT_DISABLED = 4
EXIT_UNKNOWN = 5
EXIT_EXEC_FAILED = 6

KNOWN_STATES = ("healthy", "unhealthy", "paused", "dead")


class EepExit(Exception):
    """Raised to terminate with a specific exit code and a printed message."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def warn(msg):
    sys.stderr.write("WARNING: %s\n" % msg)


# Tool-layer and vLLM endpoints are always explicit in-cluster hosts; never
# route them through a system/HTTP proxy (a corporate proxy answering 504 on
# localhost would break every probe and dispatch).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(url, method="GET", payload=None, timeout=15.0):
    """Return (status, raw_body, parsed_json_or_None). Non-2xx -> HTTPError caught."""
    req = urllib.request.Request(url, method=method)
    headers = {"Accept": "application/json"}
    if payload is not None:
        req.data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, raw, _safe_json(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return e.code, raw, _safe_json(raw)


def _safe_json(raw):
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def run_task(tool_url, tool, args, poll_interval=2.0, task_timeout=60.0):
    """Submit a task through the CAT tool-layer envelope and poll to a terminal
    state. Returns the final task body {"state", "result", "error"}."""
    base = tool_url.rstrip("/")
    try:
        status, raw, body = http_json(
            base + "/api/tasks", "POST", {"tool": tool, "args": args}, timeout=15.0
        )
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise EepExit(EXIT_TRANSPORT, "task submit to %s failed: %s" % (base, e))
    if status not in (200, 202):
        raise EepExit(EXIT_TRANSPORT, "task submit failed: HTTP %s %s" % (status, raw))
    if not isinstance(body, dict) or not body.get("task_id"):
        raise EepExit(EXIT_TRANSPORT, "task submit response missing task_id: %s" % raw)
    task_id = body["task_id"]
    deadline = time.monotonic() + task_timeout
    while True:
        if time.monotonic() > deadline:
            raise EepExit(
                EXIT_TRANSPORT, "task %s poll timed out after %ss" % (task_id, task_timeout)
            )
        time.sleep(poll_interval)
        try:
            status, raw, body = http_json(
                base + "/api/tasks/" + str(task_id), timeout=15.0
            )
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise EepExit(EXIT_TRANSPORT, "task poll failed: %s" % e)
        if not isinstance(body, dict):
            raise EepExit(EXIT_TRANSPORT, "task poll returned invalid body: %s" % raw)
        if body.get("state") in ("SUCCESS", "FAILED", "REJECTED"):
            return body


# ---------------------------------------------------------------------------
# status acquisition
# ---------------------------------------------------------------------------


def status_from_payload(payload):
    """Normalize a status payload (tool result or raw status body) into
    (capability, status_dict|None)."""
    if isinstance(payload, dict) and "capability" in payload:
        return payload.get("capability"), payload.get("status")
    if isinstance(payload, dict) and "engines" in payload:
        return "enabled", payload
    return None, None


def fetch_status_tool(tool_url, host, port, timeout_sec):
    task = run_task(
        tool_url,
        "vllm_ft_status",
        {"host": host, "port": port, "timeout_sec": timeout_sec},
    )
    state = task.get("state")
    if state == "SUCCESS":
        result = task.get("result") or {}
        return result.get("capability"), result.get("status"), None
    if state == "FAILED":
        err = task.get("error") or {}
        if err.get("code") in ("TRANSPORT_ERROR", "TIMEOUT"):
            return "unknown", None, "tool layer reports vLLM unreachable: %s" % err.get("message")
        raise EepExit(EXIT_TRANSPORT, "tool vllm_ft_status FAILED: %s" % json.dumps(task))
    raise EepExit(EXIT_USAGE, "tool vllm_ft_status REJECTED: %s" % json.dumps(task))


def fetch_status_direct(host, port, timeout_sec):
    url = "http://%s:%s/fault_tolerance/status" % (host, port)
    try:
        status, raw, body = http_json(url, timeout=timeout_sec)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return "unknown", None, "GET %s -> %s" % (url, e)
    if status == 200:
        if not isinstance(body, dict):
            raise EepExit(EXIT_TRANSPORT, "status returned non-JSON body: %s" % raw)
        return "enabled", body, None
    if status == 404:
        return "disabled", None, "GET %s -> 404" % url
    return "unknown", None, "GET %s -> HTTP %s %s" % (url, status, raw)


def fetch_status(args):
    if args.json is not None:
        if args.json == "-":
            raw = sys.stdin.read()
        else:
            with open(args.json, encoding="utf-8") as fh:
                raw = fh.read()
        payload = _safe_json(raw)
        if payload is None:
            raise EepExit(EXIT_USAGE, "--json input is not valid JSON")
        capability, status = status_from_payload(payload)
        if capability is None:
            raise EepExit(
                EXIT_USAGE,
                "--json payload must be a tool result "
                '({"capability":..., "status":...}) or a raw status body '
                '({"total_engines":..., "engines":[...]})',
            )
        return capability, status, None
    if args.direct:
        warn("--direct bypasses the tool layer (brain-in-cluster). Debugging only.")
        return fetch_status_direct(args.host, args.port, args.timeout)
    if args.tool_url:
        return fetch_status_tool(args.tool_url, args.host, args.port, args.timeout)
    raise EepExit(EXIT_USAGE, "need one of --tool-url / --direct / --json")


def analyze(status):
    engines = status.get("engines", []) if isinstance(status, dict) else []
    if not isinstance(engines, list):
        engines = []
    counts = {s: 0 for s in KNOWN_STATES}
    counts["other"] = 0
    lines = []
    for e in engines:
        if isinstance(e, dict):
            eid = e.get("id")
            st = e.get("status")
        else:
            eid, st = None, None
        if not isinstance(st, str) or st not in KNOWN_STATES:
            st = "other"
        counts[st] += 1
        lines.append("  [%s] %s" % (eid, st))
    total = (
        status.get("total_engines", len(engines))
        if isinstance(status, dict)
        else len(engines)
    )
    return total, counts, lines


def _is_int(value):
    """Strict integer check: bool is not an int here (JSON true/false)."""
    return isinstance(value, int) and not isinstance(value, bool)


def status_problems(status):
    """Validate the internal consistency of a status payload.

    Returns a list of human-readable anomalies; an empty list means the data
    is consistent (a 0-engine status is consistent, not an anomaly).
    """
    problems = []
    if not isinstance(status, dict):
        return ["status payload is not a JSON object"]
    total = status.get("total_engines")
    if not _is_int(total) or total < 0:
        problems.append("missing/invalid total_engines (%r)" % (total,))
        total = None
    engines = status.get("engines")
    if not isinstance(engines, list):
        problems.append("missing/invalid engines list")
        return problems
    if total is not None and len(engines) != total:
        problems.append(
            "total_engines=%s but engines list has %s entries" % (total, len(engines))
        )
    seen = set()
    for e in engines:
        if not isinstance(e, dict):
            problems.append("engine entry is not a JSON object: %r" % (e,))
            continue
        eid = e.get("id")
        st = e.get("status")
        if not _is_int(eid) or eid < 0:
            problems.append("engine with invalid id: %r" % (eid,))
        elif total is not None and eid >= total:
            problems.append("engine id %s out of range [0, %s)" % (eid, total))
        elif eid in seen:
            problems.append("duplicate engine id %s" % eid)
        if _is_int(eid):
            seen.add(eid)
        if not isinstance(st, str) or st not in KNOWN_STATES:
            problems.append("engine id %s has unknown status %r" % (eid, st))
    return problems


def print_engine_lines(lines):
    for line in lines:
        print(line)


def verdict_status(capability, status, evidence):
    if capability == "disabled":
        print("FT_DISABLED | GET /fault_tolerance/status -> 404 (fault tolerance not enabled)")
        if evidence:
            print("  evidence: %s" % evidence)
        return EXIT_DISABLED
    if capability == "unknown":
        print("FT_UNKNOWN | vLLM service unreachable (unreachable != disabled)")
        if evidence:
            print("  evidence: %s" % evidence)
        return EXIT_UNKNOWN
    if capability != "enabled":
        print("FT_UNKNOWN | unexpected capability value: %r" % capability)
        return EXIT_UNKNOWN
    if not isinstance(status, dict):
        print(
            "FT_ENABLED STATUS_MISSING | tool returned capability=enabled without "
            "a status payload; fix the tool layer / vLLM response"
        )
        return EXIT_TRANSPORT
    problems = status_problems(status)
    if problems:
        print("FT_ENABLED STATUS_INCONSISTENT | %s" % "; ".join(problems))
        _, _, lines = analyze(status)
        print_engine_lines(lines)
        return EXIT_TRANSPORT
    total, counts, lines = analyze(status)
    healthy = counts["healthy"]
    others = (
        counts["paused"] + counts["unhealthy"] + counts["dead"] + counts["other"]
    )
    if healthy > 0 and others > 0:
        state_word = "WAIT_PAUSE"
    elif healthy > 0:
        state_word = "NO_FAULT"
    elif others > 0:
        state_word = "READY"
    else:
        state_word = "NO_FAULT"
    print(
        "FT_ENABLED %s | total_engines=%s healthy=%s paused=%s unhealthy=%s dead=%s"
        % (
            state_word,
            total,
            healthy,
            counts["paused"],
            counts["unhealthy"],
            counts["dead"],
        )
    )
    print_engine_lines(lines)
    if state_word == "NO_FAULT" and total < 1:
        print(
            "  hint: status reports 0 engines -- verify the status data before "
            "drawing conclusions"
        )
    if state_word == "WAIT_PAUSE":
        print(
            "  hint: healthy ranks present; the fault-tolerance framework auto-issues"
            " pause -- poll status until no healthy rank remains, then dispatch"
        )
    return EXIT_OK


# ---------------------------------------------------------------------------
# apply gate + dispatch
# ---------------------------------------------------------------------------


def apply_gate(capability, status, instruction, exclude):
    """Return None on pass, or (exit_code, message) on rejection."""
    if capability == "disabled":
        return EXIT_DISABLED, "fault tolerance not enabled (404); nothing to dispatch"
    if capability == "unknown":
        return EXIT_UNKNOWN, "vLLM service unreachable; fix connectivity before dispatching"
    if capability != "enabled":
        return (
            EXIT_UNKNOWN,
            "unexpected capability value %r; verify the tool layer response" % capability,
        )
    problems = status_problems(status)
    if problems:
        return EXIT_GATE, "status data inconsistent: %s" % "; ".join(problems)
    total, _, _ = analyze(status)
    healthy = [
        e.get("id") for e in status.get("engines", []) if e.get("status") == "healthy"
    ]
    dead = [e.get("id") for e in status.get("engines", []) if e.get("status") == "dead"]
    if total < 1:
        return (
            EXIT_GATE,
            "no fault evidence: status reports 0 engines (empty engine list); "
            "nothing to dispatch -- verify the status data",
        )
    if healthy:
        if len(healthy) == total and total > 0:
            return (
                EXIT_GATE,
                "no fault evidence: all %s ranks healthy; nothing to recover -- "
                "verify the user's intent" % total,
            )
        return (
            EXIT_GATE,
            "WAIT_PAUSE: healthy ranks %s present; framework auto-pause in progress, "
            "poll status and retry when none healthy" % healthy,
        )
    if instruction == "retry":
        if dead:
            return (
                EXIT_GATE,
                "dead ranks %s present; retry will fail with 'Engine dead' -- "
                "use scale_down with exclude_dp_ranks covering all dead ranks" % dead,
            )
        return None
    if instruction == "scale_down":
        if not exclude:
            return EXIT_USAGE, "scale_down requires non-empty --exclude-dp-ranks"
        out_of_range = sorted(set(exclude) - set(range(total)))
        if out_of_range:
            return (
                EXIT_GATE,
                "exclude_dp_ranks %s out of range [0, %s) -- use the latest status "
                "(ranks are renumbered after scale_down)" % (out_of_range, total),
            )
        missing_dead = sorted(set(dead) - set(exclude))
        if missing_dead:
            return (
                EXIT_GATE,
                "exclude_dp_ranks missing dead ranks %s -- dead ranks must all be excluded"
                % missing_dead,
            )
        remaining = total - len(set(exclude))
        if remaining < 1:
            return EXIT_GATE, "all %s ranks excluded; nothing left to serve" % total
        return None
    return EXIT_USAGE, "unknown instruction %r" % instruction


def dispatch_tool(args, instruction, params):
    task = run_task(
        args.tool_url,
        "vllm_ft_apply",
        {
            "host": args.host,
            "port": args.port,
            "instruction": instruction,
            "params": params,
            "timeout_sec": args.task_timeout,
        },
        task_timeout=args.task_timeout,
    )
    state = task.get("state")
    if state == "SUCCESS":
        result = task.get("result") or {}
        return result.get("http_status"), result.get("body")
    if state == "FAILED":
        err = task.get("error") or {}
        if err.get("code") in ("TRANSPORT_ERROR", "TIMEOUT"):
            raise EepExit(
                EXIT_TRANSPORT,
                "tool reports vLLM unreachable: %s" % err.get("message"),
            )
        raise EepExit(
            EXIT_EXEC_FAILED, "tool vllm_ft_apply FAILED: %s" % json.dumps(task)
        )
    raise EepExit(EXIT_GATE, "tool vllm_ft_apply REJECTED: %s" % json.dumps(task))


def dispatch_direct(args, instruction, params):
    url = "http://%s:%s/fault_tolerance/apply" % (args.host, args.port)
    payload = {"instruction": instruction, "params": params}
    try:
        status, raw, body = http_json(url, "POST", payload, timeout=args.task_timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise EepExit(EXIT_TRANSPORT, "POST %s -> %s" % (url, e))
    return status, (body if body is not None else raw)


def cmd_status(args):
    capability, status, evidence = fetch_status(args)
    return verdict_status(capability, status, evidence)


def cmd_apply(args):
    instruction = args.instruction
    exclude = sorted(set(args.exclude_dp_ranks or []))
    params = {"timeout": args.timeout}
    if instruction == "scale_down":
        params["exclude_dp_ranks"] = exclude

    capability, status, evidence = fetch_status(args)
    if capability == "disabled":
        print("gate: capability check failed (fault tolerance not enabled)")
        if evidence:
            print("  evidence: %s" % evidence)
        return EXIT_DISABLED
    if capability == "unknown":
        print("gate: capability check failed (vLLM unreachable)")
        if evidence:
            print("  evidence: %s" % evidence)
        return EXIT_UNKNOWN
    if capability != "enabled":
        print(
            "REJECTED | unexpected capability value %r; verify the tool layer "
            "response" % capability
        )
        return EXIT_UNKNOWN
    if not isinstance(status, dict):
        print(
            "REJECTED | capability=enabled but status payload missing; cannot "
            "verify rank states -- fix the tool layer / vLLM response"
        )
        return EXIT_GATE

    rejection = apply_gate(capability, status, instruction, exclude)
    if rejection is not None:
        code, message = rejection
        print("REJECTED | %s" % message)
        return code

    if args.json is not None:
        print("GATE_PASS | %s (dry-run on --json; not dispatched)" % instruction)
        return EXIT_OK

    if args.direct:
        warn("--direct bypasses the tool layer (brain-in-cluster). Debugging only.")
        http_status, body = dispatch_direct(args, instruction, params)
    elif args.tool_url:
        http_status, body = dispatch_tool(args, instruction, params)
    else:
        raise EepExit(EXIT_USAGE, "need one of --tool-url / --direct")

    if http_status == 200:
        print(
            "SENT | POST /fault_tolerance/apply (%s) -> 200 %s"
            % (instruction, json.dumps(body, ensure_ascii=False))
        )
    else:
        print(
            "FAILED | POST /fault_tolerance/apply (%s) -> HTTP %s %s"
            % (instruction, http_status, json.dumps(body, ensure_ascii=False))
        )
        return EXIT_EXEC_FAILED

    if not args.verify:
        print(
            "  note: 200 means the framework executed the instruction, NOT that the"
            " service recovered -- re-check /fault_tolerance/status (--verify)"
        )
        return EXIT_OK

    capability2, status2, evidence2 = fetch_status(args)
    if capability2 != "enabled" or not isinstance(status2, dict):
        print("verify: status re-check failed (%s)" % capability2)
        if evidence2:
            print("  evidence: %s" % evidence2)
        return EXIT_UNKNOWN
    problems2 = status_problems(status2)
    if problems2:
        print("verify: status data inconsistent: %s" % "; ".join(problems2))
        return EXIT_TRANSPORT
    total2, counts2, lines2 = analyze(status2)
    print(
        "verify: FT_ENABLED | total_engines=%s healthy=%s paused=%s unhealthy=%s dead=%s"
        % (
            total2,
            counts2["healthy"],
            counts2["paused"],
            counts2["unhealthy"],
            counts2["dead"],
        )
    )
    print_engine_lines(lines2)
    if instruction == "retry":
        recovered = counts2["healthy"] == total2 and counts2["dead"] == 0
    else:
        before = analyze(status)[0]
        recovered = total2 == before - len(exclude) and counts2["healthy"] == total2
    if recovered:
        print("verify: RECOVERED")
    else:
        print("verify: NOT_YET_RECOVERED -- keep polling status")
        return EXIT_GATE
    return EXIT_OK


def cmd_launch(args):
    violations = []

    def need(cond, message):
        if not cond:
            violations.append(message)

    need(args.dp_size >= 1, "dp_size must be >= 1")
    need(args.redundant_experts >= 0, "redundant_experts must be >= 0")
    for name in ("port", "fault_port", "internal_fault_port"):
        val = getattr(args, name)
        need(1 <= val <= 65535, "%s must be in 1..65535, got %s" % (name, val))
    need(args.port != args.fault_port, "port and fault_port must differ (C10)")
    need(
        args.port != args.internal_fault_port,
        "port and internal_fault_port must differ (C10)",
    )
    need(
        args.fault_port != args.internal_fault_port,
        "fault_port and internal_fault_port must differ (C10)",
    )
    # C1: recovery timeout must exceed both gloo timeout and the fixed 100s
    # DeepEP/nixl-ep kernel timeout, else startup raises ValueError.
    need(
        args.recovery_timeout > max(args.gloo_timeout_seconds, 100),
        "C1 violated: recovery_timeout=%s must be > max(gloo_timeout_seconds=%s, 100) "
        "else the service fails to start (ValueError)"
        % (args.recovery_timeout, args.gloo_timeout_seconds),
    )
    need(
        args.gloo_timeout_seconds < args.recovery_timeout,
        "C2 violated: gloo_timeout_seconds=%s must be < recovery_timeout=%s"
        % (args.gloo_timeout_seconds, args.recovery_timeout),
    )
    if args.num_nodes > 1:
        need(
            args.dp_size % args.num_nodes == 0,
            "multi-node: dp_size=%s must be divisible by num_nodes=%s"
            % (args.dp_size, args.num_nodes),
        )
        need(
            0 <= args.node_rank < args.num_nodes,
            "node_rank=%s out of range [0, %s)" % (args.node_rank, args.num_nodes),
        )
    if args.dp_size_local is not None:
        need(
            1 <= args.dp_size_local <= args.dp_size,
            "dp_size_local=%s must be in [1, dp_size=%s]" % (args.dp_size_local, args.dp_size),
        )

    if violations:
        print("LAUNCH_INVALID")
        for v in violations:
            print("  - %s" % v)
        return EXIT_GATE

    spec = {
        "tool": "vllm_ft_launch",
        "args": {
            "model": args.model,
            "served_model_name": args.model_name,
            "dp_size": args.dp_size,
            "redundant_experts": args.redundant_experts,
            "port": args.port,
            "fault_port": args.fault_port,
            "internal_fault_port": args.internal_fault_port,
            "recovery_timeout": args.recovery_timeout,
            "gloo_timeout_seconds": args.gloo_timeout_seconds,
            "host": args.host,
        },
    }
    if args.dp_size_local is not None:
        spec["args"]["dp_size_local"] = args.dp_size_local
    if args.npu_ids is not None:
        try:
            npu_ids = [int(x) for x in args.npu_ids.split(",") if x.strip()]
        except ValueError:
            raise EepExit(EXIT_USAGE, "--npu-ids must be comma-separated integers")
        if not npu_ids:
            raise EepExit(EXIT_USAGE, "--npu-ids must not be empty")
        spec["args"]["npu_ids"] = npu_ids
    if args.num_nodes > 1:
        spec["args"]["node_rank"] = args.node_rank
        spec["args"]["num_nodes"] = args.num_nodes
    if args.extra_args:
        try:
            extra = json.loads(args.extra_args)
        except ValueError:
            raise EepExit(EXIT_USAGE, "--extra-args must be valid JSON")
        spec["args"]["extra_args"] = extra

    print(
        "LAUNCH_SPEC_VALID | hard constraints passed (C1/C2 checked; C3 redundant"
        " experts must still be confirmed against the model's expert count)"
    )
    print(json.dumps(spec, ensure_ascii=False, indent=2))

    if not args.tool_url:
        print("  note: not submitted -- pass --tool-url to submit vllm_ft_launch")
        return EXIT_OK

    task = run_task(
        args.tool_url, "vllm_ft_launch", spec["args"], task_timeout=args.task_timeout
    )
    state = task.get("state")
    if state == "SUCCESS":
        print("LAUNCHED | %s" % json.dumps(task.get("result"), ensure_ascii=False))
        return EXIT_OK
    if state == "REJECTED":
        print("LAUNCH_REJECTED | %s" % json.dumps(task.get("error"), ensure_ascii=False))
        return EXIT_GATE
    print("LAUNCH_FAILED | %s" % json.dumps(task.get("error"), ensure_ascii=False))
    return EXIT_EXEC_FAILED


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _common(parent):
    parent.add_argument(
        "--tool-url",
        default=None,
        help="CAT tool-layer base URL (task envelope); default transport",
    )
    parent.add_argument(
        "--direct",
        action="store_true",
        help="call the vLLM REST API directly (debugging only)",
    )
    parent.add_argument(
        "--json",
        default=None,
        metavar="FILE|-",
        help="use a saved status payload instead of the network",
    )


def _parse_rank_list(text, parser):
    if text is None:
        return None
    parts = [p.strip() for p in text.split(",") if p.strip()]
    try:
        ranks = [int(p) for p in parts]
    except ValueError:
        parser.error("--exclude-dp-ranks must be comma-separated integers, got %r" % text)
    if not ranks:
        parser.error("--exclude-dp-ranks must not be empty for scale_down")
    if any(r < 0 for r in ranks):
        parser.error("--exclude-dp-ranks must not contain negative ranks")
    return ranks


def build_parser():
    parser = argparse.ArgumentParser(
        prog="eep.py",
        description="Elastic-EP agent-side forensic and execution helper",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="probe fault-tolerance capability and engine status")
    _common(p)
    p.add_argument("--host", default="localhost", help="vLLM API host (default: localhost)")
    p.add_argument("--port", type=int, default=8006, help="vLLM API port (default: 8006)")
    p.add_argument("--timeout", type=float, default=5.0, help="HTTP timeout seconds (default: 5)")

    p = sub.add_parser("apply", help="gate-check then dispatch retry / scale_down")
    _common(p)
    p.add_argument("--host", default="localhost", help="vLLM API host (default: localhost)")
    p.add_argument("--port", type=int, default=8006, help="vLLM API port (default: 8006)")
    p.add_argument("--timeout", type=float, default=30.0, help="instruction params.timeout seconds (default: 30)")
    p.add_argument("--instruction", required=True, choices=["retry", "scale_down"], help="instruction to dispatch")
    p.add_argument("--exclude-dp-ranks", default=None, help="comma-separated DP ranks to exclude (scale_down only)")
    p.add_argument("--verify", action="store_true", help="re-check status after dispatch")
    p.add_argument("--task-timeout", type=float, default=60.0, help="tool-layer task timeout seconds (default: 60)")

    p = sub.add_parser("launch", help="validate launch parameters and emit a launch task spec")
    _common(p)
    p.add_argument("--model", required=True, help="local model path")
    p.add_argument("--model-name", required=True, help="served model name")
    p.add_argument("--dp-size", type=int, required=True, help="data parallel size (DP ranks)")
    p.add_argument("--redundant-experts", type=int, required=True, help="redundant experts per rank")
    p.add_argument("--host", default="0.0.0.0", help="bind host (default: 0.0.0.0)")
    p.add_argument("--port", type=int, default=8006, help="API port (default: 8006)")
    p.add_argument("--fault-port", type=int, default=22867, help="external fault notify port (default: 22867)")
    p.add_argument("--internal-fault-port", type=int, default=22866, help="engine fault report port (default: 22866)")
    p.add_argument("--recovery-timeout", type=int, default=120, help="engine_recovery_timeout_sec (default: 120)")
    p.add_argument("--gloo-timeout-seconds", type=int, default=30, help="gloo timeout seconds (default: 30)")
    p.add_argument("--dp-size-local", type=int, default=None, help="local DP size per node (multi-node)")
    p.add_argument("--npu-ids", default=None, help="comma-separated NPU device ids (multi-node)")
    p.add_argument("--node-rank", type=int, default=0, help="this node's rank (default: 0)")
    p.add_argument("--num-nodes", type=int, default=1, help="total node count (default: 1)")
    p.add_argument("--extra-args", default=None, help="extra vLLM args as a JSON object")
    p.add_argument("--task-timeout", type=float, default=60.0, help="tool-layer task timeout seconds (default: 60)")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "status":
        code = cmd_status(args)
    elif args.command == "apply":
        if args.instruction == "scale_down":
            args.exclude_dp_ranks = _parse_rank_list(args.exclude_dp_ranks, parser)
        code = cmd_apply(args)
    elif args.command == "launch":
        code = cmd_launch(args)
    else:
        parser.error("unknown command %r" % args.command)
    sys.exit(code)


if __name__ == "__main__":
    try:
        main()
    except EepExit as e:
        print("ERROR: %s" % e)
        sys.exit(e.code)
