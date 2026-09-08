from __future__ import annotations

import gzip
import json
import logging
import sqlite3
import zlib
from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from server.game.entities import Entity
from server.game.players import StoredPlayer, validate_player
from server.game.village import State, Village, Villager
from server.game.worldgen import generate_island

if TYPE_CHECKING:
    from server.game.world import World

SNAPSHOT_VERSION = 1
logger = logging.getLogger(__name__)


def world_to_dict(world: World) -> dict[str, Any]:
    """Detach all mutable state on the simulation loop. No terrain or _sites cursor."""
    return deepcopy(
        {
            "version": SNAPSHOT_VERSION,
            "seed": world.terrain.seed,
            "tick_count": world.tick_count,
            "grave_contents": world.grave_contents,
            "entities": [
                {"id": e.id, "kind": e.kind, "position": list(e.position), "fields": e.fields}
                for e in world.entities.values()
            ],
            "villages": [
                {
                    "index": v.index,
                    "center": list(v.center),
                    "stockpile": v.stockpile,
                    "buildings": [e.id for e in v.buildings],
                    "build_queue": v.build_queue,
                    "villagers": sorted(v.villagers),
                    "workers": {
                        id: {
                            "state": w.state.value,
                            "target": w.target,
                            "cargo": w.cargo,
                            "slot": list(w.slot) if w.slot is not None else None,
                        }
                        for id, w in v.workers.items()
                    },
                    "age": v.age,
                    "next_id": v.next_id,
                    "founders": sorted(v.founders),
                    "destination": list(v.destination) if v.destination is not None else None,
                }
                for v in world.villages
            ],
        }
    )


def world_from_dict(data: dict[str, Any]) -> World:
    """Rebuild without seeding. Unknown snapshot versions have no migration yet."""
    from server.game.world import World

    if not isinstance(data, dict) or type(data.get("version")) is not int:
        raise ValueError("missing snapshot version")
    if data["version"] != SNAPSHOT_VERSION:
        raise ValueError("unsupported snapshot version")
    if type(data["tick_count"]) is not int or data["tick_count"] < 0:
        raise ValueError("invalid tick count")
    world = World(generate_island(data["seed"]))
    world.tick_count = data["tick_count"]
    world.grave_contents = deepcopy(data.get("grave_contents", {}))
    for record in data["entities"]:
        world.add_entity(
            Entity(
                record["id"], record["kind"], tuple(record["position"]), deepcopy(record["fields"])
            )
        )
    for record in data["villages"]:
        workers = {
            id: Villager(
                id,
                State(w["state"]),
                w["target"],
                w["cargo"],
                tuple(w["slot"]) if w["slot"] is not None else None,
            )
            for id, w in record["workers"].items()
        }
        village = Village(
            index=record["index"],
            center=tuple(record["center"]),
            stockpile=deepcopy(record["stockpile"]),
            buildings=[world.entities[id] for id in record["buildings"]],
            build_queue=list(record["build_queue"]),
            villagers=set(record["villagers"]),
            workers=workers,
            age=record["age"],
            next_id=record["next_id"],
            founders=set(record["founders"]),
            destination=tuple(record["destination"]) if record["destination"] is not None else None,
        )
        if village.villagers != workers.keys() or not village.founders <= village.villagers:
            raise ValueError("inconsistent village membership")
        if not village.villagers <= world.entities.keys():
            raise ValueError("missing villager entities")
        world.villages.append(village)
    return world


def serialize_world(world: World) -> bytes:
    """Call on the event loop before dispatching the detached blob to disk."""
    return gzip.compress(
        json.dumps(world_to_dict(world), separators=(",", ":"), allow_nan=False).encode("utf-8"),
        mtime=0,
    )


def write_snapshot(db_path: str | Path, blob: bytes) -> None:
    """Disk-only operation: one transaction, connection opened/closed in the calling thread."""
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS kv "
            "(key TEXT PRIMARY KEY, value BLOB NOT NULL, updated_at TEXT NOT NULL)"
        )
        db.execute(
            "INSERT OR REPLACE INTO kv(key, value, updated_at) VALUES (?, ?, ?)",
            ("world", blob, datetime.now(UTC).isoformat()),
        )


def save_world(db_path: str | Path, world: World) -> None:
    write_snapshot(db_path, serialize_world(world))


def _quarantine(path: Path, error: Exception) -> None:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = path.with_name(f"{path.name}.corrupt-{stamp}")
    path.rename(destination)
    logger.error("Corrupt world database preserved at %s: %s; starting fresh", destination, error)


def load_world(db_path: str | Path) -> dict[str, Any] | None:
    path = Path(db_path)
    if not path.exists():
        return None
    try:
        with closing(sqlite3.connect(path)) as db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kv'"
            ).fetchone():
                return None
            row = db.execute("SELECT value FROM kv WHERE key='world'").fetchone()
    except sqlite3.DatabaseError as exc:
        # Lock/permission/I/O failures are operational, not evidence of corruption.
        if getattr(exc, "sqlite_errorcode", None) not in {
            sqlite3.SQLITE_CORRUPT,
            sqlite3.SQLITE_NOTADB,
        }:
            raise
        _quarantine(path, exc)
        return None
    if row is None:
        return None
    try:
        data = json.loads(gzip.decompress(row[0]))
        world_from_dict(data)  # Validate reconstructability before the caller resumes.
        return data
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        IndexError,
        OSError,
        EOFError,
        zlib.error,
    ) as exc:
        _quarantine(path, exc)
        return None


PLAYER_SNAPSHOT_VERSION = 1


def save_player(db_path: str | Path, stored: StoredPlayer) -> None:
    data = validate_player(stored)
    blob = gzip.compress(
        json.dumps(
            {"version": PLAYER_SNAPSHOT_VERSION, "player": data},
            separators=(",", ":"),
            allow_nan=False,
        ).encode(),
        mtime=0,
    )
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS kv "
            "(key TEXT PRIMARY KEY, value BLOB NOT NULL, updated_at TEXT NOT NULL)"
        )
        db.execute(
            "INSERT OR REPLACE INTO kv VALUES (?, ?, ?)",
            (f"player:{data['token']}", blob, datetime.now(UTC).isoformat()),
        )


def load_player(db_path: str | Path, token: str) -> StoredPlayer | None:
    if not Path(db_path).exists():
        return None
    with closing(sqlite3.connect(db_path)) as db, db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kv'"
        ).fetchone():
            return None
        key = f"player:{token}"
        row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            envelope = json.loads(gzip.decompress(row[0]))
            if (
                type(envelope["version"]) is not int
                or envelope["version"] != PLAYER_SNAPSHOT_VERSION
            ):
                raise ValueError("unsupported player version")
            data = validate_player(envelope["player"])
            if data["token"] != token:
                raise ValueError("player token mismatch")
            return data
        except (ValueError, TypeError, KeyError, OverflowError, OSError, EOFError, zlib.error):
            # Preserve the blob under a non-credential key; never log its contents/token.
            quarantine = f"corrupt-player:{uuid4()}"
            db.execute("UPDATE kv SET key=? WHERE key=?", (quarantine, key))
            logger.error(
                "Corrupt player row quarantined as %s; treating token as unknown", quarantine
            )
            return None
