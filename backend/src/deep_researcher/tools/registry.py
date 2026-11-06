from deep_researcher.tools.base import Tool
from typing import Callable,Dict,Any,Optional
from copy import deepcopy
import logging
logger = logging.getLogger(__name__)


class ToolRegistry:
    """
    工具注册表
    提供工具注册、管理和执行
    1.注册Tool对象为工具
    2.直接注册函数为工具
    """
    def __init__(self):
        self._tools: Dict[str,Tool]={}
        self._functions :Dict[str,dict[str,Any]]={}

    def register_tool(self,tool:Tool,auto_expandable:bool=False)->None:
        """
        注册Tool对象
        Args:
        tool:Tool实例
        auto_expandable:是否自动展开子工具
        """
        #检查工具是否可以展开
        if auto_expandable and tool.expandable:
            expanded_tools = tool.get_expanded_tools()
            if expanded_tools is not None:
                replaced_count = 0
                for sub_tool in expanded_tools:
                    if sub_tool in self._tools:
                        replaced_count += 1
                        logger.warning(
                            "⚠️ 工具已存在，将覆盖：tool=%s",
                            sub_tool.name,
                        )
                    self._tools[sub_tool.name]=sub_tool
                logger.info(
                    "🧰 工具组已展开：tool=%s count=%d replaced=%d",
                    tool.name,
                    len(expanded_tools),
                    replaced_count,
                )
                return 
        if tool.name in self._tools:
            logger.warning(
                "⚠️ 工具已存在，将覆盖：tool=%s",
                tool.name,
            )

        self._tools[tool.name]=tool
        logger.info(
            "🧰 工具已注册：tool=%s type=tool",
            tool.name,
        )


    def register_function(
          self,
          name: str,
          description: str,
          func: Callable[..., Any],
          parameters: dict[str, Any],
      ) -> None:
          if name in self._tools or name in self._functions:
              raise ValueError(f"工具已注册：{name}")

          if not callable(func):
              raise TypeError("func 必须是可调用对象")

          if parameters.get("type") != "object":
              raise ValueError("parameters 必须是 object 类型的 schema")

          self._functions[name] = {
              "description": description,
              "func": func,
              "parameters": deepcopy(parameters),
          }
          logger.info(
              "🧰 工具已注册：tool=%s type=function",
              name,
          )

    def unregister(self, name: str)->bool:
        """注销工具"""
        if name in self._tools:
            del self._tools[name]
            logger.info(
                "🗑️ 工具已注销：tool=%s",
                name,
            )
            return True
        elif name in self._functions:
            del self._functions[name]
            logger.info(
                "🗑️ 工具已注销：tool=%s",
                name,
            )
            return True
        else:
            logger.warning(
                "⚠️ 注销失败，工具不存在：tool=%s",
                name,
            )
            return False
        
    def get_tool(self, name: str) -> Optional[Tool]:
        """获取Tool对象"""
        return self._tools[name]

    

    def get_function(self, name: str) -> Optional[Callable]:
        """获取工具函数"""
        return self._functions[name].get("func")

    
    def execute_tool(
          self,
          name: str,
          parameters: dict[str, Any],
      ) -> Any:
          """保留原始返回值，让异常交给 Agent 处理。"""
          parameter_count = len(parameters) if isinstance(parameters, dict) else 0
          logger.debug(
              "🔧 工具执行开始：tool=%s parameters=%d",
              name,
              parameter_count,
          )
          try:
              if not isinstance(parameters, dict):
                  raise TypeError("parameters 必须是字典")

              tool = self._tools.get(name)

              if tool is not None:
                  if not tool.validate_parameters(parameters):
                      raise ValueError(f"工具 {name} 缺少必填参数")
                  result = tool.run(parameters)
              else:
                  info = self._functions.get(name)
                  if info is None:
                      raise KeyError(f"未注册工具：{name}")
                  result = info["func"](**parameters)

          except Exception:
              raise

          logger.debug(
              "✅ 工具执行完成：tool=%s result_type=%s",
              name,
              type(result).__name__,
          )
          return result

    def get_openai_tools(self) -> list[dict[str, Any]]:
          """返回两类工具的原生function calling 定义"""
          schemas = [
              tool.to_openai_schema()
              for tool in self._tools.values()
          ]

          # 普通函数使用注册时提供的参数定义。
          for name, info in self._functions.items():
              schemas.append({
                  "type": "function",
                  "function": {
                      "name": name,
                      "description": info["description"],
                      "parameters": info["parameters"],
                  },
              })

          # 避免调用者修改返回值时影响注册表中的定义。
          return deepcopy(schemas)


    def get_tools_description(self) -> str:
        """
        获取所有可用工具的格式化描述字符串

        Returns:
            工具描述字符串，用于构建提示词
        """
        descriptions =[]
        for tool in self._tools.values():
            descriptions.append(f"-{tool.name}:{tool.description}")

        for name,info in self._functions.items():
            descriptions.append(f"-{name}:{info['description']}")

        return "\n".join(descriptions) if descriptions else "暂无可用工具"
    def list_tools(self) -> list[str]:
        """列出所有工具名称"""
        return list(self._tools.keys()) + list(self._functions.keys())

    def get_all_tools(self) -> list[Tool]:
        """获取所有Tool对象"""
        return list(self._tools.values())

    def clear(self):
        """清空所有工具"""
        tool_count = len(self._tools) + len(self._functions)
        self._tools.clear()
        self._functions.clear()
        logger.info(
            "🧹 工具注册表已清空：removed=%d",
            tool_count,
        )