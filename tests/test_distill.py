#!/usr/bin/env python3
"""The distiller's one piece of logic (candidate flagging) tested without a broker."""
import importlib.util
import os

_path = os.path.join(os.path.dirname(__file__), "..", "scripts", "distill.py")
_spec = importlib.util.spec_from_file_location("distill", _path)
distill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(distill)


def test_flag_candidates_drops_cited_and_groups_by_thread():
    msgs = [{"id": 1, "thread_id": 10}, {"id": 2, "thread_id": 10},
            {"id": 3, "thread_id": 11}]
    got = distill.flag_candidates(msgs, cited={2})
    assert set(got) == {10, 11}
    assert [m["id"] for m in got[10]] == [1]
    assert [m["id"] for m in got[11]] == [3]
    assert distill.flag_candidates(msgs, cited={1, 2, 3}) == {}
