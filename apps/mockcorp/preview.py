"""依赖感知的批量预演：同一私有事务状态内推进，最终不提交业务或幂等收据。"""

from __future__ import annotations

from core.graph.contracts import Plan, Ref
from pydantic import ValidationError

from apps.mockcorp.domain import BusinessError, apply
from apps.mockcorp.models import (
    AssignTickets,
    BindSeat,
    CreateMailbox,
    CreateOrder,
    CreateTicket,
    CreateUser,
    Identifier,
    InviteMember,
)
from apps.mockcorp.store import LogEntry, Outcome, Reply, Store


class InviteInRepo(InviteMember):
    repo_id: Identifier


SCHEMAS = {
    "identity.create_user": CreateUser,
    "identity.create_mailbox": CreateMailbox,
    "code.invite_member": InviteInRepo,
    "ticket.create_ticket": CreateTicket,
    "money.create_order": CreateOrder,
    "money.bind_seat": BindSeat,
    "ticket.assign_tickets": AssignTickets,
}


def preview_plan(store: Store, tenant: str, actor: str, plan: Plan) -> Reply:
    def handle(state: dict, receipt: dict | None) -> Outcome:
        results: dict[str, dict] = {}
        items = []
        failed = False
        for step in plan.ordered_steps():
            if any(dep not in results for dep in step.depends_on):
                items.append({"step_id": step.id, "status": "skipped", "code": "dependency_failed"})
                continue
            try:
                args = {
                    key: results[value.step][value.field] if isinstance(value, Ref) else value
                    for key, value in step.arguments.items()
                }
                payload = SCHEMAS[step.operation].model_validate(args).model_dump(mode="json")
                result, diff, _ = apply(state, step.operation, payload)
                results[step.id] = result
                items.append({"step_id": step.id, "status": "ok", "result": result, "diff": diff})
            except (BusinessError, ValidationError) as exc:
                failed = True
                code = exc.code if isinstance(exc, BusinessError) else "invalid_tool_arguments"
                items.append({"step_id": step.id, "status": "conflict", "code": code})
        return Outcome(
            Reply(200, {"dry_run": True, "ok": not failed, "steps": items}),
            persist=False,
            log=LogEntry(dry_run=True),
        )

    return store.transact(tenant, None, handle, operation="plan.preview", actor=actor)
