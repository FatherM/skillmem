#!/usr/bin/env bash
# PYTHON=/path/to/python bash scripts/docker_smoke.sh --local
set -euo pipefail
exec "${PYTHON:-python}" -P - "$@" <<'PY'
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import uuid

if sys.argv[1:] not in ([], ["--local"]):
    raise SystemExit("usage: docker_smoke.sh [--local]")
local = sys.argv[1:] == ["--local"]
name = "skillmem-smoke-" + uuid.uuid4().hex
with tempfile.TemporaryDirectory(prefix="skillmem-smoke-") as home:
    env = dict(os.environ, HOME=home, USERPROFILE=home, SKILLMEM_HOME=home,
               SKILLMEM_DB=str(Path(home) / "memory.db"), MEM_SEMANTIC="0")
    command = ([sys.executable, "-P", "-c", "from skillmem.mcp_server import run; run()"]
               if local else ["docker", "run", "--rm", "-i", "--name", name,
                              "--tmpfs", "/data", "-e", "HOME=/data",
                              "-e", "SKILLMEM_DB=/data/memory.db",
                              "-e", "MEM_SEMANTIC=0", "skillmem:ci"])
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errors:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=errors, text=True, encoding="utf-8", env=env)
        replies = queue.Queue()
        def read():
            for line in proc.stdout:
                replies.put(line)
            replies.put(None)
        threading.Thread(target=read, daemon=True).start()
        def send(message):
            proc.stdin.write(json.dumps(dict(jsonrpc="2.0", **message)) + "\n")
            proc.stdin.flush()
        def receive(request_id):
            import time
            deadline = time.monotonic() + 60
            while True:
                line = replies.get(timeout=max(0, deadline - time.monotonic()))
                assert line is not None, "server exited before replying"
                reply = json.loads(line)
                if reply.get("id") == request_id:
                    assert "error" not in reply, reply
                    return reply["result"]
        try:
            send(dict(id=1, method="initialize", params=dict(
                protocolVersion="2024-11-05", capabilities={},
                clientInfo=dict(name="docker-smoke", version="1"))))
            assert receive(1)["serverInfo"]["name"] == "skillmem"
            send(dict(method="notifications/initialized", params={}))
            send(dict(id=2, method="tools/list", params={}))
            tools = receive(2)["tools"]
            assert len(tools) == 9, tools
            print(f"PASS {'local' if local else 'docker'}: initialize + tools/list returned 9 tools (stdin open)")
        except BaseException:
            errors.seek(0)
            print(errors.read(), file=sys.stderr)
            raise
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
            proc.stdout.close()
            if not local:
                subprocess.run(["docker", "rm", "-f", "-v", name],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
PY
