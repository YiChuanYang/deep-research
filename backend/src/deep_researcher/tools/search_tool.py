from __future__ import annotations
from typing import Any, Optional, List, Dict
from ddgs import DDGS
from ddgs.exceptions import DDGSException
import logging 
logger = logging.getLogger(__name__)


class SearchTool:
    """搜索工具：执行搜索，统一结果格式"""
    def __init__(
            self,
            backend:str="duckduckgo",
            timeout:int=10)->None:
        if not isinstance(timeout,int) or timeout<=0:
            raise ValueError("timeout 必须是正整数")
        self.backend = backend
        self.timeout = timeout

    def run(self, parameters: dict[str, Any]) -> dict[str, Any]:
          """校验参数、执行搜索、整理返回结果。"""

          if not isinstance(parameters, dict):
              raise ValueError("parameters 必须是字典")

          # 1. 校验查询词
          query = parameters.get("input")
          if not isinstance(query, str) or not query.strip():
              raise ValueError("input 必须是非空字符串")

          query = query.strip()

          # 2. 校验搜索后端
          backend = parameters.get("backend", self.backend)
          if backend != "duckduckgo":
              raise ValueError(f"当前不支持搜索后端：{backend}")

          # 3. 校验返回模式
          mode = parameters.get("mode", "structured")
          if mode != "structured":
              raise ValueError("当前只支持 structured 返回模式")

          # 4. 校验结果数量
          max_results = parameters.get("max_results", 5)

          # bool 是 int 的子类，使用 type 判断可排除 True 和 False。
          if type(max_results) is not int or max_results <= 0:
              raise ValueError("max_results 必须是正整数")

          # 5. 明确第一版的能力范围
          fetch_full_page = parameters.get("fetch_full_page", False)
          if not isinstance(fetch_full_page, bool):
              raise ValueError("fetch_full_page 必须是布尔值")

          if fetch_full_page:
              raise NotImplementedError("当前版本尚未实现网页全文抓取")

          # 6. 获取搜索引擎的原始返回值
          entries = self._search_duckduckgo(
              query=query,
              max_results=max_results,
          )

          # 7. 转换字段，并记录不完整结果的处理提示
          results, notices = self._normalize_results(entries)

          return {
              "results": results,
              "backend": backend,
              "answer": None,
              "notices": notices,
          }

    def _search_duckduckgo(
            self,
            query:str,
            max_results:int=10)->List[Dict[str,Any]]:
        """使用 DuckDuckGo 搜索"""
        try:
            logger.info(f"🔍开始搜索：query={query}, backend={self.backend}, max_results={max_results}")
            with DDGS(timeout=self.timeout) as client:
                return client.text(query,backend=self.backend,max_results=max_results)
            logger.info(f"✅搜索成功：query={query}, backend={self.backend}")
        except DDGSException as e:
            logger.error(f"❌搜索失败：query={query}, backend={self.backend}, error={e}")
            raise RuntimeError(f"搜索失败：{e}")

    @staticmethod
    def _normalize_results(entries: List[Dict[str,Any]])->tuple[List[Dict[str,Any]],list[str]]:
        """标准化搜索结果"""
        results:List[Dict[str,Any]]=[]
        notices:list[str]=[]
        for index ,entry in enumerate(entries,start=1):
            url =entry.get('href') or entry.get('url')
            if not isinstance(url,str) or not url.strip():
                notices.append(f"搜索结果第{index}条缺少有效URL，已忽略")
                continue
            url = url.strip()

            title = entry.get("title") or url
            if not isinstance(title,str) or not title.strip():
                notices.append(f"搜索结果第{index}条缺少有效标题，已使用URL代替")
                title = url
            else:
                title = title.strip()

            content = entry.get("body") or entry.get("content") or ""

            results.append({
                'url':url,
                'title':title,
                'content':content,
            })
        return results,notices
    

            







