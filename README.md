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
workers. Reload restarts the placeholder world. There is no database, auth,
persistence, or gameplay yet. Valid intents are validated and ignored. Both loop
hooks are empty, so the placeholder world's tick remains zero. The loop runs
independently of connections, starts with app lifespan, and stops on shutdown.
Its synchronous hooks must not block; delayed scheduling catches up every tick.

## Layout

```text
terra/
  server/
    __init__.py
    app.py                 # Lifespan, static client, health, WebSocket endpoint
    game/
      __init__.py
      loop.py              # 10 Hz / 1 Hz fixed-tick accumulator
      world.py             # Entity dictionary and tick placeholder
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
  PROTOCOL.md
  README.md
  requirements.txt
  requirements-dev.txt
  pyproject.toml
  .gitignore
```
