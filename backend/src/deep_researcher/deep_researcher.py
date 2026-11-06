from deep_researcher.config import Configuration,get_config
from deep_researcher.llm.llm import MyLLM
from deep_researcher.models import SummaryState,SummaryStateOutput,TodoItem
from deep_researcher.services.note_service import NoteService
from deep_researcher.services.search import SearchService
from deep_researcher.agents.tool_aware_agent import ToolAwareAgent
from deep_researcher.prompts import todo_planner_system_prompt,summarizer_system_prompt,reporter_system_prompt
from deep_researcher.services.planner import PlanningService
from deep_researcher.services.summarizer import SummarizationService
from deep_researcher.services.reporter import ReportingService
from deep_researcher.agents.simple_agent import SimpleAgent
import logging 
from time import perf_counter
from copy import deepcopy


logger = logging.getLogger(__name__)


class DeepResearcher:
    """研究流程串行"""
    def __init__(
            self,
            config:Configuration|None=None
    )->None:
        """组装服务，llm客户端和笔记服务全流程共享"""
        self._llm = MyLLM()
        self._config = config or get_config()
        #初始化时不赋值，在实际研究开始时赋值，使DeepResearcher能够处理多个研究主题
        self._state : SummaryState| None = None
        self._note = NoteService(self._config)
        self._search = SearchService(self._config)

        self._planner_agent = ToolAwareAgent(
            name="Agent_for_planner",
            llm=self._llm,
            sys_prompt=todo_planner_system_prompt,
            config=self._config
        )

        self._planner = PlanningService(self._planner_agent)
        self._summarizer = SummarizationService(
             summarizer_factory=self._create_summarizer_agent,
             config=self._config,
             note_service=self._note)

        self._reporter = ReportingService(agent_factory=self._create_reporter_agent)
        logger.info(f"✅[DeepResearch]初始化成功，model_provider ={self._llm.llm_provider},model={self._llm.model}")

    def run(
              self,
              topic:str,
              max_tasks:int|None=None
    )->SummaryStateOutput:
        '''输入研究主题，进行深度调研'''
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError("研究主题必须是非空字符串")

        if max_tasks is not None and (
        type(max_tasks) is not int or max_tasks <= 0
        ):
            raise ValueError("max_tasks 必须是正整数或 None")

        start_at = perf_counter()
        state = SummaryState(research_topic=topic.strip())
        self._state = state

        logger.info(
             f"🚀[DeepResearcher] 研究开始:research_topic={topic}"
        )

        try:
             #规划阶段
            self._planner_agent.clear_history()
            try:
                 self._planner.plan_todo_list(state)
            finally:
                    self._planner_agent.clear_history()

            tasks = (state.todo_items if max_tasks is None 
                     else state.todo_items[:max_tasks])

            logger.info(
                f"🧭 研究计划已就绪:tasks= {[task.title for task in tasks]}"
            )

            #子任务执行阶段
            for index,task in enumerate(tasks,start=1):
                logger.info(f"📋 [DeepResearcher]研究进度:research_topic={topic},task_title={task.title}")
                self._execute_task(state,task.id)
            completed_count =sum(
                task.status=="completed" and bool(task.summary)
                for task in state.todo_items
            )
            if completed_count == 0:
                raise RuntimeError(
                    "本轮没有获得有效任务摘要，无法生成研究报告"
                )

            #报告生成阶段
            logger.info(
                f"📝 [DeepResearcher]开始汇总研究成果：research_topic={topic},completed=%d total=%d"
            )
            report = self._reporter.generate_report(state)
            report = self._append_scope_notice(state,report)

            state.structured_report = report
            state.running_summary = report

            self._note.save_report_note(state)
            logger.info(f"✅ [DeepResearcher]研究完成,research_topic={state.research_topic}")
            return SummaryStateOutput(
                running_summary=report,
                report_markdown=report,
                todo_items=deepcopy(state.todo_items),
            )

        except Exception as e:
            logger.exception(
                f"❌ 研究流程中断：{e}"
            )
            raise



    def _execute_task(self,state:SummaryState,task_id:int)->None:
         """执行单个任务"""
         task = state.get_task(task_id)
         task.status = "in_progress"

         try: 
              #搜索阶段
              self._search.search_task(state,task_id)
              context = state.search_contexts.get(task_id)
              if not context.strip():
                task.status = "skipped"
                task.notices.append("搜索未返回可用素材，本轮跳过总结")
                logger.warning(
                    f"⚠️ 任务已跳过： task_title={task.title} reason=搜索素材为空",
                )
                return
              #基于搜索文本进入子任务总结
              summary = self._summarizer.summarize_task(state,task_id)
              normalized =summary.strip().strip("「」“”\"'。.!！")
              if normalized == "暂无可用信息":
                task.status = "skipped"
                task.notices.append("总结未获得有效研究信息，未纳入最终报告")

                logger.warning(
                    "⚠️ 任务已跳过：task_title=%s task_id=%d reason=无有效研究信息",
                    task.title,
                    task_id,
                )
                return

              logger.info(
                f"✅ 任务执行完成：task_title={task.title}")

         except Exception as exc:
            task.status = "failed"
            task.notices.append(f"失败：{type(exc).__name__}")

            logger.exception(
                "❌ 任务执行失败，将继续其他任务："
                f"task_title={task.title}"
            )

    @staticmethod
    def _append_scope_notice(state: SummaryState, report: str) -> str:
        """为部分完成的研究添加确定性的范围说明，并随报告一起保存。"""
        unfinished: list[TodoItem] = [
            task
            for task in state.todo_items
            if task.status != "completed"
        ]

        if not unfinished:
            return report

        status_labels: dict[str, str] = {
            "failed": "执行失败",
            "skipped": "未获得有效研究资料",
            "pending": "本轮未执行",
            "in_progress": "尚未完成",
        }

        lines = [
            "## 研究范围说明",
            "",
            "本报告仅基于已完成任务的摘要生成，"
            "以下任务未纳入结论，相关问题仍需补充研究：",
            "",
        ]

        for task in unfinished:
            status = status_labels.get(task.status, task.status)
            lines.append(f"- 任务 {task.id}「{task.title}」：{status}。")

        return f"{report.rstrip()}\n\n" + "\n".join(lines)

              

              

         
            



























    def _create_summarizer_agent(self)->SimpleAgent:
        """summarizer的agent工厂函数"""
        return SimpleAgent(
            name="Agent_for_summarizer",
            llm=self._llm,
            sys_prompt=summarizer_system_prompt,
            config=self._config,
            enable_tool_call=False
        )

    def _create_reporter_agent(self)->SimpleAgent:
            """reporter的agent工厂函数"""
            return SimpleAgent(
                name="Agent_for_reporter",
                llm=self._llm,
                sys_prompt=reporter_system_prompt,
                config=self._config,
                enable_tool_call=False
            )