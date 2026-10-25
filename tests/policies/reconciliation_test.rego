package caseflow.reconciliation_test

import data.caseflow.authz
import rego.v1

test_reconciliation_requires_admin if {
	every action in ["approval.reconcile", "approval.resolve"] {
		authz.allow with input as {"user": {"role": "admin"}, "action": action}
		every role in ["team_lead", "employee", "intern"] {
			not authz.allow with input as {"user": {"role": role}, "action": action}
		}
	}
}
