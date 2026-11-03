from pydantic import Field ,BaseModel
from typing import Literal,Optional,Any,Dict
from datetime import datetime




class Message(BaseModel):
    """消息类"""
    role:Literal["system","user","assistant","tool"]
    content:str|None=None
    # assistant 发起工具调用时使用。
    tool_calls: list[dict[str, Any]] | None = None
    # tool 返回执行结果时使用。
    tool_call_id: str | None = None
    timestamp:datetime=Field(default_factory=datetime.now) #type: ignore
    metadata:Optional[Dict[str,Any]]=None

    def to_dict(self)->Dict[str,Any]:
        """只返回模型接口所需要的字段"""
        results : Dict[str,Any] = {
            'role':self.role,
            'content':self.content
        } 
        if self.tool_calls:
            results['tool_calls']=self.tool_calls
        if self.tool_call_id:
            results['tool_call_id']=self.tool_call_id
        return results

    def __str__(self)->str:
        return f"[{self.role}]{self.content}"



    