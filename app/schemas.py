"""请求体验证（Pydantic v2）。体积字段用 Decimal，拒绝 float 精度问题。"""

from __future__ import annotations

import re
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

WELL_RE = re.compile(r"^[A-Z]{1,2}\d{1,3}$")


def normalize_well(value: str) -> str:
    well = value.strip().upper()
    if not WELL_RE.match(well):
        raise ValueError("孔位标签须为字母+数字，如 A1、B12")
    return well


class WellCreate(BaseModel):
    """登记孔位中的初始样本与体积。"""

    well: str
    sample: str
    volume: Decimal = Field(gt=0)

    @field_validator("well")
    @classmethod
    def _well(cls, value: str) -> str:
        return normalize_well(value)

    @field_validator("sample")
    @classmethod
    def _sample(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("样本名不能为空")
        return value


class TransferOp(BaseModel):
    """一次移液：从 source 吸取 volume 注入 destination。"""

    source: str
    destination: str
    volume: Decimal = Field(gt=0)

    @field_validator("source", "destination")
    @classmethod
    def _well(cls, value: str) -> str:
        return normalize_well(value)

    @model_validator(mode="after")
    def _distinct_wells(self) -> "TransferOp":
        if self.source == self.destination:
            raise ValueError("来源孔与目标孔不能相同")
        return self


class BatchCreate(BaseModel):
    """一批移液操作，按 operations 顺序原子执行；request_key 用于幂等去重。

    reservation_id 可选：携带时该批次必须逐孔逐量完全兑现对应预留
    （每个来源孔的吸取量之和与预留体积严格相等，且不得动用其他预留的体积），
    成功提交时在同一事务把预留标记为 consumed；否则整批失败且不消耗预留。
    """

    request_key: str = Field(min_length=1, max_length=128)
    operations: list[TransferOp] = Field(min_length=1)
    reservation_id: int | None = Field(default=None, gt=0)

    @field_validator("request_key")
    @classmethod
    def _key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("request_key 不能为空")
        return value


class PlanCreate(BaseModel):
    """先核对、后确认的移液计划：与直接提交批次同样的 operations 与预留语义。

    服务在只读事务固定涉及孔规范快照并完成逐步校验后落库计划（不写批次、
    不改变液体）；request_key 用于计划幂等去重。之后须以独立 confirm_key
    调用确认端点，快照未变化才原子执行。
    """

    request_key: str = Field(min_length=1, max_length=128)
    operations: list[TransferOp] = Field(min_length=1)
    reservation_id: int | None = Field(default=None, gt=0)

    @field_validator("request_key")
    @classmethod
    def _key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("request_key 不能为空")
        return value


class PlanConfirmCreate(BaseModel):
    """确认移液计划：独立 confirm_key 幂等去重（与计划 request_key 分离）。"""

    confirm_key: str = Field(min_length=1, max_length=128)

    @field_validator("confirm_key")
    @classmethod
    def _key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("confirm_key 不能为空")
        return value


class ReversalCreate(BaseModel):
    """撤销一批已提交移液：request_key 用于撤销操作的幂等去重。"""

    request_key: str = Field(min_length=1, max_length=128)

    @field_validator("request_key")
    @classmethod
    def _key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("request_key 不能为空")
        return value


class CapacityUpdate(BaseModel):
    """设置或取消孔位最大容量：正数表示上限，null 表示取消（不限容量）。"""

    max_capacity: Decimal | None = Field(default=None, gt=0)


class _QuarantineWellsMixin(BaseModel):
    """一组待隔离/解除的孔位：逐孔校验标签，排序去重得到目标的统一形式。"""

    wells: list[str] = Field(min_length=1)

    @field_validator("wells")
    @classmethod
    def _wells(cls, value: list[str]) -> list[str]:
        return [normalize_well(w) for w in value]

    @model_validator(mode="after")
    def _dedupe_wells(self):
        self.wells = sorted(set(self.wells))
        return self


class QuarantineCreate(_QuarantineWellsMixin):
    """孔位隔离申请：一组已登记孔 + 案件号 + 原因；request_key 用于幂等去重。"""

    request_key: str = Field(min_length=1, max_length=128)
    case_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("request_key", "case_id", "reason")
    @classmethod
    def _text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("字段不能为空")
        return value


class QuarantineReleaseCreate(_QuarantineWellsMixin):
    """解除隔离：须携带对应 case_id；request_key 用于解除操作的幂等去重。"""

    request_key: str = Field(min_length=1, max_length=128)
    case_id: str = Field(min_length=1, max_length=128)

    @field_validator("request_key", "case_id")
    @classmethod
    def _text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("字段不能为空")
        return value


# ---------------------------------------------------------------- 移液体积预留


class ReservationItem(BaseModel):
    """预留中的一孔体积：从 well 为未来批次预留 volume。"""

    well: str
    volume: Decimal = Field(gt=0)

    @field_validator("well")
    @classmethod
    def _well(cls, value: str) -> str:
        return normalize_well(value)


class ReservationCreate(BaseModel):
    """一次预留多个已登记来源孔的 Decimal 体积；request_key 用于幂等去重。

    同一孔在一次预留中只允许出现一次（重复即参数非法）。
    """

    request_key: str = Field(min_length=1, max_length=128)
    items: list[ReservationItem] = Field(min_length=1)

    @field_validator("request_key")
    @classmethod
    def _key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("request_key 不能为空")
        return value

    @model_validator(mode="after")
    def _no_duplicate_wells(self) -> "ReservationCreate":
        seen: set[str] = set()
        duplicates: list[str] = []
        for item in self.items:
            if item.well in seen:
                duplicates.append(item.well)
            seen.add(item.well)
        if duplicates:
            raise ValueError(f"同一预留中孔位不能重复：{', '.join(sorted(duplicates))}")
        return self


class ReservationCancelCreate(BaseModel):
    """取消一个生效预留：取消操作使用独立的 request_key 幂等去重。"""

    request_key: str = Field(min_length=1, max_length=128)

    @field_validator("request_key")
    @classmethod
    def _key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("request_key 不能为空")
        return value
