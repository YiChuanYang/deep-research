"""笔记工具使用指引生成器"""
from __future__ import annotations

from deep_researcher.models import TodoItem


def build_note_guidance(task: TodoItem) -> str:
    """生成适配原生 tool call 的笔记协作指引

    Args:
        task: 当前任务对象

    Returns:
        提示词片段，可直接拼入总结指令
    """
    tags_list = ["deep_research", f"task_{task.id}"]

    if task.note_id:
        return f"""
【笔记协作规则】
1.  当前任务已有笔记，ID：{task.note_id}
2.  生成总结前，必须调用 `note` 工具、action=read，读取最新笔记内容
3.  完成分析后，调用 `note` 工具、action=update，把本轮新增的要点增量追加到笔记中
4.  更新要求：
    - 保留原有段落结构，新增内容追加在末尾，用分隔线区分
    - tags 必须包含 {tags_list}，方便其他 Agent 检索
5.  笔记同步完成后，再输出面向用户的 Markdown 总结
""".strip()

    return f"""
【笔记协作规则】
1.  当前任务尚未建立笔记，请先调用 `note` 工具、action=create 创建笔记
2.  创建参数说明：
    - task_id: {task.id}
    - title: 任务 {task.id}: {task.title}
    - note_type: task_state
    - tags: {tags_list}
    - content: 先写入任务概览、检索目标，后续再补充总结
3.  创建成功后记录返回的 note_id，后续所有更新都复用这个 ID
4.  笔记同步完成后，再输出面向用户的总结
""".strip()
