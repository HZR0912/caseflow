"""纯业务变更：在事务私有副本上校验并产生 diff，不访问网络或数据库。"""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from apps.mockcorp.models import MockSettings

COLLECTIONS = {
    "identity": ("users", "mailboxes"),
    "code": ("repos", "members"),
    "ticket": ("projects", "tickets"),
    "money": ("orders", "seats", "refunds"),
}


class BusinessError(Exception):
    def __init__(self, status: int, code: str, *, retryable: bool = False):
        self.status = status
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def initial_state(settings: MockSettings) -> dict:
    return {
        **{name: {} for names in COLLECTIONS.values() for name in names},
        "balance_cents": settings.initial_balance_cents,
        "code_seat_limit": settings.code_seat_limit,
        "allowed_email_domains": settings.allowed_email_domains,
    }


def require(state: dict, collection: str, resource_id: str) -> dict:
    resource = state[collection].get(resource_id)
    if resource is None:
        raise BusinessError(404, f"{collection}_not_found")
    return resource


def active_user(state: dict, user_id: str) -> dict:
    user = require(state, "users", user_id)
    if not user["active"]:
        raise BusinessError(409, "user_disabled")
    return user


def unique(state: dict, collection: str, **fields) -> None:
    if any(all(item.get(k) == v for k, v in fields.items()) for item in state[collection].values()):
        raise BusinessError(409, f"{collection}_already_exists")


class Changes:
    def __init__(self, state: dict):
        self.state = state
        self.diff: list[dict] = []

    def put(self, collection: str, resource: dict) -> dict:
        resource = deepcopy(resource)
        rid = resource["id"]
        before = deepcopy(self.state[collection].get(rid))
        if before != resource:
            self.diff.append(
                {"collection": collection, "id": rid, "before": before, "after": resource}
            )
            self.state[collection][rid] = resource
        return resource

    def create(self, collection: str, **fields) -> dict:
        return self.put(collection, {"id": uuid4().hex, **fields})

    def balance(self, amount: int) -> None:
        before = self.state["balance_cents"]
        after = before + amount
        if after < 0:
            raise BusinessError(409, "insufficient_funds")
        self.diff.append({"collection": "balance", "before": before, "after": after})
        self.state["balance_cents"] = after


def apply(state: dict, operation: str, p: dict, *, partial: bool = False) -> tuple[dict, list, int]:
    changes = Changes(state)
    status = 200
    match operation:
        case "identity.create_user":
            unique(state, "users", username=p["username"].lower())
            result = changes.create(
                "users",
                username=p["username"].lower(),
                display_name=p["display_name"],
                active=True,
                password_version=0,
            )
            status = 201
        case "identity.create_mailbox":
            active_user(state, p["user_id"])
            email = p["email"].lower()
            if email.split("@")[1] not in state["allowed_email_domains"]:
                raise BusinessError(403, "email_domain_denied")
            unique(state, "mailboxes", email=email)
            unique(state, "mailboxes", user_id=p["user_id"])
            result = changes.create("mailboxes", user_id=p["user_id"], email=email, active=True)
            status = 201
        case "identity.reset_password":
            user = active_user(state, p["user_id"])
            result = changes.put(
                "users", {**user, "password_version": user["password_version"] + 1}
            )
        case "identity.disable_user":
            user = require(state, "users", p["user_id"])
            result = changes.put("users", {**user, "active": False})
            for mailbox in list(state["mailboxes"].values()):
                if mailbox["user_id"] == user["id"]:
                    changes.put("mailboxes", {**mailbox, "active": False})
        case "code.create_repo":
            unique(state, "repos", name=p["name"].lower())
            result = changes.create("repos", name=p["name"].lower())
            status = 201
        case "code.invite_member":
            require(state, "repos", p["repo_id"])
            active_user(state, p["user_id"])
            if not any(
                m["user_id"] == p["user_id"] and m["active"] for m in state["mailboxes"].values()
            ):
                raise BusinessError(409, "active_mailbox_required")
            unique(state, "members", repo_id=p["repo_id"], user_id=p["user_id"])
            occupied = {m["user_id"] for m in state["members"].values()}
            if p["user_id"] not in occupied and len(occupied) >= state["code_seat_limit"]:
                raise BusinessError(409, "code_seat_limit")
            result = changes.create("members", **p)
            status = 201
        case "code.set_permission":
            require(state, "repos", p["repo_id"])
            active_user(state, p["user_id"])
            member = next(
                (
                    m
                    for m in state["members"].values()
                    if m["repo_id"] == p["repo_id"] and m["user_id"] == p["user_id"]
                ),
                None,
            )
            if member is None:
                raise BusinessError(404, "membership_not_found")
            result = changes.put("members", {**member, "permission": p["permission"]})
        case "ticket.create_project":
            unique(state, "projects", key=p["key"])
            result = changes.create("projects", **p)
            status = 201
        case "ticket.create_ticket":
            require(state, "projects", p["project_id"])
            if p["assignee_id"] is not None:
                active_user(state, p["assignee_id"])
            result = changes.create("tickets", **p, status="open")
            status = 201
        case "ticket.assign_tickets":
            active_user(state, p["user_id"])
            ids = p["ticket_ids"]
            if len(set(ids)) != len(ids):
                raise BusinessError(422, "duplicate_ticket_ids")
            tickets = [require(state, "tickets", tid) for tid in ids]
            if partial and len(tickets) < 2:
                raise BusinessError(422, "partial_success_requires_multiple_items")
            completed = max(1, len(tickets) // 2) if partial else len(tickets)
            items = []
            for index, ticket in enumerate(tickets):
                if index < completed:
                    changes.put("tickets", {**ticket, "assignee_id": p["user_id"]})
                    items.append({"id": ticket["id"], "status": "assigned"})
                else:
                    items.append(
                        {
                            "id": ticket["id"],
                            "status": "failed",
                            "error": "injected_partial_failure",
                        }
                    )
            result = {"items": items, "succeeded": completed, "failed": len(tickets) - completed}
            status = 207 if partial else 200
        case "money.create_order":
            total = p["quantity"] * p["unit_price_cents"]
            changes.balance(-total)
            result = changes.create("orders", **p, total_cents=total, refunded_cents=0)
            status = 201
        case "money.bind_seat":
            order = require(state, "orders", p["order_id"])
            active_user(state, p["user_id"])
            if order["refunded_cents"]:
                raise BusinessError(409, "order_already_refunded")
            seats = [s for s in state["seats"].values() if s["active"]]
            if sum(s["order_id"] == order["id"] for s in seats) >= order["quantity"]:
                raise BusinessError(409, "purchased_seat_limit")
            if any(s["user_id"] == p["user_id"] and s["item"] == order["item"] for s in seats):
                raise BusinessError(409, "seat_already_bound")
            result = changes.create("seats", **p, item=order["item"], active=True)
            status = 201
        case "money.refund":
            order = require(state, "orders", p["order_id"])
            refunded = order["refunded_cents"] + p["amount_cents"]
            if refunded > order["total_cents"]:
                raise BusinessError(409, "refund_exceeds_payment")
            changes.balance(p["amount_cents"])
            changes.put("orders", {**order, "refunded_cents": refunded})
            if refunded == order["total_cents"]:
                for seat in list(state["seats"].values()):
                    if seat["order_id"] == order["id"]:
                        changes.put("seats", {**seat, "active": False})
            result = changes.create("refunds", **p)
            status = 201
        case _:
            raise BusinessError(404, "unknown_operation")
    return result, changes.diff, status
