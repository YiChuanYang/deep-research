"""固定问题集的类型、校验、版本指纹与选择。"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_DATASET = Path(__file__).with_name("cases.v1.json")
NonEmptyText = Annotated[str, Field(min_length=1)]
Category = Literal["overview", "comparison", "engineering", "boundaries"]
Split = Literal["dev", "holdout"]


class DatasetModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, str_strip_whitespace=True,
    )


class ResearchCase(DatasetModel):
    case_id: str = Field(pattern=r"^DR-[0-9]{3}$")
    split: Split
    category: Category
    topic: str = Field(min_length=2, max_length=500)
    key_points: list[NonEmptyText] = Field(min_length=3, max_length=5)


class ResearchDataset(DatasetModel):
    schema_version: Literal[1]
    dataset_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    cases: list[ResearchCase] = Field(min_length=1)

    @model_validator(mode="after")
    def check_duplicates(self) -> ResearchDataset:
        seen_ids: set[str] = set()
        seen_topics: set[str] = set()
        for case in self.cases:
            if case.case_id in seen_ids:
                raise ValueError(f"题目编号重复：{case.case_id}")
            if case.topic in seen_topics:
                raise ValueError(f"题目内容重复：{case.case_id}")
            if len(set(case.key_points)) != len(case.key_points):
                raise ValueError(f"验收要点重复：{case.case_id}")
            seen_ids.add(case.case_id)
            seen_topics.add(case.topic)
        return self

    def fingerprint(self) -> str:
        """规范化后计算 SHA256，空格、缩进和 BOM 不影响内容指纹。"""
        content = json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


def load_dataset(path: Path = DEFAULT_DATASET) -> ResearchDataset:
    return ResearchDataset.model_validate_json(path.read_text(encoding="utf-8-sig"))


def select_cases(
    dataset: ResearchDataset, split: Split = "dev",
    case_ids: list[str] | None = None,
) -> list[ResearchCase]:
    """按题集顺序选择；显式拒绝不存在或属于其他分组的编号。"""
    requested = set(case_ids or [])
    if case_ids is not None and len(requested) != len(case_ids):
        raise ValueError("case-id 不得重复")
    by_id = {case.case_id: case for case in dataset.cases}
    for case_id in requested:
        if case_id not in by_id:
            raise ValueError(f"题目不存在：{case_id}")
        if by_id[case_id].split != split:
            raise ValueError(f"{case_id} 属于 {by_id[case_id].split}，与 --split 不一致")
    selected = [
        case for case in dataset.cases
        if case.split == split and (not requested or case.case_id in requested)
    ]
    if not selected:
        raise ValueError("没有选中任何题目")
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(description="校验固定评测题集")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args()
    try:
        dataset = load_dataset(args.dataset)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f"校验通过，版本：{dataset.dataset_version}；题数：{len(dataset.cases)}")
    print("分组：", dict(Counter(case.split for case in dataset.cases)))
    print("类别：", dict(Counter(case.category for case in dataset.cases)))
    print("内容 SHA256：", dataset.fingerprint())


if __name__ == "__main__":
    main()
