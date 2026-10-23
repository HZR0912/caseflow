package caseflow.authz

import rego.v1

# ---------------------------------------------------------------------------
# 策略层的第一原则：fail-closed。
# 输入缺字段、角色未登记、动作前缀没有匹配 —— 全部落到 default allow := false。
# 这比"默认放行 + 黑名单"更难写错，也是面试里最容易被追问的一点。
#
# 内部规则统一以 _ 开头：Rego 的 _ 前缀规则不导出到决策文档，
# 所以 /v1/data/caseflow/authz 只返回 {allow, reason}，
# 不会把角色映射表这类内部结构泄露给调用方。
# ---------------------------------------------------------------------------

default allow := false

# 角色 -> 允许的动作前缀集合。前缀即命名空间：identity.* / code.* / ticket.* / money.* / admin.*
_role_action_prefixes := {
	"admin": {"admin", "identity", "code", "ticket", "money"},
	"team_lead": {"code", "ticket"},
	"employee": {"ticket"},
	"intern": set(),
}

# 提权动作只有 admin 能做。放在策略层而不是提示词里，因为提示词可被绕过。
_self_escalation if {
	input.action == "admin.grant"
	input.user.role != "admin"
}

_role_permits if {
	some prefix in _role_action_prefixes[input.user.role]
	startswith(input.action, prefix)
}

allow if {
	_role_permits
	not _self_escalation
}

# 给调用方一个可读原因，避免 API 层只拿到一个 false 无法定位
reason := "self_escalation_forbidden" if _self_escalation

reason := "role_not_permitted" if {
	not _self_escalation
	not _role_permits
}

reason := "allowed_by_role" if {
	not _self_escalation
	_role_permits
}

# 对外只暴露这一个对象：/v1/data/caseflow/authz/decision
# 调用方永远不应该读到内部规则，决策契约越小越不容易被误用。
decision := {
	"allow": allow,
	"reason": reason,
}
