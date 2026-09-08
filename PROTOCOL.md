# Terra WebSocket protocol — schema 1

This document defines the MVP wire contract. Only validation, immediate `welcome`,
and protocol errors are implemented in this walking skeleton. Valid intents have
no effects; no entities, snapshots, authentication, persistence, or gameplay exist yet.
The example values are illustrative, not game balance decisions.

## Transport and validation

One ordered WebSocket at `/ws`, JSON text messages only, one object per message.
Use WSS when served over HTTPS. Every message requires `t` and integer `v: 1`.
`SCHEMA_VERSION = 1` and `MAX_MESSAGE_BYTES = 65536` live in `server/net/protocol.py`.
The limit is the UTF-8 size of a complete message, inclusive; binary frames,
malformed JSON, duplicate keys, nonfinite numbers, unknown types, wrong versions,
missing fields and extra fields are invalid. Booleans are not numbers.
Objects below have exactly the shown fields, except explicitly described variants.
IDs are opaque nonempty strings of at most 128 Unicode characters. Other strings
are nonempty and at most 2048 characters. Integers have no fractional part;
`uint` means integer >= 0. Position coordinates are finite JSON numbers in tiles;
positive x is east and positive y south. Tile coordinates are integers.

`encode` and `decode` validate the complete object and raise `ProtocolError`.
Both directions use the same codec; connection handlers enforce direction.
Invalid incoming messages receive `error` with code `invalid_message`; the socket
remains open. The codec does not perform authorization or world-state validation.
The server has a bounded queue of 64 outgoing messages; producers wait for space.

Bump the schema integer for any wire field, type, allowed value, or semantic
change (including additions, since objects are closed). Deploy matching clients
and servers; there is no cross-version negotiation or partial acceptance.

## Client intents

`hello` must be the first message in the future authenticated MVP. Token issuance
is outside this protocol; treat the token as an opaque bearer credential and never
log it. The skeleton does not require hello or authenticate tokens.

```json
{"t":"hello","v":1,"token":"opaque-resume-token"}
```

`move` has exactly one of `direction` and `target`. Direction components are in
[-1,1]; the server normalizes a nonzero vector, and (0,0) stops movement. A target
requests movement toward a world position. The latest intent replaces the previous
one, applied at the next movement tick; speed and path validation are server-owned.

```json
{"t":"move","v":1,"direction":{"x":1,"y":0}}
```
```json
{"t":"move","v":1,"target":{"x":12.5,"y":8}}
```

`interact` targets exactly one entity or tile. Actions are exactly `chop`, `mine`,
`eat`, `trade`, `craft`, `pickup`. The server checks range and target applicability.
`craft` interaction selects/opens a crafting station; the separate `craft` intent
requests one recipe execution using that context (or a hand recipe). `trade`
requests the target merchant's server-defined default exchange; arbitrary offers
are outside this MVP. `eat`/`pickup` operate on the target's available resource.

```json
{"t":"interact","v":1,"target":{"entity_id":"tree-1"},"action":"chop"}
```
```json
{"t":"interact","v":1,"target":{"tile":{"x":12,"y":8}},"action":"mine"}
```
```json
{"t":"craft","v":1,"recipe_id":"wooden-axe"}
```

Chat is plain text, rendered as text, never HTML. MVP chat is local: delivered to
connected players whose AOI contains the sender at send time, including the sender.

```json
{"t":"chat","v":1,"text":"Hello there"}
```

## Server messages

`welcome` assigns the controlled entity and supplies authoritative timing/config.
`tick` is world time in movement ticks, starting at zero: seconds = tick / 10.
Rates are fixed at integer 10 and 1; AOI radius is a positive number in tiles.
In the skeleton the entity ID is a connection UUID with no corresponding entity,
and tick stays zero because the world and both hooks are placeholders.

```json
{"t":"welcome","v":1,"entity_id":"player-1","tick":120,"config":{"movement_hz":10,"sim_hz":1,"aoi_radius":32}}
```

`snapshot` atomically replaces the client's AOI entity map. Send it after welcome
and before deltas. All entity IDs within a message must be unique.

```json
{"t":"snapshot","v":1,"tick":120,"entities":[{"id":"player-1","kind":"player","position":{"x":12,"y":8},"name":"Ada","hp":100},{"id":"tree-1","kind":"tree","position":{"x":14,"y":8},"resource_remaining":10}]}
```

`delta` contains full records for `entered` and `changed`, and IDs for `left`.
The three groups must have distinct IDs. `entered` adds previously absent IDs;
`changed` replaces a complete existing record; `left` removes an existing ID.
Empty arrays are valid. Apply a message atomically. AOI exit and destruction both
use `left`; it conveys no destruction reason. No field-level merge or tombstones.

```json
{"t":"delta","v":1,"tick":121,"entered":[{"id":"rock-1","kind":"rock","position":{"x":15,"y":8},"resource_remaining":5}],"left":["tree-1"],"changed":[{"id":"player-1","kind":"player","position":{"x":12.1,"y":8},"name":"Ada","hp":100}]}
```

`inventory` is a full private replacement of the recipient's occupied slots.
Slot indexes are unique uints; omitted slots are empty. Quantities are positive
integers. Durability is a finite fraction [0,1], with 1 for non-degrading items.

```json
{"t":"inventory","v":1,"tick":121,"slots":[{"slot":0,"item_id":"wooden-axe","quantity":1,"durability":0.8}]}
```

`event` is a private notification with exactly one of these three variants.
Inventory/entity updates carry authoritative state; events do not mutate client
state. A successful trade has `success: true`; reason is nonempty human-readable
text in both success and failure cases.

```json
{"t":"event","v":1,"tick":122,"event":"tool_broke","item_id":"wooden-axe"}
```
```json
{"t":"event","v":1,"tick":123,"event":"you_died","grave_id":"grave-1"}
```
```json
{"t":"event","v":1,"tick":124,"event":"trade_result","success":false,"reason":"Not enough wood"}
```
```json
{"t":"chat","v":1,"sender_id":"player-2","tick":124,"text":"Hello there"}
```

`error` reports a rejected message/intent. Codes use the ID string format; clients
must tolerate new code values and display `message` as text. MVP meanings are
`invalid_message`, `unauthorized`, `invalid_intent`, and `not_implemented`.
The skeleton emits only `invalid_message`; valid intents are silently ignored.

```json
{"t":"error","v":1,"code":"invalid_message","message":"unknown message type"}
```

## Entities

Every entity has `id`, `kind`, `position`, and exactly the kind-specific fields
below. HP, resource counts, and expiry ticks are uints. Names are text; building,
road and shop types and owner IDs use ID strings. No inventory or tokens are
exposed through entity records. Additional kinds require a schema bump.

| Kind | Required kind-specific fields |
| --- | --- |
| player | `name`, `hp` |
| villager | `name`, `hp` |
| tree | `resource_remaining` |
| rock | `resource_remaining` |
| berry-bush | `resource_remaining` |
| building | `building_type`, `hp` |
| grave | `owner_id`, `expires_tick` |
| merchant | `name`, `shop_id` |
| road | `road_type` |

For example, a grave record is
`{"id":"grave-1","kind":"grave","position":{"x":2,"y":3},"owner_id":"player-1","expires_tick":900}`.

## AOI and timing

The server computes a circular AOI centered on the authoritative player position
using squared Euclidean distance <= radius squared, including the player itself.
After each 100 ms movement tick (and any coincident simulation tick), it compares
membership with the previous sent state: new members enter, missing members leave,
and retained members whose serialized fields differ change. Send at most one delta
per movement tick, including an empty delta when unchanged, to advance client time.
A teleport uses the same membership comparison. Changes outside AOI are not sent.
A snapshot must fit the message limit; population/radius must be bounded by the
future server implementation. Chunking is not defined in version 1; do not silently
truncate a snapshot or delta. If a state message cannot fit, close with 1009.

The loop uses monotonic elapsed time, an integer nanosecond accumulator, 10 Hz
movement and 1 Hz simulation. Every tenth movement tick runs simulation afterward.
No elapsed time is discarded during catch-up. The future world counter increments
once per movement tick; the world continues ticking with zero connections. Tick
values are logical time, not Unix timestamps. No wall-clock values are transmitted.

After a snapshot at T, delta ticks must increase strictly. Other messages may share
a tick. Clients interpolate remote positions between buffered authoritative states
at a render time two movement ticks (200 ms) behind estimated server time. Estimate
server time from the newest received tick plus local monotonic elapsed time. Hold
the newest position if no bracketing state exists; do not extrapolate. On enter,
start at the received position; on leave remove the entity. An unknown changed/left
ID or out-of-order delta is a state mismatch: reconnect for a fresh snapshot.
No prediction, rollback, client timestamps, replay, or intent acknowledgements yet.

## Reconnection

The future client opens a new socket and sends `hello` with its existing token.
The server resolves it to the same persistent player, sends `welcome` with current
time, then a fresh `snapshot` and `inventory`. Invalid tokens receive `unauthorized`
and close code 1008. A new authorized connection replaces the old connection for
that player (old socket closes with 1000). Disconnection does not pause the world.
Never replay unacknowledged intents automatically: they may have already executed.
There is no delta-history resume or offline event replay. In this skeleton every
connection gets a new placeholder UUID immediately; token resume is not implemented.

Kingdoms, war, and jobs are explicitly out of scope.
