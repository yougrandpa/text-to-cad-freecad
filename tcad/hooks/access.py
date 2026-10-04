"""Per-turn access policy without mutating the shared service dispatcher."""
from tcad.core.access import AccessMode, READ_ONLY_TOOLS
from tcad.core.types import HookDecision, HookEvent, HookResult


class AccessHooks:
    def __init__(self, delegate, mode: AccessMode):
        self.delegate = delegate
        self.mode = mode

    def dispatch(self, event, payload):
        # Observer taps must record the effective decision, not an ASK that
        # was auto-approved afterwards. The tap applies policy before logging.
        observed = getattr(self.delegate, "dispatch_with_access", None)
        if observed is not None:
            return observed(event, payload, self.mode)
        if event == HookEvent.PRE_TOOL_USE:
            if self.mode == AccessMode.READ_ONLY and payload.get("tool_name") not in READ_ONLY_TOOLS:
                return HookResult(decision=HookDecision.DENY, hook_name="access_mode",
                                  reason="仅可读取：禁止修改、导出和 Python 执行")
            if self.mode == AccessMode.FULL:
                return HookResult(decision=HookDecision.ALLOW, hook_name="access_mode",
                                  reason="用户选择完全访问：跳过工具审批和路径限制")
        result = (self.delegate.dispatch(event, payload) if self.delegate else
                  HookResult(decision=HookDecision.ALLOW, hook_name="access_mode"))
        if (self.mode == AccessMode.AUTO and event == HookEvent.PRE_TOOL_USE
                and payload.get("tier") in {"read", "write"}
                and result.decision == HookDecision.ASK):
            return result.model_copy(update={"decision": HookDecision.ALLOW,
                "reason": "用户选择自动审批：" + result.reason})
        return result
