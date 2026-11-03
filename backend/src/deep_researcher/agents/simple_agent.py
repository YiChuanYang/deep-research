from deep_researcher.llm.llm import MyLLM
from deep_researcher.agents.message import Message
from deep_researcher.config import Configuration
from deep_researcher.agents.agent import Agent
from deep_researcher.tools.registry import ToolRegistry
from typing import Optional,Dict,Any,List
import logging
import json
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
    ):
        super().__init__(name,llm,sys_prompt,config)
        self.tool_registry = tool_registry 
        self.enable_tool_call = enable_tool_call and self.tool_registry is not None
        logger.info(f"✅[SimpleAgent]初始化成功，llm_provider={self.llm.llm_provider},enable_tool_call={self.enable_tool_call}")



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

            logger.info(f"🔧[SimpleAgent]run进入第{iteration}次工具迭代，tool_choice={tool_choice}")

            try:
                response = self.llm.invoke(
                    messages=self._build_messages(),
                    tools=schemas,
                    tool_choice=tool_choice,
                    **options
                )
                if response is None or response.choices is None:
                    logger.error("❌[SimpleAgent]run:模型响应为空")
                    raise ValueError("模型响应为空") 
                choice = response.choices[0]

            except Exception as e:
                return f"❌[SimpleAgent]run响应失败，e={e}"

            message = choice.message
            calls = message.tool_calls
            #没有工具调用直接返回文本
            if not calls:
                logger.warning(f"[SimplAgent]run:本次模型返回无tool_call,iteration={iteration},提前返回结果:{"是" if iteration<+max_tool_iterations else "否"}")
                text = message.content or message.refusal
                if not isinstance(text, str) or not text.strip():
                      raise RuntimeError(
                          "模型没有返回文本或工具调用"
                      )

                self.add_message(
                      Message(role="assistant", content=text)
                  )

                return text

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
           




            






        