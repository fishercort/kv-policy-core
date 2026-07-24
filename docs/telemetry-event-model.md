# KV telemetry event model (v0)

The neutral, engine-agnostic model that KV-cache telemetry from any inference engine
normalizes to. This is the telemetry half of the protocol (the policy/port half is
`kv_policy_core.interfaces`). An engine adapter maps that engine's native cache events to
this model; everything downstream — analysis, export, storage — consumes only this model, so
it is engine-independent by construction.

**Versioned.** Every batch carries `schema_version` (this doc: `v0`). Consumers pin it;
adapters that emit a newer version are a visible diff, not a silent drift.

## Design invariants

1. **Metadata-only by design.** The model carries block *hashes*, sizes, tiers, timestamps,
   and lifecycle — never token ids, never text, never KV tensors. The metadata-only property
   is a property of the *protocol*, not just of one implementation: there is no field in
   which content could travel. (KV tensors are not present in any engine's event stream;
   they never leave the accelerator.)
2. **Prefix-semantic hashes.** A block hash is prefix-chained: `hash(parent_hash,
   block_tokens)`. The same prefix produces the same hash across processes only when the
   engines share the hash function and its seed; adapters record the hashing parameters so a
   consumer knows whether cross-instance hashes are comparable. Overlap is measured on
   chained hashes, never bags of blocks.
3. **Salted at egress.** Raw content-hash ids are a content fingerprint. Before any record
   leaves the host they are replaced by a per-domain salted id (`H(salt, domain, raw)`),
   deterministic within a (salt, domain) so overlap remains detectable while the raw id
   never crosses the boundary. The raw hash exists only in-process, transiently.

## Events

A `Batch` is `{schema_version, ts, source_id, events[]}`. `source_id` identifies the
emitting engine instance (for cross-instance analysis). Each event is one of:

### BlockStored
A full block was (re)computed and inserted into the cache.
```
BlockStored {
  block_hash:   Hash        # prefix-chained; salted at egress
  parent_hash:  Hash | null # the chained parent (null = prefix root)
  block_size:   int         # tokens per block (the cache granularity)
  medium:       Tier        # GPU | CPU | FS | OBJ
  ts:           float
}
```
No `token_ids`. Adapters that receive token ids from their engine (e.g. vLLM) MUST drop them
at the adapter boundary; they are never admitted into the model.

### BlockRemoved
A block was evicted/dropped from a tier.
```
BlockRemoved { block_hash: Hash, medium: Tier, ts: float }
```

### AllBlocksCleared
A tier was fully cleared (flush, restart, session close).
```
AllBlocksCleared { medium: Tier, ts: float }
```

### RequestCacheReport (optional)
Per-request cache outcome, where the engine exposes it — the local-hit count an engine's own
prefix cache already served, so cross-instance analysis can subtract what one engine already
saw.
```
RequestCacheReport {
  request_id:        Id
  local_cached_tokens:    int     # served from this instance's own cache
  external_cached_tokens: int | null
  ts:               float
}
```

## Sequencing

Adapters SHOULD forward a monotonic per-source sequence number alongside each batch so a
consumer can detect dropped batches (a gap = lost events = undercounted analysis). The
sequence is transport metadata, not part of the event body.

## Reference adapter: vLLM

vLLM's KV cache events (`BlockStored` / `BlockRemoved` / `AllBlocksCleared` over a ZMQ
publisher, msgpack, verified against v0.11.0) map directly:
- vLLM `BlockStored.block_hashes` / `parent_block_hash` → `BlockStored.block_hash` /
  `parent_hash` (one model event per block hash), `block_size` → `block_size`, `medium` →
  `medium`. vLLM's `token_ids` is **dropped at the adapter** (metadata-only invariant).
- vLLM `BlockRemoved` / `AllBlocksCleared` → the same-named model events.
- Prometheus `prefix_cache_queries`/`_hits` and per-request `cached_tokens` →
  `RequestCacheReport`.
- Cross-instance hash comparability requires the vLLM instances to share `PYTHONHASHSEED`
  and hash algo; the adapter records both so a consumer knows the hashes are comparable.

## Conformance

Two corpora under `conformance/` pin the model; any implementation (the Rust `infertap`
Source, a future adapter) must reproduce both.

- `conformance/telemetry/` pins the Compute contract: given a config and a stream of
  normalized events, the required residual/eviction detection, catalog stats, and
  metadata-only records.
- `conformance/vllm-wire/` pins the vLLM decode-then-normalize path at the byte layer. Each
  fixture carries `wire_hex`, the msgpack payload as msgspec (the encoder vLLM runs) produces
  it, so integer-width selection, str/bin family, and omit-trailing-default are the real wire
  choices rather than a decoder agreeing with itself. A well-formed fixture states the
  normalized `events` a faithful decoder must emit; a malformed fixture states the fail-closed
  `error`: which `stage` rejects it (`decode` = not valid msgpack, `normalize` = valid
  msgpack but invalid schema, `any` = either) and a substring its message must contain. The
  corpus header pins the vLLM and msgspec versions, so regenerating against a newer vLLM is
  the drift signal.

The vLLM adapter is two layers kept sharp: decode the payload to a value faithfully (no
coercion, an absent optional stays absent), then normalize the value to the model (where all
schema semantics live). The malformed rows carry hostile input as a contract: truncated and
non-msgpack payloads fail at decode; wrong-type, wrong-arity, and unknown-tag events fail at
normalize. Bytes-family (sha256) hashes are v1 scope; v0 fails closed on them rather than
mis-decoding.

## Version history
- **v0** — initial model: BlockStored / BlockRemoved / AllBlocksCleared / RequestCacheReport,
  metadata-only, prefix-chained hashes, salted egress, per-source sequencing.
