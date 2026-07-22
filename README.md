# kv-policy-core

The neutral policy interface both the benchmark and the engines depend on: `Port`,
`Policy`, `BlockMeta`, `Decision`, and the reference `SimPort`. Depends on nothing,
so an engine can implement the interface and a benchmark can score policies without
either depending on the other.
