#!/usr/bin/env python3
"""Regression tests for eep.py (stdlib-only, no external deps, no NPU needed).

Run:  python test_eep.py

Covers:
  A. status --json   forensics (capability + readiness verdicts)
  B. apply  --json   decision-gate dry-runs (retry / scale_down)
  C. launch          hard-constraint validation
  D. tool layer      task-envelope client against an in-process mock tool layer
  E. --direct        direct REST calls against an in-process mock vLLM API
  P. probe           test inference request (direct + tool layer)
  K. kill-rank       fault-injection gates (tool layer only)

Results are printed and also written to test_eep_results.txt next to this file.
Exit code: 0 = all passed, 1 = at least one failure.
"""

import http.server
import json
import os
import socket
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
EEP = os.path.join(HERE, "eep.py")
RESULTS_FILE = os.path.join(HERE, "test_eep_results.txt")
PY = sys.executable

STATUS_ALL_PAUSED = {
    "total_engines": 4,
    "engines": [
        {"id": 0, "status": "paused"},
        {"id": 1, "status": "unhealthy"},
        {"id": 2, "status": "dead"},
        {"id": 3, "status": "paused"},
    ],
}
STATUS_ONE_HEALTHY = {
    "total_engines": 4,
    "engines": [
        {"id": 0, "status": "healthy"},
        {"id": 1, "status": "paused"},
        {"id": 2, "status": "dead"},
        {"id": 3, "status": "paused"},
    ],
}
STATUS_ALL_HEALTHY = {
    "total_engines": 4,
    "engines": [{"id": i, "status": "healthy"} for i in range(4)],
}
STATUS_ALL_PAUSED_NO_DEAD = {
    "total_engines": 3,
    "engines": [
        {"id": 0, "status": "paused"},
        {"id": 1, "status": "unhealthy"},
        {"id": 2, "status": "paused"},
    ],
}
STATUS_ZERO_ENGINES = {"total_engines": 0, "engines": []}


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence request logging
        pass

    def _send(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def do_POST(self):
        server = self.server
        if self.path == "/api/tasks":
            payload = self._read_json()
            tool = payload.get("tool")
            task_id = "t%d" % server.next_task_id
            server.next_task_id += 1
            server.tool_of_task[task_id] = tool
            server.calls.append((tool, payload.get("args")))
            self._send(202, {"task_id": task_id})
            return
        if self.path == "/fault_tolerance/apply":
            payload = self._read_json()
            server.vllm_calls.append(payload)
            code, body = server.vllm_apply
            self._send(code, body)
            return
        if self.path == "/v1/chat/completions":
            payload = self._read_json()
            server.probe_calls.append(payload)
            code, body = server.vllm_probe
            self._send(code, body)
            return
        self._send(404, {})

    def do_GET(self):
        server = self.server
        if self.path.startswith("/api/tasks/"):
            task_id = self.path.rsplit("/", 1)[-1]
            tool = server.tool_of_task.get(task_id, "unknown")
            self._send(200, server.task_response(tool))
            return
        if self.path == "/fault_tolerance/status":
            code, body = server.vllm_status
            self._send(code, body)
            return
        self._send(404, {})


class MockServer(http.server.ThreadingHTTPServer):
    """Serves both the CAT tool-layer envelope (/api/tasks) and a mock vLLM
    fault-tolerance API (/fault_tolerance/*) on an ephemeral localhost port."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.next_task_id = 1
        self.tool_of_task = {}
        self.calls = []  # [(tool, args), ...]
        self.vllm_calls = []  # [apply payload, ...]
        self.probe_calls = []  # [chat payload, ...]
        self.task_responses = {}  # tool -> final body (dict or list of dicts)
        self.vllm_status = (200, {"total_engines": 0, "engines": []})
        self.vllm_apply = (200, {"message": "Instruction executed successfully."})
        self.vllm_probe = (200, {"choices": [{"message": {"content": "pong"}}]})
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%s" % self.server_address[1]

    def task_response(self, tool):
        resp = self.task_responses.get(tool)
        if resp is None:
            return {"state": "FAILED", "error": {"code": "UNKNOWN_TOOL", "message": str(tool)}}
        if isinstance(resp, list):
            return resp.pop(0) if len(resp) > 1 else resp[0]
        return resp

    def stop(self):
        self.shutdown()
        self.server_close()


def run(args, stdin_text=None, timeout=120):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [PY, EEP] + args,
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
    )


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    cases = []
    report = []

    def add(name, args, stdin=None, expect_exit=None, expect=None,
            expect_not=None, expect_err=None, check=None):
        cases.append({
            "name": name, "args": args, "stdin": stdin,
            "expect_exit": expect_exit, "expect": expect or [],
            "expect_not": expect_not or [], "expect_err": expect_err or [],
            "check": check,
        })

    # ---------------- A. status --json ----------------
    add("A1 status wait_pause", ["status", "--json", "-"],
        json.dumps(STATUS_ONE_HEALTHY), 0, ["FT_ENABLED WAIT_PAUSE", "healthy=1", "dead=1"])
    add("A2 status ready", ["status", "--json", "-"],
        json.dumps(STATUS_ALL_PAUSED), 0, ["FT_ENABLED READY", "healthy=0"])
    add("A3 status no_fault", ["status", "--json", "-"],
        json.dumps(STATUS_ALL_HEALTHY), 0, ["FT_ENABLED NO_FAULT"])
    add("A4 status raw body", ["status", "--json", "-"],
        json.dumps(STATUS_ALL_PAUSED_NO_DEAD), 0, ["FT_ENABLED READY", "total_engines=3"])
    add("A5 status disabled", ["status", "--json", "-"],
        json.dumps({"capability": "disabled", "status": None}), 4, ["FT_DISABLED", "404"])
    add("A6 status unknown", ["status", "--json", "-"],
        json.dumps({"capability": "unknown", "status": None}), 5, ["FT_UNKNOWN", "unreachable"])
    add("A7 status bad json", ["status", "--json", "-"], "not-json", 2, ["ERROR"])
    add("A8 status zero engines", ["status", "--json", "-"],
        json.dumps(STATUS_ZERO_ENGINES), 0, ["FT_ENABLED NO_FAULT", "total_engines=0", "0 engines"])
    add("A9 status enabled but status null", ["status", "--json", "-"],
        json.dumps({"capability": "enabled", "status": None}), 1, ["FT_ENABLED STATUS_MISSING"])
    add("A10 status total mismatch", ["status", "--json", "-"],
        json.dumps({"total_engines": 4, "engines": STATUS_ALL_PAUSED_NO_DEAD["engines"]}), 1,
        ["FT_ENABLED STATUS_INCONSISTENT", "total_engines=4 but engines list has 3"])
    add("A11 status duplicate id", ["status", "--json", "-"],
        json.dumps({"total_engines": 2, "engines": [{"id": 0, "status": "paused"},
                                                    {"id": 0, "status": "healthy"}]}), 1,
        ["FT_ENABLED STATUS_INCONSISTENT", "duplicate engine id 0"])
    add("A12 status unknown status value", ["status", "--json", "-"],
        json.dumps({"total_engines": 1, "engines": [{"id": 0, "status": "stuck"}]}), 1,
        ["FT_ENABLED STATUS_INCONSISTENT", "unknown status"])
    add("A13 status engines not a list", ["status", "--json", "-"],
        json.dumps({"total_engines": 1, "engines": {"id": 0, "status": "paused"}}), 1,
        ["FT_ENABLED STATUS_INCONSISTENT", "engines list"])
    add("A14 status non-dict engine entry", ["status", "--json", "-"],
        json.dumps({"total_engines": 2, "engines": [{"id": 0, "status": "paused"}, "garbage"]}), 1,
        ["FT_ENABLED STATUS_INCONSISTENT", "not a JSON object"])
    add("A15 status unhashable status value", ["status", "--json", "-"],
        json.dumps({"total_engines": 1, "engines": [{"id": 0, "status": ["x"]}]}), 1,
        ["FT_ENABLED STATUS_INCONSISTENT", "unknown status"])
    add("A16 status engines missing", ["status", "--json", "-"],
        json.dumps({"capability": "enabled", "status": {"total_engines": 0}}), 1,
        ["FT_ENABLED STATUS_INCONSISTENT", "engines list"])
    add("A17 status bool total_engines", ["status", "--json", "-"],
        json.dumps({"total_engines": True, "engines": []}), 1,
        ["FT_ENABLED STATUS_INCONSISTENT", "invalid total_engines"])
    add("A18 status bool engine id", ["status", "--json", "-"],
        json.dumps({"total_engines": 2,
                    "engines": [{"id": False, "status": "paused"},
                                {"id": True, "status": "paused"}]}), 1,
        ["FT_ENABLED STATUS_INCONSISTENT", "invalid id"])

    # ---------------- B. apply --json (gate dry-run) ----------------
    add("B1 retry healthy rejected", ["apply", "--json", "-", "--instruction", "retry"],
        json.dumps(STATUS_ONE_HEALTHY), 3, ["REJECTED", "WAIT_PAUSE", "healthy ranks"])
    add("B2 retry dead rejected", ["apply", "--json", "-", "--instruction", "retry"],
        json.dumps(STATUS_ALL_PAUSED), 3, ["REJECTED", "Engine dead", "scale_down"])
    add("B3 retry gate pass", ["apply", "--json", "-", "--instruction", "retry"],
        json.dumps(STATUS_ALL_PAUSED_NO_DEAD), 0, ["GATE_PASS", "retry"])
    add("B4 scale healthy rejected", ["apply", "--json", "-", "--instruction", "scale_down",
                                      "--exclude-dp-ranks", "2"],
        json.dumps(STATUS_ONE_HEALTHY), 3, ["REJECTED", "WAIT_PAUSE"])
    add("B5 scale missing dead", ["apply", "--json", "-", "--instruction", "scale_down",
                                  "--exclude-dp-ranks", "3"],
        json.dumps(STATUS_ALL_PAUSED), 3, ["REJECTED", "missing dead ranks", "[2]"])
    add("B6 scale gate pass", ["apply", "--json", "-", "--instruction", "scale_down",
                               "--exclude-dp-ranks", "2,3"],
        json.dumps(STATUS_ALL_PAUSED), 0, ["GATE_PASS", "scale_down"])
    add("B7 scale no exclude", ["apply", "--json", "-", "--instruction", "scale_down"],
        json.dumps(STATUS_ALL_PAUSED), 2, ["REJECTED", "requires non-empty"])
    add("B8 apply disabled", ["apply", "--json", "-", "--instruction", "retry"],
        json.dumps({"capability": "disabled", "status": None}), 4, ["gate: capability check failed"])
    add("B9 apply unknown", ["apply", "--json", "-", "--instruction", "retry"],
        json.dumps({"capability": "unknown", "status": None}), 5, ["gate: capability check failed"])
    add("B10 apply all healthy no fault", ["apply", "--json", "-", "--instruction", "retry"],
        json.dumps(STATUS_ALL_HEALTHY), 3, ["REJECTED", "no fault evidence"])
    add("B11 apply zero engines", ["apply", "--json", "-", "--instruction", "retry"],
        json.dumps(STATUS_ZERO_ENGINES), 3, ["REJECTED", "0 engines"])
    add("B12 scale exclude out of range", ["apply", "--json", "-", "--instruction", "scale_down",
                                           "--exclude-dp-ranks", "2,5"],
        json.dumps(STATUS_ALL_PAUSED), 3, ["REJECTED", "out of range", "[5]"])
    add("B13 apply enabled but status null", ["apply", "--json", "-", "--instruction", "retry"],
        json.dumps({"capability": "enabled", "status": None}), 3, ["REJECTED", "status payload missing"])
    add("B14 apply inconsistent status", ["apply", "--json", "-", "--instruction", "retry"],
        json.dumps({"total_engines": 4, "engines": STATUS_ALL_PAUSED_NO_DEAD["engines"]}), 3,
        ["REJECTED", "status data inconsistent"])
    add("B15 apply unexpected capability", ["apply", "--json", "-", "--instruction", "retry"],
        json.dumps({"capability": "weird", "status": STATUS_ALL_PAUSED_NO_DEAD}), 5,
        ["REJECTED", "unexpected capability"])
    add("B16 apply unexpected capability + no status", ["apply", "--json", "-", "--instruction", "retry"],
        json.dumps({"capability": "weird", "status": None}), 5,
        ["REJECTED", "unexpected capability"])

    # ---------------- C. launch ----------------
    launch_base = ["launch", "--model", "m", "--model-name", "n",
                   "--dp-size", "4", "--redundant-experts", "48"]
    add("C1 launch valid", launch_base, None, 0, ["LAUNCH_SPEC_VALID", "vllm_ft_launch"])
    add("C2 launch C1 violation", launch_base + ["--recovery-timeout", "60"], None, 3,
        ["LAUNCH_INVALID", "C1 violated", "ValueError"])
    add("C3 launch C2 violation", launch_base + ["--gloo-timeout-seconds", "130"], None, 3,
        ["LAUNCH_INVALID", "C2 violated"])
    add("C4 launch port conflict", launch_base + ["--fault-port", "8006"], None, 3,
        ["LAUNCH_INVALID", "port and fault_port must differ"])
    add("C7 launch internal port conflict", launch_base + ["--internal-fault-port", "8006"], None, 3,
        ["LAUNCH_INVALID", "port and internal_fault_port must differ"])
    add("C8 launch fault vs internal conflict", launch_base + ["--fault-port", "22866"], None, 3,
        ["LAUNCH_INVALID", "fault_port and internal_fault_port must differ"])
    add("C5 launch multinode", launch_base + ["--num-nodes", "3"], None, 3,
        ["LAUNCH_INVALID", "divisible"])
    add("C6 launch usage", ["launch"], None, 2)

    # ---------------- D. tool-layer envelope (mock) ----------------
    def add_tool(name, config, args, stdin, expect_exit, expect, expect_not=None, check=None):
        def scenario():
            server = MockServer()
            server.task_responses = config["responses"]
            server.vllm_status = config.get("vllm_status", (200, STATUS_ALL_PAUSED_NO_DEAD))
            server.vllm_apply = config.get("vllm_apply", (200, {"message": "ok"}))
            return server

        def body():
            server = scenario()
            proc = run([a.replace("{URL}", server.url) for a in args], stdin, timeout=120)
            server.stop()
            return proc, server

        cases.append({"name": name, "stdin": stdin, "expect_exit": expect_exit,
                      "expect": expect, "expect_not": expect_not or [],
                      "expect_err": [], "check": check, "_body": body})

    add_tool(
        "D1 tool status enabled",
        {"responses": {"vllm_ft_status": {"state": "SUCCESS", "result": {"capability": "enabled", "status": STATUS_ALL_PAUSED}}}},
        ["status", "--tool-url", "{URL}", "--host", "h", "--port", "8006"], None,
        0, ["FT_ENABLED READY"],
        check=lambda proc, s: len(s.calls) == 1 and s.calls[0][0] == "vllm_ft_status",
    )
    add_tool(
        "D2 tool status transport error",
        {"responses": {"vllm_ft_status": {"state": "FAILED", "error": {"code": "TRANSPORT_ERROR", "message": "connection refused"}}}},
        ["status", "--tool-url", "{URL}"], None,
        5, ["FT_UNKNOWN", "unreachable"],
    )
    add_tool(
        "D3 tool apply sent",
        {"responses": {
            "vllm_ft_status": {"state": "SUCCESS", "result": {"capability": "enabled", "status": STATUS_ALL_PAUSED_NO_DEAD}},
            "vllm_ft_apply": {"state": "SUCCESS", "result": {"http_status": 200, "body": {"message": "Instruction executed successfully."}}},
        }},
        ["apply", "--tool-url", "{URL}", "--instruction", "retry", "--timeout", "30"], None,
        0, ["SENT", "200"],
        check=lambda proc, s: len(s.calls) == 2
        and s.calls[1][0] == "vllm_ft_apply"
        and s.calls[1][1]["instruction"] == "retry"
        and s.calls[1][1]["params"]["timeout"] == 30,
    )
    add_tool(
        "D4 tool apply gate rejects before dispatch",
        {"responses": {"vllm_ft_status": {"state": "SUCCESS", "result": {"capability": "enabled", "status": STATUS_ONE_HEALTHY}}}},
        ["apply", "--tool-url", "{URL}", "--instruction", "retry"], None,
        3, ["REJECTED", "WAIT_PAUSE"],
        check=lambda proc, s: len(s.calls) == 1,  # apply never dispatched
    )
    add_tool(
        "D5 tool apply verify recovered",
        {"responses": {
            "vllm_ft_status": [{"state": "SUCCESS", "result": {"capability": "enabled", "status": STATUS_ALL_PAUSED_NO_DEAD}},
                               {"state": "SUCCESS", "result": {"capability": "enabled", "status": STATUS_ALL_HEALTHY}}],
            "vllm_ft_apply": {"state": "SUCCESS", "result": {"http_status": 200, "body": {"message": "ok"}}},
        }},
        ["apply", "--tool-url", "{URL}", "--instruction", "retry", "--verify"], None,
        0, ["SENT", "verify: RECOVERED"],
    )
    add_tool(
        "D6 tool launch",
        {"responses": {"vllm_ft_launch": {"state": "SUCCESS", "result": {"service_id": "svc-1", "api_port": 8006}}}},
        ["launch", "--tool-url", "{URL}", "--model", "m", "--model-name", "n",
         "--dp-size", "4", "--redundant-experts", "48"], None,
        0, ["LAUNCH_SPEC_VALID", "LAUNCHED", "svc-1"],
    )

    # ---------------- E. --direct (mock vLLM) ----------------
    def add_direct(name, config, args, expect_exit, expect, expect_not=None, check=None):
        def body():
            server = MockServer()
            server.vllm_status = config.get("vllm_status", (200, STATUS_ALL_PAUSED_NO_DEAD))
            server.vllm_apply = config.get("vllm_apply", (200, {"message": "ok"}))
            server.vllm_probe = config.get(
                "vllm_probe", (200, {"choices": [{"message": {"content": "pong"}}]})
            )
            host, port = "127.0.0.1", server.server_address[1]
            proc = run([a.replace("{HOST}", host).replace("{PORT}", str(port)) for a in args],
                       None, timeout=120)
            server.stop()
            return proc, server

        cases.append({"name": name, "stdin": None, "expect_exit": expect_exit,
                      "expect": expect, "expect_not": expect_not or [],
                      "expect_err": [], "check": check, "_body": body})

    add_direct(
        "E1 direct status 200",
        {"vllm_status": (200, STATUS_ALL_PAUSED)},
        ["status", "--direct", "--host", "{HOST}", "--port", "{PORT}"],
        0, ["FT_ENABLED READY"],
    )
    add_direct(
        "E2 direct status 404",
        {"vllm_status": (404, {"detail": "Not Found"})},
        ["status", "--direct", "--host", "{HOST}", "--port", "{PORT}"],
        4, ["FT_DISABLED", "404"],
    )
    add_direct(
        "E3 direct apply gate rejects healthy",
        {"vllm_status": (200, STATUS_ONE_HEALTHY)},
        ["apply", "--direct", "--host", "{HOST}", "--port", "{PORT}", "--instruction", "retry"],
        3, ["REJECTED", "WAIT_PAUSE"],
        check=lambda proc, s: len(s.vllm_calls) == 0,
    )
    add_direct(
        "E4 direct apply sent",
        {"vllm_status": (200, STATUS_ALL_PAUSED_NO_DEAD),
         "vllm_apply": (200, {"message": "Instruction executed successfully."})},
        ["apply", "--direct", "--host", "{HOST}", "--port", "{PORT}", "--instruction", "retry", "--timeout", "30"],
        0, ["SENT", "200"],
        check=lambda proc, s: len(s.vllm_calls) == 1
        and s.vllm_calls[0]["instruction"] == "retry"
        and s.vllm_calls[0]["params"] == {"timeout": 30},
    )
    add_direct(
        "E5 direct apply 500",
        {"vllm_status": (200, STATUS_ALL_PAUSED_NO_DEAD),
         "vllm_apply": (500, {"detail": "Instruction execution failed. Reason: Engine dead"})},
        ["apply", "--direct", "--host", "{HOST}", "--port", "{PORT}", "--instruction", "retry"],
        6, ["FAILED", "HTTP 500", "Engine dead"],
    )
    add_direct(
        "E6 direct apply 404 disabled",
        {"vllm_status": (404, {"detail": "Not Found"})},
        ["apply", "--direct", "--host", "{HOST}", "--port", "{PORT}", "--instruction", "retry"],
        4, ["gate: capability check failed"],
    )

    closed_port = free_port()
    cases.append({
        "name": "E7 direct status unreachable", "stdin": None, "expect_exit": 5,
        "expect": ["FT_UNKNOWN"], "expect_not": [], "expect_err": [], "check": None,
        "_body": lambda: (run(["status", "--direct", "--host", "127.0.0.1",
                               "--port", str(closed_port), "--timeout", "3"], None, timeout=120), None),
    })

    # ---------------- P. probe ----------------
    add_direct(
        "P1 direct probe 200",
        {"vllm_probe": (200, {"choices": [{"message": {"content": "pong"}}]})},
        ["probe", "--direct", "--host", "{HOST}", "--port", "{PORT}"],
        0, ["PROBE_OK", "200", "pong"],
        check=lambda proc, s: len(s.probe_calls) == 1
        and s.probe_calls[0]["messages"][0]["content"] == "ping"
        and s.probe_calls[0]["max_tokens"] == 1
        and "model" not in s.probe_calls[0],
    )
    add_direct(
        "P2 direct probe 500",
        {"vllm_probe": (500, {"error": "boom"})},
        ["probe", "--direct", "--host", "{HOST}", "--port", "{PORT}"],
        6, ["PROBE_FAILED", "HTTP 500"],
    )
    probe_closed_port = free_port()
    cases.append({
        "name": "P3 direct probe unreachable", "stdin": None, "expect_exit": 5,
        "expect": ["PROBE_UNREACHABLE"], "expect_not": [], "expect_err": [], "check": None,
        "_body": lambda: (run(["probe", "--direct", "--host", "127.0.0.1",
                               "--port", str(probe_closed_port), "--timeout", "3"], None, timeout=120), None),
    })
    add_tool(
        "P4 tool probe ok",
        {"responses": {"vllm_probe_request": {"state": "SUCCESS", "result": {
            "http_status": 200, "elapsed_ms": 12,
            "body": {"choices": [{"message": {"content": "hi"}}]}}}}},
        ["probe", "--tool-url", "{URL}", "--host", "h", "--port", "8006", "--prompt", "hello"], None,
        0, ["PROBE_OK", "hi"],
        check=lambda proc, s: len(s.calls) == 1
        and s.calls[0][0] == "vllm_probe_request"
        and s.calls[0][1]["prompt"] == "hello"
        and s.calls[0][1]["max_tokens"] == 1,
    )
    add_tool(
        "P5 tool probe transport error",
        {"responses": {"vllm_probe_request": {"state": "FAILED", "error": {"code": "TRANSPORT_ERROR", "message": "connection refused"}}}},
        ["probe", "--tool-url", "{URL}"], None,
        5, ["PROBE_UNREACHABLE"],
    )

    # ---------------- K. kill-rank (tool layer only) ----------------
    add_tool(
        "K1 kill-rank no confirmation",
        {"responses": {"vllm_ft_status": {"state": "SUCCESS", "result": {"capability": "enabled", "status": STATUS_ALL_PAUSED_NO_DEAD}}}},
        ["kill-rank", "--tool-url", "{URL}", "--host", "h", "--port", "8006", "--rank", "1"], None,
        3, ["REJECTED", "confirmation", "rank=1"],
        check=lambda proc, s: len(s.calls) == 1,  # no kill dispatched
    )
    add_tool(
        "K2 kill-rank ft disabled",
        {"responses": {"vllm_ft_status": {"state": "SUCCESS", "result": {"capability": "disabled", "status": None}}}},
        ["kill-rank", "--tool-url", "{URL}", "--host", "h", "--port", "8006", "--rank", "1", "--yes"], None,
        4, ["REJECTED", "not enabled"],
        check=lambda proc, s: len(s.calls) == 1,
    )
    add_tool(
        "K3 kill-rank target already dead",
        {"responses": {"vllm_ft_status": {"state": "SUCCESS", "result": {"capability": "enabled", "status": STATUS_ALL_PAUSED}}}},
        ["kill-rank", "--tool-url", "{URL}", "--host", "h", "--port", "8006", "--rank", "2", "--yes"], None,
        3, ["REJECTED", "already dead"],
        check=lambda proc, s: len(s.calls) == 1,
    )
    add_tool(
        "K4 kill-rank rank out of range",
        {"responses": {"vllm_ft_status": {"state": "SUCCESS", "result": {"capability": "enabled", "status": STATUS_ALL_PAUSED_NO_DEAD}}}},
        ["kill-rank", "--tool-url", "{URL}", "--host", "h", "--port", "8006", "--rank", "9", "--yes"], None,
        3, ["REJECTED", "out of range"],
        check=lambda proc, s: len(s.calls) == 1,
    )
    add_tool(
        "K5 kill-rank success explicit rank",
        {"responses": {
            "vllm_ft_status": {"state": "SUCCESS", "result": {"capability": "enabled", "status": STATUS_ALL_PAUSED_NO_DEAD}},
            "vllm_kill_rank": {"state": "SUCCESS", "result": {"killed_rank": 1, "pid": 1234, "method": "SIGKILL"}},
        }},
        ["kill-rank", "--tool-url", "{URL}", "--host", "h", "--port", "8006", "--rank", "1", "--yes"], None,
        0, ["KILLED", "rank=1", "pid=1234"],
        check=lambda proc, s: len(s.calls) == 2
        and s.calls[1][0] == "vllm_kill_rank"
        and s.calls[1][1]["rank"] == 1
        and s.calls[1][1]["confirmed"] is True,
    )
    add_tool(
        "K6 kill-rank random target",
        {"responses": {
            "vllm_ft_status": {"state": "SUCCESS", "result": {"capability": "enabled", "status": STATUS_ALL_PAUSED_NO_DEAD}},
            "vllm_kill_rank": {"state": "SUCCESS", "result": {"killed_rank": 1, "pid": 1234, "method": "SIGKILL"}},
        }},
        ["kill-rank", "--tool-url", "{URL}", "--host", "h", "--port", "8006", "--yes"], None,
        0, ["KILLED"],
        check=lambda proc, s: len(s.calls) == 2
        and s.calls[1][0] == "vllm_kill_rank"
        and s.calls[1][1]["rank"] in (0, 1, 2)
        and s.calls[1][1]["confirmed"] is True,
    )
    add_tool(
        "K7 kill-rank all dead",
        {"responses": {"vllm_ft_status": {"state": "SUCCESS", "result": {"capability": "enabled", "status": {"total_engines": 2, "engines": [
            {"id": 0, "status": "dead"}, {"id": 1, "status": "dead"}]}}}}},
        ["kill-rank", "--tool-url", "{URL}", "--host", "h", "--port", "8006", "--yes"], None,
        3, ["REJECTED", "all 2 ranks are dead"],
        check=lambda proc, s: len(s.calls) == 1,
    )
    add(
        "K8 kill-rank direct refused",
        ["kill-rank", "--direct", "--host", "h", "--port", "8006", "--yes"], None,
        2, ["tool layer"],
    )
    add(
        "K9 kill-rank json refused",
        ["kill-rank", "--json", "-", "--yes"], json.dumps(STATUS_ALL_PAUSED_NO_DEAD),
        2, ["live service"],
    )
    add(
        "K10 kill-rank ssh+tool-url refused",
        ["kill-rank", "--ssh", "u@h", "--tool-url", "http://127.0.0.1:1", "--json", "-", "--yes"],
        json.dumps(STATUS_ALL_PAUSED_NO_DEAD), 2, ["not both"],
    )
    add(
        "K11 kill-rank ssh needs status source",
        ["kill-rank", "--ssh", "u@h", "--yes"], None,
        2, ["status source"],
    )
    add(
        "K12 kill-rank ssh no confirmation",
        ["kill-rank", "--ssh", "u@h", "--json", "-", "--rank", "1"],
        json.dumps(STATUS_ALL_PAUSED_NO_DEAD), 3, ["confirmation", "rank=1"],
    )
    add(
        "K13 kill-rank ssh target already dead",
        ["kill-rank", "--ssh", "u@h", "--json", "-", "--rank", "2", "--yes"],
        json.dumps(STATUS_ALL_PAUSED), 3, ["already dead"],
    )

    # ---------------- run ----------------
    passed = failed = 0
    lines = []
    for case in cases:
        name = case["name"]
        if "_body" in case:
            proc, server = case["_body"]()
        else:
            proc = run(case["args"], case["stdin"], timeout=120)
            server = None
        exit_code = proc.returncode
        ok = exit_code == case["expect_exit"]
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        if ok:
            for needle in case["expect"]:
                if needle not in stdout:
                    ok = False
                    break
        if ok:
            for needle in case["expect_not"]:
                if needle in stdout:
                    ok = False
                    break
        if ok:
            for needle in case["expect_err"]:
                if needle not in stderr:
                    ok = False
                    break
        if ok and case.get("check"):
            try:
                if not case["check"](proc, server):
                    ok = False
            except Exception as exc:
                ok = False
                stdout += "\n[check raised: %r]" % exc
        if ok:
            passed += 1
            lines.append("PASS  %-34s (exit %d)" % (name, exit_code))
        else:
            failed += 1
            lines.append("FAIL  %-34s (exit %d, expected %d)" % (name, exit_code, case["expect_exit"]))
            lines.append("      expected in stdout: %r" % case["expect"])
            if case["expect_not"]:
                lines.append("      not expected in stdout: %r" % case["expect_not"])
            if case["expect_err"]:
                lines.append("      expected in stderr: %r" % case["expect_err"])
            if stdout.strip():
                lines.append("      stdout: %s" % stdout.strip().replace("\n", "\\n"))
            if stderr.strip():
                lines.append("      stderr: %s" % stderr.strip().replace("\n", "\\n"))

    summary = "TOTAL=%d PASS=%d FAIL=%d" % (len(cases), passed, failed)
    lines.append("")
    lines.append(summary)

    text = "\n".join(lines)
    print(text)
    with open(RESULTS_FILE, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print("results written to %s" % RESULTS_FILE)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
