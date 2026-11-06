from time import perf_counter

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
            timeout:int=120,
            provider:Optional[str]=None,)->None:

        if provider:
            self.llm_provider = provider
            if not all([api_key, base_url, model]):
                logger.warning(
                    "⚠️ 自定义 provider 配置不完整，尝试从环境变量补全：provider=%s",
                    self.llm_provider,
                )
                api_key, base_url, model =self._resolve_credentials() # type: ignore
                if not all([api_key, base_url, model]):
                    logger.error(
                        "❌ provider 凭证不完整且环境变量中未找到配置：provider=%s",
                        self.llm_provider,
                    )
                    raise ValueError(f"[MyLLM]自定义provider参数不全，且环境中未找到完整的凭证信息，provider={self.llm_provider}，请检查配置")

        else:
            self.llm_provider = self._auto_detect_provider() or "zhipu"
            logger.debug(
                "🔌 已选择 LLM provider：%s，正在读取环境配置",
                self.llm_provider,
            )
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
            **kwargs:Any)->ChatCompletion:
        """invoke方法，支持原生工具调用"""
        request = dict(kwargs)
        if "temperature" not in request:
            request['temperature'] = float(os.getenv("LLM_TEMPERATURE","0.0"))
        if "max_tokens" not in request:
              request["max_tokens"] = int(os.getenv("LLM_MAX_TOKENS", "4080"))
        request.update(
            model=self.model,
            messages=messages,
            stream=False,
        )
        if tools:
            request["tools"] = tools
            request["tool_choice"] = tool_choice

        logger.debug(
            "📤 LLM 请求开始：provider=%s model=%s messages=%d tools=%d tool_choice=%s",
            self.llm_provider,
            self.model,
            len(messages),
            len(tools or []),
            request.get("tool_choice", "none"),
        )

        started_at = perf_counter()
        response = self.client.chat.completions.create(**request)
        if not response.choices:
            raise ValueError("模型响应中没有 choices")

        usage = getattr(response, "usage", None)
        logger.debug(
            "模型响应完成 · model=%s tokens=%s 耗时=%.2fs",
            getattr(response, "model", self.model),
            getattr(usage, "total_tokens", None),
            perf_counter() - started_at,
            extra={
                "event": "model_response",
                "model": self.model,
                "provider": self.llm_provider,
                "tokens": getattr(usage, "total_tokens", None),
                "duration_s": round(perf_counter() - started_at, 3),
            },
        )
        return response



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
    

