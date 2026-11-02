from src.liminal_bridge.crdt import Register, PNCounter, GSet, ORSet, compare_vcs


def test_compare_vcs():
    # v1 > v2
    v1 = {"a": 2, "b": 1}
    v2 = {"a": 1, "b": 1}
    assert compare_vcs(v1, v2) == 1
    assert compare_vcs(v2, v1) == -1

    # v1 > v2 (missing key treated as 0)
    v1 = {"a": 1}
    v2 = {}
    assert compare_vcs(v1, v2) == 1
    assert compare_vcs(v2, v1) == -1

    # concurrent
    v1 = {"a": 1, "b": 0}
    v2 = {"a": 0, "b": 1}
    assert compare_vcs(v1, v2) == 0
    assert compare_vcs(v2, v1) == 0

    # equal
    v1 = {"a": 1}
    v2 = {"a": 1}
    assert compare_vcs(v1, v2) == 0


def test_lww_register_merge():
    # Scenario 1: Causality
    r1 = Register("old", 100, "A", {"A": 1})
    r2 = Register("new", 100, "B", {"A": 2})  # causally after r1

    r1.merge(r2)
    assert r1.value() == "new"
    assert r1.vc == {"A": 2}

    # Scenario 2: Concurrent (timestamp wins)
    r1 = Register("val1", 100, "A", {"A": 1})
    r2 = Register("val2", 200, "B", {"B": 1})  # concurrent

    r1.merge(r2)
    assert r1.value() == "val2"
    assert r1.timestamp == 200

    # Scenario 3: Concurrent (timestamp tie, origin wins)
    r1 = Register("valA", 100, "A", {"A": 1})
    r2 = Register("valB", 100, "B", {"B": 1})  # concurrent

    # B > A
    r1.merge(r2)
    assert r1.value() == "valB"
    assert r1.origin == "B"

    # Reverse merge
    r1 = Register("valA", 100, "A", {"A": 1})
    r2 = Register("valB", 100, "B", {"B": 1})  # concurrent

    r2.merge(r1)  # r1 (A) < r2 (B)
    assert r2.value() == "valB"


def test_pn_counter():
    c1 = PNCounter()
    c1.inc("A", 10)
    c1.dec("A", 2)
    assert c1.value() == 8

    c2 = PNCounter()
    c2.inc("B", 5)

    c1.merge(c2)
    assert c1.value() == 13  # 8 + 5

    # Test idempotent merge
    c1.merge(c2)
    assert c1.value() == 13

    # Test concurrent updates
    c3 = PNCounter()
    c3.inc("A", 5)  # concurrent with c1's inc(A, 10)
    # c1 has P[A]=10, c3 has P[A]=5. Max is 10.
    c1.merge(c3)
    assert c1.p["A"] == 10
    assert c1.value() == 13  # Still 13 because B is 5

    # But if c3 incremented B?
    c4 = PNCounter()
    c4.inc("B", 10)  # B was 5
    c1.merge(c4)
    assert c1.p["B"] == 10
    assert c1.value() == 18  # 8 (from A) + 10 (from B)


def test_g_set():
    s1 = GSet({"a", "b"})
    s2 = GSet({"b", "c"})

    s1.merge(s2)
    assert s1.value() == {"a", "b", "c"}


def test_or_set():
    s1 = ORSet()
    s1.add("x")
    s1.add("y")

    s2 = ORSet()
    s2.merge(s1)
    assert s2.value() == {"x", "y"}

    s2.remove("x")
    assert s2.value() == {"y"}

    # s1 still has x
    assert s1.value() == {"x", "y"}

    # Merge s2 into s1 (s2 removed x)
    s1.merge(s2)
    assert s1.value() == {
        "y"
    }  # x should be removed because s2 observed it and removed it

    # Concurrent add: s3 adds x again
    s3 = ORSet()
    s3.add("x")  # New UUID

    s1.merge(s3)
    assert "x" in s1.value()
    assert "y" in s1.value()


def test_serialization():
    # LWW
    r = Register("test", 123.456, "node1", {"node1": 1})
    d = r.to_dict()
    r2 = Register.from_dict(d)
    assert r2.value() == r.value()
    assert r2.vc == r.vc

    # PNCounter
    c = PNCounter({"a": 1}, {"b": 2})
    d = c.to_dict()
    c2 = PNCounter.from_dict(d)
    assert c2.value() == -1

    # ORSet
    s = ORSet()
    s.add("test")
    d = s.to_dict()
    s2 = ORSet.from_dict(d)
    assert s2.value() == {"test"}
