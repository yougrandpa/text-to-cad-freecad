"""Validate a model's final review against measured, current Gate evidence.

A checklist is an interpretation of the request, not proof of exhaustive semantic
coverage or real-world performance. Only recorded deterministic constraints can
be accepted here; all other objectives remain pending human acceptance.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from tcad.core.types import CheckStatus, Confidence, Severity
from tcad.ir.requirements import source_numbers_match
from tcad.verify.checks_spec import FIRST_TIER_OWNED


class ReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_text: str = Field(min_length=1, max_length=2000)
    check_ids: list[str] = Field(default_factory=list, max_length=64)


class DesignReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=4000)
    checklist: list[ReviewItem] = Field(min_length=1, max_length=64)
    remaining_work: list[str] = Field(max_length=64)


def _user_numbers_match(expr) -> bool:
    """Reject invented numeric acceptance criteria even with a copied source span.

    Conservative on purpose: derived dimensions or Chinese-number ambiguity stay
    pending; a model's confirmed flag is not independent user confirmation.
    """
    if expr.kind in ("feature_count", "symmetric"):
        return False  # structural declarations are not measured functionality
    source = expr.source_text
    if expr.kind == "count" and not any(word in source.lower() for word in ("solid", "实体")):
        return False  # "one toolbox" does not mean solid-count is its function
    return source_numbers_match(expr)


def validate_review(review: DesignReview, ir, report, request_text: str) -> dict:
    pending = list(review.remaining_work)
    # Associate each check with its actual user-sourced constraint. A generic
    # solid/export pass is not evidence for a cavity, hole or useful mechanism.
    constraints_by_check: dict[str, list] = {}
    index = 0
    first_tier = {"bbox": "bbox_spec", "volume": "mass_spec", "wall_thickness": "wall_thickness"}
    for expr in ir.requirements.constraints:
        if expr.kind in FIRST_TIER_OWNED:
            check_id = first_tier[expr.kind]
        else:
            check_id = f"spec_{expr.kind}_{expr.target or 'all'}_{index}"
            index += 1
        if expr.confirmed:
            constraints_by_check.setdefault(check_id, []).append(expr)
    eligible = {}
    for result in report.results:
        expressions = constraints_by_check.get(result.check_id, [])
        if (result.status == CheckStatus.PASS and result.severity == Severity.BLOCKING
                and result.confidence == Confidence.DETERMINISTIC and expressions
                and all(e.source_text.strip() and e.source_text in request_text and _user_numbers_match(e)
                        for e in expressions)):
            eligible[result.check_id] = expressions
    if not eligible:
        pending.append("没有来自用户原话且已通过客观测量的确认约束；当前只验证了构建。")
    checked = set()
    for item in review.checklist:
        if item.source_text not in request_text:
            pending.append(f"需求来源未确认：{item.source_text}")
        if not item.check_ids or any(check_id not in eligible for check_id in item.check_ids):
            pending.append(f"缺少通过的需求测量证据：{item.source_text}")
        elif any(not any(e.source_text == item.source_text for e in eligible[check_id])
                 for check_id in item.check_ids):
            pending.append(f"检查证据与需求不对应：{item.source_text}")
        else:
            checked.update(item.check_ids)
    # No confirmed requirement may be hidden by a selective checklist.
    for check_id in constraints_by_check:
        if check_id not in checked:
            pending.append(f"确认约束未获验收：{check_id}")
    return {
        "verified": not pending,
        "scope": "recorded_constraints",
        "summary": review.summary,
        "checklist": [item.model_dump() for item in review.checklist],
        "remaining_work": list(dict.fromkeys(pending)),
        "ir_version": report.ir_version,
        "note": "只验证已记录约束；需求解释的完整性与实际机械功能仍需用户验收。",
    }
