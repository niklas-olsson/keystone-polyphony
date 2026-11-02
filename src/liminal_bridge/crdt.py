import uuid
from typing import Dict, Any, Set, Tuple
from abc import ABC, abstractmethod


class CRDT(ABC):
    @abstractmethod
    def merge(self, other: "CRDT") -> "CRDT":
        """Merges another CRDT into this one, returning the updated self."""
        pass

    @abstractmethod
    def value(self) -> Any:
        """Returns the current value of the CRDT."""
        pass

    @abstractmethod
    def to_dict(self) -> Dict[str, Any]:
        """Serializes the CRDT state to a dictionary."""
        pass

    @classmethod
    @abstractmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CRDT":
        """Deserializes the CRDT state from a dictionary."""
        pass


def compare_vcs(vc1: Dict[str, int], vc2: Dict[str, int]) -> int:
    """
    Compares two vector clocks.
    Returns:
        1 if vc1 > vc2 (vc1 is causally after vc2)
       -1 if vc2 > vc1 (vc2 is causally after vc1)
        0 if concurrent or equal
    """
    all_keys = set(vc1.keys()) | set(vc2.keys())

    # Check if vc1 >= vc2
    vc1_ge_vc2 = True
    vc1_gt_vc2 = False

    # Check if vc2 >= vc1
    vc2_ge_vc1 = True
    vc2_gt_vc1 = False

    for k in all_keys:
        v1 = vc1.get(k, 0)
        v2 = vc2.get(k, 0)

        if v1 < v2:
            vc1_ge_vc2 = False
        if v1 > v2:
            vc1_gt_vc2 = True

        if v2 < v1:
            vc2_ge_vc1 = False
        if v2 > v1:
            vc2_gt_vc1 = True

    if vc1_ge_vc2 and vc1_gt_vc2:
        return 1
    if vc2_ge_vc1 and vc2_gt_vc1:
        return -1
    return 0


class Register(CRDT):
    """
    Register CRDT with pluggable tiebreaker logic.
    Uses Vector Clocks for causal ordering.
    If concurrent, uses a provided resolve function, or falls back to
    timestamps for concurrency resolution, and Origin Node ID as a final tie-breaker.
    """

    def __init__(self, value: Any, timestamp: float, origin: str, vc: Dict[str, int]):
        self._value = value
        self.timestamp = timestamp
        self.origin = origin
        self.vc = vc

    def _lww(self, other: "Register"):
        # Legacy LWW tie-break using timestamp and origin
        if other.timestamp > self.timestamp:
            self._value = other._value
            self.timestamp = other.timestamp
            self.origin = other.origin
            self.vc = other.vc
        elif other.timestamp == self.timestamp:
            if other.origin > self.origin:
                self._value = other._value
                self.timestamp = other.timestamp
                self.origin = other.origin
                self.vc = other.vc
        return self

    def merge(self, other: "Register", *, resolve=None) -> "Register":
        if not isinstance(other, Register):
            raise ValueError("Cannot merge with non-Register")

        cmp = compare_vcs(self.vc, other.vc)

        if cmp == -1:
            # Other is newer (causally after self)
            self._value = other._value
            self.timestamp = other.timestamp
            self.origin = other.origin
            self.vc = other.vc
        elif cmp == 0:
            # Concurrent or equal
            if resolve:
                winner_val = resolve(self.value(), other.value())
                self._value = winner_val
                # Deterministically advance metadata past both
                if other.timestamp > self.timestamp or (other.timestamp == self.timestamp and other.origin > self.origin):
                    self.origin = other.origin
                self.timestamp = max(self.timestamp, other.timestamp)
                # Point-wise max of vector clocks
                merged_vc = dict(self.vc)
                for k, v in other.vc.items():
                    merged_vc[k] = max(merged_vc.get(k, 0), v)
                self.vc = merged_vc
            else:
                self._lww(other)

        # If cmp == 1 (self is newer), do nothing
        return self

    def value(self) -> Any:
        return self._value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "register",
            "value": self._value,
            "timestamp": self.timestamp,
            "origin": self.origin,
            "vc": self.vc,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Register":
        return cls(
            value=data["value"],
            timestamp=data["timestamp"],
            origin=data["origin"],
            vc=data["vc"],
        )


class PNCounter(CRDT):
    """
    Positive-Negative Counter.
    Supports increment and decrement operations.
    State:
        p: Dict[node_id, count] (positive counts)
        n: Dict[node_id, count] (negative counts)
    """

    def __init__(self, p: Dict[str, int] = None, n: Dict[str, int] = None):
        self.p = p or {}
        self.n = n or {}

    def inc(self, node_id: str, amount: int = 1):
        if amount < 0:
            raise ValueError("Amount must be non-negative")
        self.p[node_id] = self.p.get(node_id, 0) + amount

    def dec(self, node_id: str, amount: int = 1):
        if amount < 0:
            raise ValueError("Amount must be non-negative")
        self.n[node_id] = self.n.get(node_id, 0) + amount

    def merge(self, other: "PNCounter") -> "PNCounter":
        if not isinstance(other, PNCounter):
            raise ValueError("Cannot merge with non-PNCounter")

        # Merge P
        for node, count in other.p.items():
            self.p[node] = max(self.p.get(node, 0), count)

        # Merge N
        for node, count in other.n.items():
            self.n[node] = max(self.n.get(node, 0), count)

        return self

    def value(self) -> int:
        return sum(self.p.values()) - sum(self.n.values())

    def to_dict(self) -> Dict[str, Any]:
        return {"type": "pn-counter", "p": self.p, "n": self.n}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PNCounter":
        return cls(p=data.get("p"), n=data.get("n"))


class GSet(CRDT):
    """
    Grow-only Set.
    Elements can only be added, never removed.
    """

    def __init__(self, elements: Set[Any] = None):
        self.elements = elements or set()

    def add(self, element: Any):
        # We need elements to be hashable
        self.elements.add(element)

    def merge(self, other: "GSet") -> "GSet":
        if not isinstance(other, GSet):
            raise ValueError("Cannot merge with non-GSet")
        self.elements.update(other.elements)
        return self

    def value(self) -> Set[Any]:
        return self.elements.copy()

    def to_dict(self) -> Dict[str, Any]:
        return {"type": "g-set", "elements": list(self.elements)}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GSet":
        return cls(elements=set(data.get("elements", [])))


class ORSet(CRDT):
    """
    Observed-Remove Set.
    Elements can be added and removed.
    State:
        elements: Dict[element, Set[uuid]]
        tombstones: Set[uuid] (optimized away in some implementations, but here we use Add-Wins)

    Implementation:
    We track unique tags (UUIDs) for each added element.
    Add(e): generate new tag t, add (e, {t}) to state.
    Remove(e): remove all tags associated with e from state.
    Merge: Union of elements. If an element exists in both, union the tags.
           However, standard OR-Set usually needs tombstones or observed-set.
           Wait, a state-based OR-Set usually represents the set as:
           S = {(e, u) | e in E, u in UniqueTags}
           Add(e): S = S U {(e, new_u)}
           Remove(e): S = S - {(e, u) | (e, u) in S}
           Merge(S1, S2): (S1 U S2) - ( (O1 - S1) U (O2 - S2) ) where O is observed?

           Actually, the standard State-based OR-Set requires keeping the set of "observed" adds
           or using version vectors.

           A simpler approach without Version Vectors is keeping two sets: Added and Removed.
           A = set of (element, uuid)
           R = set of (element, uuid)
           Value = {e | exists u s.t. (e, u) in A and (e, u) not in R}
           Merge: A = A1 U A2, R = R1 U R2.
           This is actually a 2P-Set where elements are unique (element + uuid).
           But we want to re-add elements.
           Yes, (e, u1) is different from (e, u2).
           So if I add 'x' (gets u1), then remove 'x' (adds (x, u1) to R),
           then add 'x' again (gets u2), it is in value because (x, u2) is in A but not R.
    """

    def __init__(
        self, added: Set[Tuple[Any, str]] = None, removed: Set[Tuple[Any, str]] = None
    ):
        self.added = added or set()
        self.removed = removed or set()

    def add(self, element: Any):
        try:
            hash(element)
        except TypeError:
            raise TypeError(
                f"Element must be hashable. Got {type(element)}. Consider serializing to JSON string."
            )

        u = str(uuid.uuid4())
        self.added.add((element, u))

    def remove(self, element: Any):
        # Find all instances of element in added that are NOT in removed
        to_remove = [
            item
            for item in self.added
            if item[0] == element and item not in self.removed
        ]
        self.removed.update(to_remove)

    def merge(self, other: "ORSet") -> "ORSet":
        if not isinstance(other, ORSet):
            raise ValueError("Cannot merge with non-ORSet")
        self.added.update(other.added)
        self.removed.update(other.removed)
        return self

    def value(self) -> Set[Any]:
        # Return elements that are in added but not in removed
        return {item[0] for item in self.added if item not in self.removed}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "or-set",
            "added": list(self.added),
            "removed": list(self.removed),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ORSet":
        # Tuples are serialized as lists in JSON, so we need to convert back to tuples
        added = {tuple(x) for x in data.get("added", [])}
        removed = {tuple(x) for x in data.get("removed", [])}
        return cls(added=added, removed=removed)


class RevisionLog(CRDT):
    """
    A Grow-only CRDT representing an append-only log of code or state revisions.
    State:
        revisions: Set[Tuple[float, str, str, str]]
            Each tuple is (timestamp, author, revision_id, unified_diff_text).
            Using a UUID/revision_id alongside timestamp prevents collisions.
    """

    def __init__(self, revisions: Set[Tuple[float, str, str, str]] = None):
        self.revisions = revisions if revisions is not None else set()

    def append(self, timestamp: float, author: str, revision_id: str, diff_text: str):
        self.revisions.add((timestamp, author, revision_id, diff_text))

    def merge(self, other: "RevisionLog") -> "RevisionLog":
        if not isinstance(other, RevisionLog):
            raise ValueError("Cannot merge with non-RevisionLog")
        self.revisions.update(other.revisions)
        return self

    def value(self) -> list[Dict[str, Any]]:
        # Return a chronologically sorted list of revisions
        sorted_revs = sorted(list(self.revisions), key=lambda x: x[0])
        return [
            {
                "timestamp": rev[0],
                "author": rev[1],
                "revision_id": rev[2],
                "diff": rev[3],
            }
            for rev in sorted_revs
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "revision-log",
            "revisions": list(self.revisions),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RevisionLog":
        revisions = {tuple(x) for x in data.get("revisions", [])}
        return cls(revisions=revisions)


# Backwards compatibility alias
LWWRegister = Register
