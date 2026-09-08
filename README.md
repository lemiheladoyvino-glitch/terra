# Terra

Walking skeleton for a server-authoritative shared-world medieval MMO. Python 3.12,
FastAPI native WebSockets, asyncio, and a static Phaser 3.90.0 browser client.
Read [PROTOCOL.md](PROTOCOL.md) before extending the transport.

## Install and run

Python 3.12 is required (`requires-python = ">=3.12,<3.13"`). Ensure `python`
resolves to Python 3.12 before creating the virtual environment.

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
uvicorn server.app:app --reload
```

Open http://127.0.0.1:8000 for the 800×600 placeholder grid; the browser console
logs `welcome`. Loading Phaser requires internet access to jsDelivr. `/healthz`
returns `{"status":"ok"}`. Runtime-only installs use `requirements.txt`.

```sh
pytest
ruff check .
```

Run from the repo root. One uvicorn process is one world; do not use multiple
workers. Reload regenerates the seed-42 island and resources. There is no database,
auth, or persistence. Connections create players at a shared deterministic spawn;
disconnect removes them. The 10 Hz hook integrates movement and streams terrain
chunks plus AOI snapshots/deltas; the 1 Hz hook is reserved for future simulation.
Non-movement intents are validated and ignored. The browser remains the placeholder
grid until E1.3; it does not yet render streamed state or send movement intents.

The loop runs independently of connections, starts with app lifespan, and stops on
shutdown. Delayed scheduling catches up every tick. Simulation sends never block:
a full outgoing queue closes that connection with 1011; oversized state closes
with 1009. The recv/reply path still waits for queue space.

## Layout

```text
terra/
  server/
    __init__.py
    app.py                 # Lifespan, static client, health, WebSocket endpoint
    game/
      __init__.py
      loop.py              # 10 Hz / 1 Hz fixed-tick accumulator
      world.py             # Terrain, entities, spatial index, tick counter
      worldgen.py          # Deterministic island and terrain chunks
      entities.py          # Entity records and movement speed
      simulation.py        # Player lifecycle, movement, AOI streaming
    net/
      __init__.py
      protocol.py          # Strict JSON codec and schema constants
      connection.py        # Registry, receive loop, bounded send queue
  client/
    index.html
    js/main.js             # WebSocket and empty Phaser grid
  tests/
    test_protocol.py       # Codec, documented examples, transport smoke test
    test_loop.py           # Virtual-clock counts, catch-up, lifecycle
    test_worldgen.py       # Generation, spatial index, snapshot size
    test_simulation.py     # Movement, AOI/chunks, lifecycle, backpressure
  PROTOCOL.md
  README.md
  requirements.txt
  requirements-dev.txt
  pyproject.toml
  .gitignore
```
