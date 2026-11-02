from dotenv import load_dotenv
from typing import Any,Dict,Optional,List
from openai import OpenAI
import os
import logging
from openai.types.chat import ChatCompletion


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
            messages:List[Dict[str,Any]],
            tools:List[Dict[str,Any]]|None=None,
            *,
            tool_choice:str='auto',
            **kwargs:Any)->ChatCompletion|None:
        """invoke方法，支持原生工具调用"""
        request = dict(kwargs)
        if "temperature" not in request:
            request['temperature'] = float(os.getenv("LLM_TEMPERATURE","0.0"))
        if "max_tokens" not in request:
              request["max_tokens"] = int(os.getenv("LLM_MAX_TOKENS", "4080"))
        request.update(
            model=self.model,
            messages = messages,
            stream = False,
        )
        if tools:
            request['tools']=tools
            request['tool_choice']=tool_choice
        try:
            logger.info(f"正在等待模型响应，model={self.model}")
            response = self.client.chat.completions.create(**request)
            if not response.choices:
                logger.error(f"❌[MyLLM]_invoke模型响应中无choice,response={response}")
                raise ValueError(f"❌模型响应中无choice,response={response}")
            logger.info(f"✅[MyLLM]_invoke模型响应成功，model={self.model}")
            return response
        except Exception as e:
            logger.error(f"❌[MyLLM]_invoke模型响应失败，原因：{e}")
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
    

