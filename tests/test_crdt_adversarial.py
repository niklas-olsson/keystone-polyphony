import pytest
from src.liminal_bridge.crdt import Register

def test_resolve_commutativity():
    r1 = Register("valA", 100, "A", {"A": 1})
    r2 = Register("valB", 100, "B", {"B": 1})

    def my_resolve(a, b):
        return max(a, b)

    r1_copy = Register(r1.value(), r1.timestamp, r1.origin, dict(r1.vc))
    r2_copy = Register(r2.value(), r2.timestamp, r2.origin, dict(r2.vc))

    # merge r2 into r1
    r1.merge(r2, resolve=my_resolve)

    # merge r1 into r2
    r2_copy.merge(r1_copy, resolve=my_resolve)

    assert r1.value() == r2_copy.value()
    assert r1.timestamp == r2_copy.timestamp
    assert r1.origin == r2_copy.origin
    assert r1.vc == r2_copy.vc

def test_resolve_overrides_lww():
    r1 = Register("late_loser", 200, "A", {"A": 1})
    r2 = Register("early_winner", 100, "B", {"B": 1})

    def my_resolve(a, b):
        return "early_winner"

    r1.merge(r2, resolve=my_resolve)
    assert r1.value() == "early_winner"
    assert r1.timestamp == 200
    assert r1.origin == "A"
    assert r1.vc == {"A": 1, "B": 1}
