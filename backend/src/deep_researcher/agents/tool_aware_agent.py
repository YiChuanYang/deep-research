from deep_researcher.agents.message import Message
from deep_researcher.agents.simple_agent import SimpleAgent
from deep_researcher.llm.llm import MyLLM
from deep_researcher.config import Configuration
from deep_researcher.tools.registry import ToolRegistry
from typing import List,Optional,Dict,Callable,Any
from deep_researcher.agents.message import Message
import json
import logging

logger = logging.getLogger(__name__)





class ToolAwareAgent(SimpleAgent):
    """带工具调用监听能力的增强版 Agent。

    在原生工具调用的基础上，增加：
    - 工具调用事件监听器，外部可追踪每次调用的详情
    - 参数自动清洗规范化，提升调用成功率
    - 监听器异常隔离，不影响主流程
    """
    def __init__(
                self,
                name:str,
                llm:MyLLM,
                sys_prompt:Optional[str]=None,
                config:Optional[Configuration]=None,
                tool_registry:Optional[ToolRegistry]=None,
                enable_tool_call:bool=False,
                tool_call_listener:Optional[Callable[Dict[str,Any],None]]=None)->None:
        """初始化 ToolAwareSimpleAgent。

                Args:
                    tool_call_listener: 工具调用监听器回调函数，接收包含工具调用信息的字典
                """
        super().__init__(name,llm,sys_prompt,config,tool_registry,enable_tool_call)
        self.tool_call_listener = tool_call_listener
        logger.info(f"✅[ToolAwareAgent]初始化成功,tool_call_listener={True if self.tool_call_listener else None}")
    def _execute_tool_call(self, call: Any) -> Message:
        """重写父类的execute_tool_call方法：执行工具+参数清洗+事件监听"""
        tool_name = call.function.name
        tool_call_id = call.id
        raw_parameters = call.function.arguments
        logger.info(f"[ToolAwareAgent]execute_tool_call正在执行工具调用，tool_name={tool_name},tool_call_id={tool_call_id}")
        success =True

        try:
            #解析并清洗参数
            parameters = json.loads(raw_parameters)
            clean_parameters = self._sanitize_parameters(parameters)

            #执行工具
            if self.tool_registry is None:
                raise ValueError("工具表未注册")
            observation = self.tool_registry.execute_tool(
                name=tool_name,
                parameters=clean_parameters)

            #将结果统一为字符串
            result = (
                observation if isinstance(observation,str)
                else json.dumps(observation,ensure_ascii=False)
            )
            logger.info(f"✅[ToolAwareAgent]工具调用成功，tool_name={tool_name},tool_call_id={tool_call_id}")
        except Exception as e:
            success =False
            logger.warning(f"❌[ToolAwareAgent]工具调用失败，tool_name={tool_name},tool_call_id={tool_call_id}，将返回失败原因{e}")
            result = json.dumps({
                "error_type":type(e).__name__,
                "error_message":str(e)
            })

        if self.tool_call_listener:
            event={
                "agent_name":self.name,
                "tool_call_id":tool_call_id,
                "tool_name":tool_name,
                "raw_parameters":raw_parameters,
                "clean_parameters":clean_parameters,
                "success":success,
                "result":result
            }

            try:
                self.tool_call_listener(event)
                logger.info(f"✅[ToolAwareAgent]工具监听成功，tool_name={tool_name},tool_call_id={tool_call_id}")
            except Exception as e:
                logger.info(f"❌[ToolAwareAgent]工具监听失败，tool_name={tool_name},tool_call_id={tool_call_id}")
            
        return Message(
            role="tool",
            tool_call_id=tool_call_id,
            content=result,
        )
 
    @staticmethod
    def _sanitize_parameters(parameters:Dict[str,Any])->Dict[str,Any]:
        """对参数进行清洗，目前暂时处理空值默认和字符串去空格"""
        sanitized :Dict[str,Any]={}
        for key,value in parameters.items():
            if value is None:
                sanitized[key]=value
                continue
            if isinstance(value,str):
                sanitized[key]=value.strip()

        return sanitized

    @staticmethod
    def attach_registry(
        agent:'ToolAwareAgent',
        registry:Optional[ToolRegistry]
    )->None:
        """动态挂载注册表"""
        if registry is None:
            return
        agent.tool_registry =registry
        agent.enable_tool_call =True


    