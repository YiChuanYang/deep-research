from dotenv import load_dotenv
from typing import Any,Dict,Optional,List
from openai import OpenAI
import os
import logging

logger = logging.getLogger(__name__)

load_dotenv()

class MyLLM():
    def __init__(
            self,
            model:Optional[str]=None,
            api_key:Optional[str]=None,
            base_url:Optional[str]=None,
            timeout:int=30,
            provider:Optional[str]=None,)->None:

        if provider:
            self.llm_provider = provider
            if not all([api_key, base_url, model]):
                logger.warning(f"[MyLLM]自定义provider参数不全，provider={self.llm_provider}，将从环境中尝试补全")
                api_key, base_url, model =self._resolve_credentials() # type: ignore
                if not all([api_key, base_url, model]):
                    logger.error(f"[MyLLM]自定义provider参数不全，且环境中未找到完整的凭证信息，provider={self.llm_provider}，请检查配置")
                    raise ValueError(f"[MyLLM]自定义provider参数不全，且环境中未找到完整的凭证信息，provider={self.llm_provider}，请检查配置")

        else:
            self.llm_provider = self._auto_detect_provider() or "zhipu"
            logger.info(f"[MyLLM]自动检测到provider={self.llm_provider}，将尝试从环境中获取凭证信息")
            api_key, base_url, model =self._resolve_credentials() # type: ignore
        self.api_key = api_key  
        self.base_url = base_url
        self.model = model
        self.timeout = timeout or int(os.getenv("LLM_TIMEOUT", 30))

        self.client = OpenAI(
            api_key=self.api_key, 
            base_url=self.base_url, 
            timeout=self.timeout)
    def invoke(
            self,
            messages:List[Dict[str,str]],
            tools:Optional[List[Dict]]=None,
            **kwargs)->Optional[str]:
        """支持openai原生工具调用的invoke方法"""
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=False,
                tools=tools,
                tool_choice='auto' if tools else None,
                temperature=kwargs.get('temperature',float(os.getenv("LLM_TEMPERATURE",0.0))),
                max_tokens=kwargs.get('max_tokens',int(os.getenv('LLM_MAX_TOKENS',4080))),
            ) # type: ignore
            logger.info(f"[MyLLM]invoke成功,tool_choice={'auto' if tools else None}")
            return response
        except Exception as e:
            logger.error(f"❌[MyLLM]invoke失败,tool_choice={'auto' if tools else None},错误信息:{e}")
            return None


    def _auto_detect_provider(self)->str:
        """自动检测provider,当前支持zhipu"""
        if os.getenv("ZHIPU_API_KEY"):
            return "zhipu"
        return 'zhipu'  # 默认使用 zhipu

    def _resolve_credentials(self)->tuple[str|None,str|None,str|None]:
        """根据provider自动解析api_key,base_url,model_id"""
        if self.llm_provider == "zhipu":
            api_key = os.getenv("ZHIPU_API_KEY")
            base_url = os.getenv("ZHIPU_BASE_URL")
            model_id = os.getenv("ZHIPU_MODEL_NAME")
            return api_key, base_url, model_id
        return None, None, None
    

