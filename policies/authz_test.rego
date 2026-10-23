package caseflow.authz_test

import rego.v1
import data.caseflow.authz

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
