package caseflow.authz_test

test_trace_read_scopes if {
	every role in ["admin", "employee", "team_lead"] {
		authz.allow with input as {"user": {"role": role}, "action": "trace.read"}
	}
	authz.allow with input as {"user": {"role": "admin"}, "action": "trace.read_all"}
	not authz.allow with input as {"user": {"role": "employee"}, "action": "trace.read_all"}
	not authz.allow with input as {"user": {"role": "intern"}, "action": "trace.read"}
}

import data.caseflow.authz
import rego.v1

test_only_admin_can_approve if {
	every action in ["approval.create", "approval.read", "approval.decide"] {
		authz.allow with input as {"user": {"role": "admin"}, "action": action}
		not authz.allow with input as {"user": {"role": "team_lead"}, "action": action}
		not authz.allow with input as {"user": {"role": "intern"}, "action": action}
	}
}

# opa test policies/ -v    ← CI 里跑这一条
# 策略先写单测再接服务，这是 ADR-0001 里承诺的做法。

test_admin_can_grant_admin if {
	authz.allow with input as {"user": {"id": "u_1", "role": "admin"}, "action": "admin.grant", "resource": {"type": "account"}}
}

test_team_lead_cannot_grant_admin if {
	not authz.allow with input as {"user": {"id": "u_2", "role": "team_lead"}, "action": "admin.grant", "resource": {"type": "account"}}
}

test_team_lead_can_invite_member if {
	authz.allow with input as {"user": {"id": "u_2", "role": "team_lead"}, "action": "code.invite_member", "resource": {"type": "repo"}}
}

test_intern_denied_everything if {
	not authz.allow with input as {"user": {"id": "u_3", "role": "intern"}, "action": "ticket.create", "resource": {"type": "project"}}
}

# 关键的一条：未知角色 / 缺字段必须拒绝，而不是报错或放行
test_unknown_role_denied if {
	not authz.allow with input as {"user": {"id": "u_9", "role": "cto"}, "action": "code.create_repo", "resource": {"type": "repo"}}
}

test_missing_input_denied if {
	not authz.allow with input as {}
}

test_reason_is_reported if {
	authz.reason == "role_not_permitted" with input as {"user": {"id": "u_3", "role": "intern"}, "action": "money.place_order", "resource": {"type": "order"}}
}

# 对外契约：客户端只读 decision，不允许直接依赖内部规则
test_decision_contract if {
	authz.decision == {"allow": false, "reason": "self_escalation_forbidden"} with input as {"user": {"id": "u_3", "role": "intern"}, "action": "admin.grant", "resource": {"type": "account"}}
}

test_namespace_prefix_collision_denied if {
	every action in ["codeevil.delete", "code_admin.grant", "ticketing.create"] {
		authz.decision == {"allow": false, "reason": "role_not_permitted"} with input as {"user": {"id": "u_2", "role": "team_lead"}, "action": action, "resource": {"type": "repo"}}
	}
}

test_malformed_actions_denied if {
	every action in ["code", "code.", "code..delete", "code.delete.", "code. delete", "code.delete\n", "code./delete", "Code.delete", 42, null] {
		not authz.allow with input as {"user": {"id": "u_2", "role": "team_lead"}, "action": action, "resource": {"type": "repo"}}
	}
}

test_valid_namespace_actions_allowed if {
	every action in ["code.invite_member", "code.repo.create", "ticket.create"] {
		authz.allow with input as {"user": {"id": "u_2", "role": "team_lead"}, "action": action, "resource": {"type": "repo"}}
	}
}

test_memory_is_personal_and_org_write_requires_admin if {
	every role in ["admin", "team_lead", "employee"] {
		authz.allow with input as {"user": {"role": role}, "action": "memory.read"}
		authz.allow with input as {"user": {"role": role}, "action": "memory.write"}
		authz.allow with input as {"user": {"role": role}, "action": "organization.read"}
	}
	authz.allow with input as {"user": {"role": "admin"}, "action": "organization.write"}
	not authz.allow with input as {"user": {"role": "team_lead"}, "action": "organization.write"}
	not authz.allow with input as {"user": {"role": "employee"}, "action": "organization.write"}
	not authz.allow with input as {"user": {"role": "intern"}, "action": "memory.read"}
}
