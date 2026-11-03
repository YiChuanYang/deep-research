'''agent基类'''
from abc import ABC,abstractmethod
from typing import Optional,Any,List
from deep_researcher.llm.llm import MyLLM
from deep_researcher.config import Configuration
from deep_researcher.agents.message import Message
class Agent(ABC):
    def __init__(
            self,
            name:str,
            llm:MyLLM,
            sys_prompt:Optional[str]=None,
            config:Optional[Configuration]=None)->None:
        self.name = name
        self.llm = llm
        self.config = config or Configuration()
        self.sys_prompt = sys_prompt or "你是一个有用的AI助手"

        self._history:List[Message]=[]

    @abstractmethod
    def run(self,input_text:str,**kwargs)->str:
        '''查询'''
        pass

    def add_message(self,message:Message):
        '''添加消息'''
        self._history.append(message)
    def get_hsitory(self)->list[Message]:
        '''获取历史消息'''
        return self._history.copy()
    def clear_history(self):
        '''清空历史消息'''
        self._history.clear()
    def __str__(self)->str:
        return f"Agent(name={self.name},provider={self.llm.llm_provider})"

