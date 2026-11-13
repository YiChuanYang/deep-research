"""v2 回退兼容层：历史只读，禁止跨版本续跑，不改变新研究的算法。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from deep_researcher.api.errors import ApiError
from deep_researcher.api.manager import ResearchManager
from deep_researcher.graph.graph_nodes import ResearchNodes, build_graph
from tests.test_streaming import client_for, workflow


def test_v5_history_can_be_read_without_rewriting_checkpoint(tmp_path: Path) -> None:
    researcher, config, _ = workflow(tmp_path, ["# Report\n\nSupported fact [E0001]"])
    result = researcher.run("test")
    snapshot = researcher.read_run(result.run_id)
    payload = asdict(snapshot.state)
    payload["todo_items"][0]["review_status"] = "unavailable"
    before = deepcopy(payload)
    run_config = {"configurable": {"thread_id": result.run_id}}
    nodes = ResearchNodes(config, "test")
    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite3")) as saver:
        graph = build_graph(nodes, saver)
        graph.update_state(run_config, {"payload": payload}, as_node="save_report")
    restored = researcher.read_run(result.run_id)
    assert restored.report_ready and restored.state.structured_report == result.report_markdown
    assert not hasattr(restored.state.get_task(1), "review_status")
    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite3")) as saver:
        assert saver.get_tuple(run_config).checkpoint["channel_values"]["payload"] == before
    manager = ResearchManager(config, data_dir=tmp_path)
    try:
        with client_for(manager) as client:
            assert client.get(f"/api/research/{result.run_id}").status_code == 200
            response = client.get(f"/api/research/{result.run_id}/result")
            assert response.status_code == 200
            assert response.json()["report_markdown"] == result.report_markdown
    finally:
        manager.close()


def test_post_v2_resume_is_rejected_before_queueing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    researcher, config, _ = workflow(tmp_path, [])
    run_id = researcher.create_run("test")
    snapshot = researcher.read_run(run_id)
    snapshot = replace(snapshot, info=replace(snapshot.info, config={
        **snapshot.info.config, "enable_summary_reflection": True,
    }))
    manager = ResearchManager(config, data_dir=tmp_path)
    monkeypatch.setattr(manager, "get_snapshot", lambda _: snapshot)
    try:
        with pytest.raises(ApiError) as error:
            manager.resume(run_id)
        assert error.value.status_code == 409
        assert error.value.code == "incompatible_workflow_version"
        assert not manager._jobs
    finally:
        manager.close()
