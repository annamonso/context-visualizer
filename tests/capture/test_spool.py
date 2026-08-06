"""Path-A capture spool.

The spool is the durability boundary between an instrumented agent and
ChronoLog: if a record is lost or a session id is mangled here, no downstream
view can recover it. Two properties matter most — append-only ordering with
monotonic sequence ids, and exact preservation of the ``role@scenario`` id.
"""

from chronolog_observability.capture.spool import (
    KIND_CONTEXT_GRAPH,
    KIND_INTERACTIONS,
    CaptureSpool,
    _safe,
)


def test_interactions_round_trip_in_write_order(tmp_path):
    spool = CaptureSpool(tmp_path)
    for i in range(5):
        spool.record_interaction("agent@scn", {"prompt": f"p{i}", "response": f"r{i}"})

    got = spool.read("agent@scn", KIND_INTERACTIONS)
    assert [r["prompt"] for r in got] == [f"p{i}" for i in range(5)]


def test_sequence_ids_are_monotonic_and_dense(tmp_path):
    spool = CaptureSpool(tmp_path)
    seqs = [spool.record_interaction("a@s", {"prompt": str(i)}) for i in range(4)]
    assert seqs == sorted(seqs), "sequence ids must increase"
    assert len(set(seqs)) == 4, "sequence ids must be unique"


def test_sequence_continues_across_spool_instances(tmp_path):
    """A restarted capture process must not reuse sequence ids.

    Reuse would make the sync worker's high-water-mark dedup drop real records.
    """
    first = CaptureSpool(tmp_path)
    s0 = first.record_interaction("a@s", {"prompt": "before restart"})

    reopened = CaptureSpool(tmp_path)
    s1 = reopened.record_interaction("a@s", {"prompt": "after restart"})

    assert s1 > s0, f"sequence restarted: {s0} then {s1}"


def test_session_id_keeps_its_at_sign(tmp_path):
    """``role@scenario`` must survive verbatim.

    Mapping ``@`` to ``_`` desynchronised the stored ChronoLog story name from
    the id the read path looks up, so clicked scenario nodes showed zero tokens
    and an empty conversation. Guard the fix.
    """
    assert _safe("planner@thesis-demo") == "planner@thesis-demo"

    spool = CaptureSpool(tmp_path)
    spool.record_interaction("planner@thesis-demo", {"prompt": "x"})
    assert "planner@thesis-demo" in spool.list_sessions()


def test_path_separators_cannot_escape_the_spool_directory(tmp_path):
    """A session id is attacker-influenced; it must not traverse directories."""
    hostile = "../../etc/passwd"
    assert "/" not in _safe(hostile)

    spool = CaptureSpool(tmp_path)
    spool.record_interaction(hostile, {"prompt": "x"})
    written = list(tmp_path.rglob("*.jsonl"))
    assert written, "nothing was written"
    for p in written:
        assert p.parent == tmp_path, f"escaped the spool root: {p}"


def test_kinds_are_stored_separately(tmp_path):
    spool = CaptureSpool(tmp_path)
    spool.record_interaction("a@s", {"prompt": "an interaction"})
    spool.record_context_node("a@s", {"op": "add", "node": "a context node"})

    interactions = spool.read("a@s", KIND_INTERACTIONS)
    context = spool.read("a@s", KIND_CONTEXT_GRAPH)

    assert len(interactions) == 1 and len(context) == 1
    assert "prompt" in interactions[0] and "op" in context[0]


def test_sessions_are_isolated_from_each_other(tmp_path):
    spool = CaptureSpool(tmp_path)
    spool.record_interaction("a@s", {"prompt": "mine"})
    spool.record_interaction("b@s", {"prompt": "theirs"})

    assert [r["prompt"] for r in spool.read("a@s", KIND_INTERACTIONS)] == ["mine"]
    assert set(spool.list_sessions()) == {"a@s", "b@s"}


def test_reading_an_unknown_session_is_empty_not_an_error(tmp_path):
    assert CaptureSpool(tmp_path).read("never-written", KIND_INTERACTIONS) == []


def test_list_sessions_on_a_fresh_spool_is_empty(tmp_path):
    assert CaptureSpool(tmp_path / "does-not-exist-yet").list_sessions() == []
