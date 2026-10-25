"""Receipt-bound, compare-and-set semantic undo within the supplier transaction."""

from copy import deepcopy

from core.tools.compensation import UNDO_ACTIONS

from apps.mockcorp.domain import BusinessError, Changes, apply


def compensate(state, source, operation):
    if not source or source.get("operation") != operation or source.get("dry_run") is not False:
        raise BusinessError(409, "compensation_source_mismatch")
    if operation not in UNDO_ACTIONS:
        raise BusinessError(409, "compensation_unsupported")
    diff = source["diff"]
    # Check all affected objects before mutating any; balance is restored by delta, never snapshot.
    for change in diff:
        if change["collection"] != "balance":
            current = state[change["collection"]].get(change["id"])
            if current != change["after"]:
                raise BusinessError(409, "compensation_state_changed")
    changes = Changes(state)
    original = source["result"]
    if operation == "ticket.assign_tickets":
        for change in diff:
            changes.put("tickets", change["before"])
        result = {"id": "assignment_undo", "restored_ids": [d["id"] for d in diff]}
    elif operation == "money.create_order":
        if any(s["order_id"] == original["id"] and s["active"] for s in state["seats"].values()):
            raise BusinessError(409, "compensation_dependency_in_use")
        result, refund_diff, _ = apply(
            state,
            "money.refund",
            {"order_id": original["id"], "amount_cents": original["total_cents"]},
        )
        return result, refund_diff
    elif operation == "code.invite_member":
        before = deepcopy(state["members"].pop(original["id"]))
        changes.diff.append(
            {"collection": "members", "id": original["id"], "before": before, "after": None}
        )
        result = {"id": original["id"]}
    elif operation == "money.bind_seat":
        result = changes.put("seats", {**original, "active": False})
    else:
        user_id = original["id"] if operation == "identity.create_user" else original["user_id"]
        if any(m["user_id"] == user_id for m in state["members"].values()):
            raise BusinessError(409, "compensation_dependency_in_use")
        if operation == "identity.create_user":
            if (
                any(m["user_id"] == user_id and m["active"] for m in state["mailboxes"].values())
                or any(s["user_id"] == user_id and s["active"] for s in state["seats"].values())
                or any(
                    t.get("assignee_id") == user_id and t["status"] == "open"
                    for t in state["tickets"].values()
                )
            ):
                raise BusinessError(409, "compensation_dependency_in_use")
            result = changes.put("users", {**original, "active": False})
        else:
            result = changes.put("mailboxes", {**original, "active": False})
    return result, changes.diff
