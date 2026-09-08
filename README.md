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

Open http://127.0.0.1:8000 and walk with WASD or arrow keys. Open a second tab
to see another player. The canvas resizes to the window; the HUD shows connection,
tile position, visible entities, and server tick. Loading Phaser requires internet access to jsDelivr. `/healthz`
returns `{"status":"ok"}`. Runtime-only installs use `requirements.txt`.

```sh
pytest
ruff check .
node --test client/js/*.test.mjs
```

The client uses plain ES modules without a build step or npm dependencies. Node.js
is only needed for the RLE unit test.

Run from the repo root. One uvicorn process is one world; do not use multiple
workers. Restart resumes the last saved world; a new database starts a seed-42 island.
Players resume with a durable bearer token stored in browser localStorage. Connections create players at a shared deterministic spawn;
disconnect removes the live entity and saves the player. The 10 Hz hook integrates movement and streams terrain
chunks plus AOI snapshots/deltas; the 1 Hz hook is reserved for future simulation.
Non-movement intents are validated and ignored. The browser caches terrain chunks,
interpolates remote entities by 200 ms, and predicts local movement. Disconnection
clears client state and retries every second with the saved player token.

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
    package.json           # ES module mode for Node's built-in test runner
    js/
      constants.js         # Shared tile scale, palette, timing, movement constants
      main.js              # Phaser scene, keyboard input, DOM HUD
      net.js               # WebSocket dispatch, deduplicated moves, reconnect
      render.js            # Chunk/entity visuals, interpolation, prediction
      rle.js               # Strict terrain chunk decoder
      rle.test.mjs         # Node built-in unit tests
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

## World persistence

`TERRA_DB` selects the SQLite file (default `./terra.db`). New worlds use seed 42;
existing snapshots restore their own seed, tick, entities, and villages without
resource or village reseeding. Autosave runs every 30 seconds; graceful shutdown
stops the loop, waits for an active save, then writes one final snapshot. Mapping,
JSON encoding, and gzip compression run on the event loop; only SQLite I/O runs
in a worker thread. Saves share a lock and use one atomic row transaction.

World snapshot version 1 and player snapshot version 1 are independent of WebSocket schema 4. There are no migrations
yet: incompatible versions or corrupt snapshots quarantine the original database
as `<filename>.corrupt-<UTC timestamp>`, log an error, and start a fresh world.
Terrain is regenerated from the seed at the fixed MVP size of 192 tiles. Changing
the generator requires a snapshot version bump or a future migration.

Village buildings reference entity IDs; workers retain every FSM field. The
`_sites` search cursor is deliberately omitted and restarted lazily on load, so
an in-progress expansion search can take a different number of ticks to finish.
Player inventory, HP, hunger, name, stable ID, and position are saved in compressed
`player:<token>` rows on disconnect and when changed every ten survival ticks.
SQLite writes run in a worker, serialized by a player-save lock; shutdown drains
pending saves. Corrupt player rows are preserved under `corrupt-player:<uuid>`
without quarantining the world database or logging the token. Grave contents are
now an optional field in world snapshots; existing version-1 worlds still load.
Player entities from crash snapshots are removed until their owners reconnect.
World and player saves are separate transactions, not an atomic cross-row checkpoint.
