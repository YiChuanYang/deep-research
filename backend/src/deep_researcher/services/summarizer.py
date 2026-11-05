from deep_researcher.models import TodoItem,SummaryState
from deep_researcher.agents.tool_aware_agent import ToolAwareAgent
from deep_researcher.prompts import summarizer_running_prompt
from typing import Callable,Optional
from deep_researcher.config import Configuration
import logging 
from deep_researcher.utils import strip_thinking_tokens
from deep_researcher.services.note_guidance import build_note_guidance


logger = logging.getLogger(__name__)
class SumarizationService:
    """任务总结服务"""
    def __init__(
            self,
            summarizer_factory:Callable[[],ToolAwareAgent],
            config:Configuration
    )->None:
        self._agent_factory = summarizer_factory
        self.config = config
        logger.info(f"✅[SummarizationService]初始化成功")

    def summarize_task(self,task:TodoItem,state:SummaryState,context:str)->str:
        """针对单个TodoItem,基于搜索上下文生成Markdown总结"""
        prompt = self._build_prompt(state=state,task=task,context=context)
        agent = self._agent_factory()
        try:

            response = agent.run(input_text=prompt)
            logger.info(f"✅[SummarizationService]总结成功,task_id ={task.id}")

        finally:
            agent.clear_history()


        #输出清洗
        summary_text = response.strip()
        if self.config.strip_thinking_tokens:
            summary_text = strip_thinking_tokens(summary_text)
            
        return summary_text
    

    def _build_prompt(self,state:SummaryState,task: TodoItem,context:str)->str:
        """构建总结提示词"""
        summary_prompt = summarizer_running_prompt.format(
        research_topic=state.research_topic,
        title=task.title,
        intent=task.intent,
        query=task.query,
        context=context,
    )
        return f"{summary_prompt}\n\n{build_note_guidance(task)}"