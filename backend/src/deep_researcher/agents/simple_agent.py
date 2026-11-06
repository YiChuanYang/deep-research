from deep_researcher.llm.llm import MyLLM
from deep_researcher.agents.message import Message
from deep_researcher.config import Configuration
from deep_researcher.agents.agent import Agent
from deep_researcher.tools.registry import ToolRegistry
from typing import Optional,Dict,Any,List
import logging
import json
from openai.types.chat import ChatCompletionMessageFunctionToolCall
logger = logging.getLogger(__name__)




class SimpleAgent(Agent):
    def __init__(
            self,
            name:str,
            llm:MyLLM,
            sys_prompt:Optional[str]=None,
            config:Optional[Configuration]=None,
            tool_registry:Optional[ToolRegistry]=None,
            enable_tool_call:bool=False
    )->None:
        super().__init__(name,llm,sys_prompt,config)
        self.tool_registry = tool_registry 
        self.enable_tool_call = enable_tool_call and self.tool_registry is not None
        logger.info(
            "🤖 Agent 已初始化：agent=%s provider=%s tool_call=%s",
            self.name,
            self.llm.llm_provider,
            self.enable_tool_call,
        )



    def _build_messages(self)->List[Dict[str,Any]]:
        """构造消息列表"""
        return [
            {'role':'system',
             'content':self.sys_prompt},
             *[
                 message.to_dict() for message in self._history
             ]
        ]
    
    def _get_tool_openai_schema(self)->List[Dict[str,Any]]:
        """通过注册表，返回已注册工具的openai tool call格式schema"""
        if not self.enable_tool_call or self.tool_registry is None:
            return []
        return self.tool_registry.get_openai_tools()

    def _execute_tool_call(
            self,
            call:Any,
            )->Message:
        name = call.function.name
        tool_call_id =call.id
        raw_parameters = call.function.arguments

        try:
            parameters = json.loads(raw_parameters)
            if not self.tool_registry:
                raise ValueError("工具注册表为空，无法执行工具调用")
            result = self.tool_registry.execute_tool(
                name=name,
                parameters = parameters
            )
            content = result if isinstance(result,str) else json.dumps(result, ensure_ascii=False)

        except Exception as e:
            logger.exception(
                "❌ Agent 工具调用失败：tool=%s call_id=%s error_type=%s",
                name,
                tool_call_id,
                type(e).__name__,
            )
            content = f"工具调用失败,失败原因：{e}"

        return Message(
            role='tool',
            content=content,
            tool_call_id=tool_call_id

        )

    def run(
            self,
            input_text:str,
            max_tool_iterations:int=5,
            **kwargs:Any
    )->str:
        """支持对话和简单工具调用
        Args:
            input_text:输入文本
            max_tool_iterations:最大工具迭代轮次
            kwargs:其中tool_choice只控制首次请求，默认auto,达到上限后使用none
        
        """
        if not input_text.strip():
            raise ValueError(f"输入文本必须是非空字符串")
        if not isinstance(max_tool_iterations,int) or max_tool_iterations<0:
            raise ValueError("max_tool_iterations参数必须为非负整数")

        options = dict(kwargs)
        first_choice = options.pop('tool_choice','auto')

        #获取可用工具的openai格式schemas列表
        schemas = self._get_tool_openai_schema()

        self.add_message(Message(
            role='user',
            content=input_text
        ))
        for iteration in range(max_tool_iterations+1):
            if not schemas or iteration == max_tool_iterations:
                tool_choice = 'none'
            elif iteration == 0:
                tool_choice = first_choice
            else:
                tool_choice = 'auto'

            logger.info(
                "🔄 Agent 迭代：agent=%s iteration=%d tool_choice=%s tools=%d",
                self.name,
                iteration,
                tool_choice,
                len(schemas),
            )

            try:
                response = self.llm.invoke(
                    messages=self._build_messages(),
                    tools=schemas,
                    tool_choice=tool_choice,
                    **options
                )
                if response is None or not response.choices:
                    raise ValueError("模型响应为空")
                choice = response.choices[0]

            except Exception as e:
                logger.exception(
                    "❌ Agent 模型请求失败：agent=%s iteration=%d error_type=%s",
                    self.name,
                    iteration,
                    type(e).__name__,
                )
                raise RuntimeError("模型请求失败") from e

            message = choice.message
            all_calls = message.tool_calls or []
            calls = [
                call
                for call in all_calls
                if isinstance(call, ChatCompletionMessageFunctionToolCall)
            ]
            unsupported_call_count = len(all_calls) - len(calls)
            if unsupported_call_count:
                logger.warning(
                    "⚠️ Agent 收到不支持的工具调用：agent=%s iteration=%d count=%d",
                    self.name,
                    iteration,
                    unsupported_call_count,
                )

            #没有工具调用直接返回文本
            if not calls:
                if choice.finish_reason == "length":
                    raise RuntimeError("模型输出被截断，未获得完整答复")
                if message.refusal:
                    raise RuntimeError(f"模型拒绝生成答复：{message.refusal}")
                text = message.content
                if not isinstance(text, str) or not text.strip():
                      raise RuntimeError(
                          "模型没有返回文本或工具调用"
                      )

                logger.info(
                    "✅ Agent 运行完成：agent=%s iteration=%d response_chars=%d",
                    self.name,
                    iteration,
                    len(text),
                )

                self.add_message(
                      Message(role="assistant", content=text)
                  )

                return text

            tool_names = [call.function.name for call in calls]
            logger.info(
                f"🧰 Agent[{self.name}]进入工具调用，将调用工具：{tool_names}"
            )

            #保存工具调用消息
            self.add_message(Message(
                role='assistant',
                content=message.content,
                tool_calls=[
                    {'id':call.id,
                     'type':call.type,
                     'function':{
                         'name':call.function.name,
                         'arguments':call.function.arguments
                     }}
                     for call in calls
                ])

            )
            
            for call in calls:
                observation = self._execute_tool_call(call)
                self.add_message(observation)

        logger.warning(
            "⚠️ Agent 达到工具迭代上限：agent=%s max_iterations=%d",
            self.name,
            max_tool_iterations,
            )
        raise RuntimeError("工具调用迭代已达到上限，未获得最终答复")
