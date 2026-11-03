from typing import Callable, Dict, Optional, Any, List,get_type_hints
from pydantic import BaseModel
import inspect
from abc import ABC,abstractmethod
import re

def tool_action(name:Optional[str]=None,description:Optional[str]=None):
    '''装饰器:标记一个方法为可展开的工具 action'''
    def decorator(func:Callable)->Callable:
        func._is_tool_action = True
        func._tool_name = name or func.__name__
        func._tool_description = description or func.__doc__
        return func
    return decorator



class ToolParameter(BaseModel):
    '''工具参数定义'''
    name:str
    type:str
    description:str
    required:bool=True
    default:Any=None


class Tool(ABC):
    """工具基类

    支持两种使用模式：
    1. 普通模式：工具作为单一实体使用
    2. 可展开模式：工具可以展开为多个独立的子工具（每个子工具对应一个功能）

    展开模式支持两种实现方式：
    - 手动定义子工具类（传统方式）
    - 使用 @tool_action 装饰器自动生成（推荐）
    """
    def __init__(self,name:str,description:str,expandable:bool=False):
        """初始化工具

        Args:
            name: 工具名称
            description: 工具描述
            expandable: 是否可展开为多个子工具
        """
        self.name =name
        self.description= description
        self.expandable = expandable

    @abstractmethod
    def run(self,parameters:Dict[str,Any])->Any:
        """执行工具"""
        pass
    @abstractmethod
    def get_parameters(self) -> List[ToolParameter]:
        """获取工具参数定义"""
        pass

    def get_expanded_tools(self)->Optional[List['Tool']]:
        """获取展开后的工具列表
        Returns:
            如果工具支持展开，返回工具列表，否则None
        """
        if not self.expandable:
            return None
        tools = []

        for name,method in inspect.getmembers(self,predicate=inspect.ismethod):
            if hasattr(method,'_is_tool_action'):
                tool=AutoGenerateTool(
                    parent=self,
                    method=method,
                    name=method._tool_name,
                    description=method._tool_description
                )
                tools.append(tool)

        return tools if tools else None

    def validate_parameters(self,parameters:Dict[str,Any])->bool:
        """校验参数
        """
        required_params = [param.name for param in self.get_parameters() if param.required]
        return all(param in parameters for param in required_params)

    def to_dict(self)->Dict[str,Any]:
        """转换为字典"""
        return {
            "name":self.name,
            "description":self.description,
            "parameters":[param.model_dump() for param in self.get_parameters()]
        }
    def to_openai_schema(self)->Dict[str,Any]:
        """将参数需求转换为Openai function calling的标注schema"""
        properties = {}
        required = []

        for p in self.get_parameters():
            p_description = f"{p.description}(默认：{p.default})" if p.default is not None else f"{p.description}"
            prop ={
                "type":p.type,
                "description":p_description

            }
            if p.type == "array":
                prop["items"]={"type":"string"}

            properties[p.name]=prop
            if p.required:
                required.append(p.name)

        return {
            "type":"function",
            "function":{
                "name":self.name,
                "description":self.description,
                "parameters":{
                    "type":"object",
                    "properties":properties,
                    "required":required
                }
            }
        }



class AutoGenerateTool(Tool):
    """自动生成的工具 - 从方法签名和 docstring 自动提取参数"""
    def __init__(self,parent:Tool,method:Callable,name:str=None,description:str=None):
        """初始化工具

        Args:
        parent:父工具实例
        method:工具方法
        name:工具名，未传则直接使用函数名
        description:工具描述，未传则从函数文档中提取
        """
        self.parent = parent
        self.method=method
        if name is None:
            method_name=method.__name__.lstrip()
            name = f"{parent.name}_{method_name}"

        if description is None:
            description = self._extract_description_from_docstring()

        #回传给父类
        super().__init__(name=name,description=description)
        self._parameters = self._parse_parameters()



    def _extract_description_from_docstring(self)->str:
        """从docstring提取描述"""
        doc = inspect.getdoc(self.method)

        if not doc:
            return f"执行{self.method.__name__}"
        lines = doc.split("\n")
        for line in lines:
            line = line.strip()
            if line and not line.startswith('Args:') and not line.startswith('Returns:'):
                return line
        return f"执行{self.method.__name__}"

    def _parse_parameters(self)->List[ToolParameter]:
        '''从方法签名和docstring自动提取工具参数'''
        #
        sig = inspect.signature(self.method)
        type_hints = get_type_hints(self.method)
        docstring = inspect.getdoc(self.method) or ""
        #从docstring提取参数描述文本
        param_descriptions = self._parse_param_descriptions(docstring)
        parameters = []

        for param_name,param in sig.parameters.items():
            if param_name=="self":
                continue
            #获取类型
            param_type_hint = type_hints.get(param_name,str)
            param_type = self._python_type_to_tool_type(param_type_hint)
            #获取描述
            param_description = param_descriptions.get(param_name,f"参数{param_name}")
            #判断是否必须
            required = param.default==inspect.Parameter.empty
            default = None if required else param.default

            parameters.append(ToolParameter(
                name=param_name,
                type=param_type,
                description=param_description,
                required=required,
                default=default
            ))
        return parameters





    def _parse_param_descriptions(self, docstring: str) -> Dict[str, str]:
        """从 docstring 解析参数描述

        支持格式:
            Args:
                param_name: 参数描述
                another_param: 另一个参数描述
        """
        descriptions = {}

        # 查找 Args: 部分
        args_match = re.search(r'Args:\s*\n(.*?)(?:\n\s*\n|Returns:|$)', docstring, re.DOTALL)
        if not args_match:
            return descriptions

        args_section = args_match.group(1)

        # 解析每个参数
        # 匹配格式: param_name: 描述 或 param_name (type): 描述
        param_pattern = r'^\s*(\w+)(?:\s*\([^)]+\))?\s*:\s*(.+?)(?=^\s*\w+\s*(?:\([^)]+\))?\s*:|$)'
        matches = re.finditer(param_pattern, args_section, re.MULTILINE | re.DOTALL)

        for match in matches:
            param_name = match.group(1).strip()
            param_desc = match.group(2).strip()
            # 清理描述中的多余空白
            param_desc = re.sub(r'\s+', ' ', param_desc)
            descriptions[param_name] = param_desc

        return descriptions

    def _python_type_to_tool_type(self,py_type)->str:
        '''将python类型转换为工具类型字符串'''
        #先处理泛类型
        origin = getattr(py_type,'__origin__',None)
        if origin is not None:
            if origin is list:
                return "array"
            if origin is dict:
                return "object"

        type_map={
            str:"string",
            int:"integer",
            float:"number",
            bool:"boolean",
            list:"array",
            dict:"object",
        }
        return type_map.get(py_type,"string")

    def run(self,parameters:Dict[str,Any])->Any:
        return self.method(**parameters)
    def get_parameters(self) -> List[ToolParameter]:
        return self._parameters
    

    