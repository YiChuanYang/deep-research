"""离线验证引用规范化，不调用模型或搜索服务。"""

from __future__ import annotations

from deep_researcher.logging_config import log_context, run_cli
from deep_researcher.models import Evidence, Source, SummaryState, TodoItem
from deep_researcher.services.evidence_service import CitationError, EvidenceService


def build_state() -> SummaryState:
    """构造三个真实存在于演示状态中的证据。"""
    state = SummaryState(
        run_id="a" * 32,
        research_topic="引用规范化离线验证",
    )
    state.todo_items = [
        TodoItem(
            id=1,
            title="验证引用",
            intent="检查格式修复和允许范围",
            query="离线演示",
            evidence_ids=["E0001", "E0002", "E0003"],
        )
    ]
    state.sources["S0001"] = Source(
        source_id="S0001",
        url="https://example.com/demo",
        title="演示来源",
    )
    for number in range(1, 4):
        # :04d 表示整数至少显示四位，不足时左侧补零。
        evidence_id = f"E{number:04d}"
        state.evidence[evidence_id] = Evidence(
            evidence_id=evidence_id,
            source_id="S0001",
            task_id=1,
            text=f"第 {number} 条演示材料。",
            retrieved_at="2026-11-12T00:00:00+00:00",
            query="离线演示",
            backend="demo",
            kind="search_snippet",
        )
    return state


def main() -> None:
    state = build_state()
    service = EvidenceService()
    # 第三个证据存在，但本次不允许使用，用来验证允许范围没有被放宽。
    allowed = ["E0001", "E0002"]
    accepted: list[tuple[str, str]] = [
        ("发现一 [E0001, E0002]", "发现一 [E0001][E0002]"),
        ("发现二 [E0001，E0002]", "发现二 [E0001][E0002]"),
        ("发现三 [E0002,E0001]", "发现三 [E0002][E0001]"),
        ("原本正确 [E0001][E0002]", "原本正确 [E0001][E0002]"),
    ]
    rejected = [
        "未知编号 [E0001,E9999]",
        "超出本次允许范围 [E0001,E0003]",
        "不能擅自展开范围 [E0001-E0002]",
        "不能补全缺失字母 [E0001,0002]",
        "不能补零 [E1,E2]",
        "暂不支持分号 [E0001;E0002]",
        "完全没有引用",
        "正文包含URL [E0001,E0002] https://example.com",
    ]
    with log_context(run_id=state.run_id, task_id=1, stage="引用规范化验证"):
        print("一、应当成功的情况")
        for original, expected in accepted:
            result = service.prepare_text(state, original, allowed, document="summary")
            assert result.text == expected
            print("输入：", original)
            print("输出：", result.text)
            print("编号：", result.citation_ids)
            print()
            # 幂等性：已经规范化的正文再处理一次，结果不继续变化。
            assert service.prepare_text(state, result.text, allowed) == result

        print("二、应当拒绝的情况")
        for original in rejected:
            try:
                service.prepare_text(state, original, allowed)
            except CitationError as exc:
                print(f"正确拒绝：{original}\n原因：{exc}\n")
            else:
                # try 没有抛异常才执行 else；这些样例必须被拒绝。
                raise AssertionError(f"错误地接受了：{original}")

        print("三、原有严格校验不能被放宽")
        try:
            service.validate(state, "原始错误格式 [E0001, E0002]", allowed)
        except CitationError:
            print("通过：直接调用 validate 仍会拒绝合写")
        else:
            raise AssertionError("validate 不应自动接受合写引用")

        # 证据服务只返回结果，不推进研究状态。
        assert state.get_task(1).summary is None
        assert state.structured_report is None
        print("\n全部离线检查通过。")


if __name__ == "__main__":
    run_cli(main)
