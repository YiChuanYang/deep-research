"""笔记工具使用指引生成器."""

from __future__ import annotations

from deep_researcher.models import TodoItem


def build_note_guidance(task: TodoItem) -> str:
    """指导模型记录实际研究内容，由服务层保证最终摘要保存."""
    if task.note_id:
        return f"""
【笔记协作】
当前任务编号：{task.id}，笔记 ID：{task.note_id}。
服务层已读取笔记，内容附在下面。
结合本轮素材补充、纠正旧结论，区分旧信息与新信息。
如需调用 note.update，content 必须包含真实研究内容。
不要为同一任务重复调用 create。
最终输出完整 Markdown 总结，系统会将最终总结和来源保存到笔记。
""".strip()

    return f"""
【笔记协作】
当前任务编号：{task.id}，尚无任务笔记。
先分析搜索素材，再输出完整的 Markdown 总结。
如果调用 note.create，必须填写实际研究结论，并包含：
- task_id: {task.id}
- title: 任务 {task.id}: {task.title}
- note_type: task_state
- tags: ["deep_research", "task_{task.id}"]

content 必须是研究内容，不能填写“稍后补充”“请记录”等操作说明。
同一任务最多 create 一次。
系统会在总结完成后保存最终正文和来源。
""".strip()
