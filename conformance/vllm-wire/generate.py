"""Generator for the vLLM wire conformance corpus.

Self-contained: depends only on msgspec, the msgpack encoder vLLM itself runs
(ZmqEventPublisher, v0.11.0). It imports nothing from any reference agent -- the corpus is
an authored SPECIFICATION of the byte-level decode+normalize contract, not a dump of any one
implementation's behavior. Every conformer (the Rust `infertap` Source, any future adapter)
must reproduce it; none is privileged.

Two layers are pinned here:

  wire_hex   the msgpack payload as msgspec encodes it -- the exact bytes a decoder meets on
             the wire. Authored batches are serialized with msgspec so encoder-discretion
             corners (integer-width selection, str/bin family, omit-trailing-default) are the
             real ones, caught at the desk instead of on a live box.
  expect     the required outcome. For a well-formed payload, the normalized events a faithful
             decoder must produce (`_normalize` below is this repo's compact reference of the
             v0 model -- fan-out, token-drop, absent-or-null medium). For a malformed payload,
             the fail-closed error: which stage rejects it (decode = not valid msgpack;
             normalize = valid msgpack, invalid schema; any = either) and a substring its
             message must contain. The malformed rows pin fail-closed as a contract, not an
             accident -- hostile-input hardening landing inside the conformance suite.

Wire form (array_like + tag, v0.11.0):
    batch = [ts, [event, ...], data_parallel_rank]
    event = ["BlockStored", block_hashes, parent_block_hash, token_ids, block_size, lora_id,
             medium]   # tag-first; omit_defaults may drop the trailing medium
Regenerate (drift check when vLLM's schema bumps): `python generate.py`.
"""

import json
from pathlib import Path

import msgspec

SCHEMA_VERSION = "v0"
# The wire schema this corpus is pinned to, and the encoder that produced the bytes. When
# vLLM's event schema evolves, regeneration against a new msgspec/vLLM is the drift signal.
PRODUCER = {"vllm": "0.11.0", "event_schema": "vllm-0.11.0-1", "msgspec": msgspec.__version__}
SOURCE = "s0"

enc = msgspec.msgpack.encode

_I64_MAX = 2**63 - 1
_I64_MIN = -(2**63)


# --- the v0 reference normalize (this repo's own; independent of any agent) ----------------
def _normalize(batch):
    """Authored reference: vLLM wire batch -> the normalized events a conformer must emit.
    Token ids (event[3] of BlockStored) are dropped; a BlockStored fans out one event per
    hash; medium is absent-or-null -> None. An unknown tag is schema drift (raises)."""
    ts = batch[0]
    out = []
    for e in batch[1]:
        tag = e[0]
        if tag == "BlockStored":
            parent = e[2] if len(e) > 2 else None
            block_size = e[4]
            medium = e[6] if len(e) > 6 else None
            for h in e[1]:
                out.append({"type": "StoredBlock", "ts": ts, "block_hash": h,
                            "parent_hash": parent, "block_size": block_size, "medium": medium})
        elif tag == "BlockRemoved":
            medium = e[2] if len(e) > 2 else None
            for h in e[1]:
                out.append({"type": "RemovedBlock", "ts": ts, "block_hash": h, "medium": medium})
        elif tag == "AllBlocksCleared":
            medium = e[1] if len(e) > 1 else None
            out.append({"type": "ClearedAll", "ts": ts, "medium": medium})
        else:
            raise ValueError(f"unknown tag {tag!r}")
    return out


# --- well-formed rows: authored batch -> msgspec bytes + required events -------------------
# (name, note, batch)
_TOKENS12 = list(range(12))
VALID = [
    ("stored_fanout",
     "one BlockStored, three hashes; fans out to three StoredBlock, token ids dropped",
     [1.0, [["BlockStored", [10, 11, 12], None, _TOKENS12, 4, None, "GPU"]], 0]),
    ("stored_with_parent",
     "chained parent hash carried through; lora_id present but not in the normalized model",
     [1.0, [["BlockStored", [20], 7, _TOKENS12, 4, 3, "GPU"]], 0]),
    ("removed_multi",
     "one BlockRemoved, two hashes; fans out to two RemovedBlock",
     [2.0, [["BlockRemoved", [30, 31], "GPU"]], 0]),
    ("cleared_gpu",
     "AllBlocksCleared with an explicit medium",
     [3.0, [["AllBlocksCleared", "GPU"]], 0]),
    ("cleared_absent_medium",
     "AllBlocksCleared with medium omitted (omit_defaults) -> medium is null",
     [4.0, [["AllBlocksCleared"]], 0]),
    ("medium_null_explicit",
     "BlockStored full arity with medium encoded as nil -> null, same as absent",
     [1.0, [["BlockStored", [40], None, [], 4, None, None]], 0]),
    ("medium_absent_omitted",
     "BlockStored with trailing medium omitted (lora_id present) -> medium null",
     [1.0, [["BlockStored", [41], None, [], 4, 0]], 0]),
    ("int_width_boundaries",
     "hashes at every msgpack integer-family boundary; decode must widen faithfully, not clip",
     [1.0, [["BlockStored",
             [0, 127, 128, 2**31 - 1, 2**32, -1, -(2**31), _I64_MAX, _I64_MIN],
             None, [], 4, None, "GPU"]], 0]),
    ("empty_block_list",
     "BlockStored with an empty hash list -> zero events, not an error",
     [1.0, [["BlockStored", [], None, [], 4, None, "GPU"]], 0]),
    ("empty_events_batch",
     "a batch carrying no events -> zero events",
     [9.0, [], 0]),
    ("max_token_array",
     "block_size 64 with 64 token ids; the large token array is decoded then dropped",
     [1.0, [["BlockStored", [50], None, list(range(64)), 64, None, "GPU"]], 0]),
    ("multi_event_batch",
     "the wire carries batches; decode handles >1 event and preserves order",
     [7.0, [["BlockStored", [60, 61], None, [], 4, None, "GPU"],
            ["BlockRemoved", [60], "GPU"],
            ["AllBlocksCleared", "GPU"]], 0]),
    ("no_dp_rank",
     "data_parallel_rank omitted (batch arity 2); still a valid batch",
     [1.0, [["BlockStored", [70], None, [], 4, None, "GPU"]]]),
]


# --- malformed rows: raw bytes + the fail-closed error a conformer must raise --------------
# (name, note, wire_bytes, stage, contains)
def _malformed():
    full = enc([1.0, [["BlockStored", [10, 11, 12], None, [0, 1, 2], 4, None, "GPU"]], 0])
    return [
        ("truncated_payload", "half a valid payload; not valid msgpack",
         full[: len(full) // 2], "decode", "msgpack"),
        ("garbage_bytes", "0xc1 is never a valid msgpack byte", bytes([0xC1, 0xC1, 0xC1, 0xC1]),
         "decode", "msgpack"),
        ("top_level_string", "valid msgpack, but the payload is a string, not a batch array",
         enc("hello"), "normalize", "array"),
        ("events_not_array", "batch[1] is not an events array", enc([1.0, "nope", 0]),
         "normalize", "events"),
        ("hash_is_string", "a block hash is a string, not an integer",
         enc([1.0, [["BlockStored", ["deadbeef"], None, [], 4, None, "GPU"]], 0]),
         "normalize", "integer"),
        ("block_size_is_string", "block_size is a string, not an integer",
         enc([1.0, [["BlockStored", [1], None, [], "big", None, "GPU"]], 0]),
         "normalize", "block_size"),
        ("unknown_tag", "an event tag outside the pinned schema is drift, not a silent skip",
         enc([1.0, [["BlockTeleported", [1], "GPU"]], 0]), "normalize", "drift"),
        ("event_not_array", "an event is a bare string, not an array",
         enc([1.0, ["BlockStored"], 0]), "normalize", "array"),
        ("stored_short_arity", "BlockStored missing block_size (arity too short)",
         enc([1.0, [["BlockStored", [1], None, [1, 2]]], 0]), "normalize", "block_size"),
        ("bytes_family_hash_v1",
         "a bytes-family (sha256) hash is v1 scope; v0 fails closed rather than mis-decoding",
         enc([1.0, [["BlockStored", [b"\x00" * 32], None, [], 4, None, "GPU"]], 0]), "any", ""),
    ]


def build():
    rows = []
    for name, note, batch in VALID:
        rows.append({
            "schema_version": SCHEMA_VERSION, "producer": PRODUCER, "name": name, "note": note,
            "source": SOURCE, "wire_hex": enc(batch).hex(), "expect": {"events": _normalize(batch)},
        })
    for name, note, wire, stage, contains in _malformed():
        rows.append({
            "schema_version": SCHEMA_VERSION, "producer": PRODUCER, "name": name, "note": note,
            "source": SOURCE, "wire_hex": wire.hex(),
            "expect": {"error": {"stage": stage, "contains": contains}},
        })
    return rows


def write():
    d = Path(__file__).resolve().parent
    rows = build()
    for r in rows:
        (d / f"{r['name']}.json").write_text(json.dumps(r, indent=1) + "\n")
    manifest = {
        "schema_version": SCHEMA_VERSION, "producer": PRODUCER,
        "valid": [r["name"] for r in rows if "events" in r["expect"]],
        "malformed": [r["name"] for r in rows if "error" in r["expect"]],
    }
    (d / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    return d, len(rows)


if __name__ == "__main__":
    d, n = write()
    print(f"wrote {n} vllm-wire fixtures + manifest to {d}")
