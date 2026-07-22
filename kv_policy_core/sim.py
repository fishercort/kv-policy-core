"""The reference Port: SimPort, a plain in-memory implementation of the Port seam.

A spec ships with its reference implementation. SimPort is the canonical semantics
an engine's Port (e.g. miniserve's MiniservePort) must reproduce: it owns a resident
set as a dict and applies eviction decisions directly. It depends only on the core
interface, so both the benchmark (which scores on top of it) and an engine's
conformance test (which compares its Port against it) can use it without depending
on each other.
"""

from kv_policy_core.interfaces import BlockMeta, CacheView, Policy


class SimPort:
    """Reference Port over an in-memory resident set. `mode` is enforce (decisions
    enacted) or advisory (recorded but not enacted) — the shadow-before-enforce
    switch."""

    def __init__(self, capacity_tokens: int, mode: str = "enforce"):
        self.capacity_tokens = capacity_tokens
        self.mode = mode
        self._resident: dict = {}
        self.view = CacheView(self._resident)
        self.resident_tokens = 0
        self.evictions = 0
        self._policy: Policy | None = None

    def bind_policy(self, policy: Policy) -> None:
        self._policy = policy
        policy.bind(self.view)

    def set_protected(self, working: frozenset) -> None:
        self.view._set_protected(working)

    def resident(self) -> dict:
        return self._resident

    def get(self, block_id):
        return self._resident.get(block_id)

    def admit(self, meta: BlockMeta) -> None:
        self._resident[meta.block_id] = meta
        self.resident_tokens += meta.size_tokens

    def _reap(self, vid) -> None:
        m = self._resident.pop(vid)
        self.resident_tokens -= m.size_tokens
        self.evictions += 1

    def reap_if_resident(self, vid, protected: frozenset) -> None:
        """Proactive (maintain) reap: only if resident and not currently needed."""
        if vid in self._resident and vid not in protected:
            self._reap(vid)

    def free_to_fit(self, needed_tokens: int, now_ms: int, protected: frozenset) -> None:
        while self.capacity_tokens - self.resident_tokens < needed_tokens:
            evictable = sum(
                m.size_tokens for b, m in self._resident.items() if b not in protected
            )
            if evictable < needed_tokens - (self.capacity_tokens - self.resident_tokens):
                raise RuntimeError(
                    "request working set exceeds capacity: its prefix cannot be "
                    "held resident even after evicting everything evictable"
                )
            victims = self._policy.evict(
                needed_tokens - (self.capacity_tokens - self.resident_tokens), now_ms
            )
            if not victims:
                raise RuntimeError(
                    "policy.evict returned no victims but space is needed; "
                    "a correct policy must free space or the request cannot be admitted"
                )
            for vid in victims:
                if vid in protected or vid not in self._resident:
                    raise RuntimeError(
                        f"policy tried to evict block {vid}, which is "
                        f"{'needed by the current request' if vid in protected else 'not resident'}"
                    )
                self._reap(vid)

    def now_ms(self) -> int:  # sim time comes from the trace; unused by the seam
        return 0
