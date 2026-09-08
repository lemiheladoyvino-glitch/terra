#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="$PWD/.venv/bin/python"
UVICORN="$PWD/.venv/bin/uvicorn"
PORT="${SMOKE_PORT:-8899}"
export PORT
TEMP_DIR="$(mktemp -d)"
export TERRA_DB="$TEMP_DIR/terra.db"
PID=""
TERM_SENT=false
cleanup() {
  local status=$?
  trap - EXIT
  set +e
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
    if [[ "$TERM_SENT" != true ]]; then
      kill -TERM "$PID" 2>/dev/null || true
    fi
    sleep 1
    if kill -0 "$PID" 2>/dev/null; then
      kill -KILL "$PID" 2>/dev/null || true
    fi
    wait "$PID" 2>/dev/null || true
  fi
  rm -rf "$TEMP_DIR"
  exit "$status"
}
trap cleanup EXIT
"$UVICORN" server.app:app --host 0.0.0.0 --port "$PORT" --workers 1 --no-access-log --log-config log_config.yaml >"$TEMP_DIR/server.log" 2>&1 &
PID=$!
ready=false
for ((i=0; i<100; i++)); do
  if ! kill -0 "$PID" 2>/dev/null; then
    cat "$TEMP_DIR/server.log"
    echo 'FAIL: server exited before readiness' >&2
    exit 1
  fi
  if curl --fail --silent --max-time 1 "http://127.0.0.1:$PORT/healthz" >"$TEMP_DIR/health.json" &&
     "$PYTHON" -c 'import json,sys; h=json.load(open(sys.argv[1])); assert h["status"] == "ok"; assert all(isinstance(h[k],int) for k in ("tick","entities","villages"))' "$TEMP_DIR/health.json"; then
    ready=true
    break
  fi
  sleep 0.2
done
if [[ "$ready" != true ]]; then cat "$TEMP_DIR/server.log"; echo 'FAIL: readiness timeout' >&2; exit 1; fi
echo 'PASS: /healthz is ready with world counters'
"$PYTHON" - <<'PY'
import asyncio
import json
import os
from websockets.asyncio.client import connect

async def smoke():
    async with connect(f'ws://127.0.0.1:{os.environ["PORT"]}/ws') as ws:
        await ws.send(json.dumps({"t": "hello", "v": 4, "token": "smoke-new-player"}))
        welcome = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert welcome['t'] == 'welcome'
        chunk_seen = False
        async with asyncio.timeout(10):
            while True:
                message = json.loads(await ws.recv())
                chunk_seen |= message['t'] == 'chunk'
                if message['t'] == 'snapshot':
                    assert chunk_seen
                    own = next(e for e in message['entities'] if e['id'] == welcome['entity_id'])
                    break
        print('PASS: welcome, terrain chunk, and snapshot received')
        for x, y in [(1, 0), (0, 1), (-1, 0), (0, -1)]:
            await ws.send(json.dumps({'t': 'move', 'v': 4, 'direction': {'x': x, 'y': y}}))
            async with asyncio.timeout(5):
                for _ in range(10):
                    message = json.loads(await ws.recv())
                    if message['t'] == 'delta' and any(
                        e['id'] == own['id'] and e['position'] != own['position']
                        for e in message['changed']
                    ):
                        print('PASS: authoritative movement delta received')
                        return
        raise AssertionError('player never moved')

asyncio.run(smoke())
PY
kill -TERM "$PID"
TERM_SENT=true
for ((i=0; i<75; i++)); do
  if ! kill -0 "$PID" 2>/dev/null; then break; fi
  sleep 0.2
done
if kill -0 "$PID" 2>/dev/null; then echo 'FAIL: SIGTERM shutdown timeout' >&2; exit 1; fi
# SIGTERM exit status (143) is expected; verify persistence below.
wait "$PID" || true
PID=""
"$PYTHON" - <<'PY'
import gzip
import json
import os
import sqlite3
with sqlite3.connect(os.environ['TERRA_DB']) as db:
    row = db.execute("SELECT value FROM kv WHERE key='world'").fetchone()
assert row is not None, 'missing shutdown snapshot'
snapshot = json.loads(gzip.decompress(row[0]))
assert snapshot['tick_count'] > 0
print('PASS: SIGTERM completed and durable world snapshot verified')
PY
cat "$TEMP_DIR/server.log"
