"""识别明显的报告自评语句；只做有界检查，不冒充通用语义核验。"""

from __future__ import annotations

import re

from deep_researcher.llm.structured import ModelOutputError

_REPORT_JUDGEMENT = re.compile(
    r"(?:本|这份|当前|上述|该)?(?:报告|正文|草稿)\s*"
    r"(?:中\s*)?(?:是否|有没有|尚未|未曾|没有|未|遗漏|缺少|缺乏|已经|已)?\s*"
    r"(?:提及|提到|包含|涵盖|写到|写出|说明|介绍|覆盖|遗漏|缺少|缺乏)"
    r"|(?:does|did|has|have)\s+(?:the|this|our)\s+(?:report|draft)\s+"
    r"(?:mention|include|cover|address)"
    r"|(?:the|this|our)\s+(?:report|draft)\s+"
    r"(?:(?:does|did|has)\s+not\s+|never\s+)?(?:mention|include|cover|omit|lack)",
    re.IGNORECASE,
)


def is_report_judgement(text: str) -> bool:
    """仅识别明确的“报告有没有写”模式；不能判断任意句子的真假。"""
    return _REPORT_JUDGEMENT.search(text) is not None


def validate_research_question(question: str, query: str) -> None:
    """外部检索不能回答当前草稿是否写了某内容，错误交回评审修复。"""
    if is_report_judgement(question) or is_report_judgement(query):
        raise ModelOutputError(
            "补充问题必须询问外部资料中的事实，不能询问当前报告是否提及某内容；"
            "请先核对原文，确有相关资料缺口才改为具体的领域问题"
        )
