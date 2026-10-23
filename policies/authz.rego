package caseflow.authz

import rego.v1

# ---------------------------------------------------------------------------
# 策略层的第一原则：fail-closed。
# 输入缺字段、角色未登记、动作前缀没有匹配 —— 全部落到 default allow := false。
# 这比"默认放行 + 黑名单"更难写错，也是面试里最容易被追问的一点。
# ---------------------------------------------------------------------------

default allow := false

# 角色 -> 允许的动作前缀集合。前缀即命名空间：identity.* / code.* / ticket.* / money.* / admin.*
role_action_prefixes := {
	"admin": {"admin", "identity", "code", "ticket", "money"},
	"team_lead": {"code", "ticket"},
	"employee": {"ticket"},
	"intern": set(),
}

# 提权动作只有 admin 能做。放在策略层而不是提示词里，因为提示词可被绕过。
self_escalation if {
	input.action == "admin.grant"
	input.user.role != "admin"
}

role_permits if {
	some prefix in role_action_prefixes[input.user.role]
	startswith(input.action, prefix)
}

allow if {
	role_permits
	not self_escalation
}

# 给调用方一个可读原因，避免 API 层只拿到一个 false 无法定位
reason := "self_escalation_forbidden" if self_escalation

reason := "role_not_permitted" if {
	not self_escalation
	not role_permits
}

reason := "allowed_by_role" if {
	not self_escalation
	role_permits
}
