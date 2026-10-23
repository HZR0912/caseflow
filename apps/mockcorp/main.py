"""MockCorp HTTP 契约：4 个系统、13 个写操作，所有写入走同一事务执行入口。"""

from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from apps.api.auth import SubjectDep
from apps.api.settings import get_settings
from apps.mockcorp.domain import COLLECTIONS
from apps.mockcorp.models import (
    AssignTickets,
    BindSeat,
    CreateMailbox,
    CreateOrder,
    CreateProject,
    CreateRepo,
    CreateTicket,
    CreateUser,
    Input,
    InviteMember,
    MockSettings,
    Refund,
    ResetPassword,
    SetPermission,
)
from apps.mockcorp.service import MockCorp
from apps.mockcorp.store import PostgresStore, Store


class Options:
    def __init__(
        self,
        dry_run: bool = False,
        fault: Annotated[str | None, Query(max_length=50)] = None,
        key: Annotated[
            str | None, Header(alias="Idempotency-Key", pattern=r"^[A-Za-z0-9._:-]{1,200}$")
        ] = None,
    ):
        self.dry_run = dry_run
        self.fault = fault
        self.key = key


OptionsDep = Annotated[Options, Depends()]


def get_service(request: Request) -> MockCorp:
    return request.app.state.mockcorp


ServiceDep = Annotated[MockCorp, Depends(get_service)]


def write(service, subject, options, operation, body, **path) -> JSONResponse:
    payload = body.model_dump(mode="json")
    if isinstance(body, ResetPassword):
        # SecretStr 的序列化掩码不能用于请求摘要：不同密码必须产生不同指纹。
        payload["new_password"] = body.new_password.get_secret_value()
    reply = service.execute(
        subject.tenant,
        operation,
        {**payload, **path},
        key=options.key,
        dry_run=options.dry_run,
        fault=options.fault,
        actor=subject.id,
    )
    return JSONResponse(reply.body, status_code=reply.status, headers=reply.headers)


def create_app(store: Store | None = None, settings: MockSettings | None = None) -> FastAPI:
    config = settings or MockSettings()
    app = FastAPI(title="MockCorp API", version="0.2.0", description="仅供虚构数据演示与故障评测")
    app.state.mockcorp = MockCorp(
        store or PostgresStore(get_settings().database_url, config), config
    )

    @app.exception_handler(psycopg.Error)
    def database_error(request: Request, exc: psycopg.Error):
        return JSONResponse(
            {"error": {"code": "database_unavailable", "retryable": True}}, status_code=503
        )

    @app.get("/healthz")
    def health(service: ServiceDep):
        service.store.health()
        return {"status": "ok", "service": "mockcorp"}

    @app.get("/v1/state", tags=["inspection"])
    def state(subject: SubjectDep, service: ServiceDep):
        return service.store.snapshot(subject.tenant)

    @app.get("/v1/oplog", tags=["inspection"])
    def oplog(
        subject: SubjectDep,
        service: ServiceDep,
        after_id: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ):
        """本租户的操作日志，按 id 升序。after_id 是增量游标，可反复拉取。

        这是**供给方视角**的事实记录：Agent 说自己做了什么，可以拿这里交叉核对。
        """
        return service.store.oplog(subject.tenant, after_id=after_id, limit=limit)

    @app.get("/v1/{system}/{collection}", tags=["inspection"])
    def listing(system: str, collection: str, subject: SubjectDep, service: ServiceDep):
        if collection not in COLLECTIONS.get(system, ()):
            raise HTTPException(404, "unknown_collection")
        snapshot = service.store.snapshot(subject.tenant)
        return {"items": list(snapshot[collection].values())}

    @app.get("/v1/{system}/{collection}/{resource_id}", tags=["inspection"])
    def resource(
        system: str, collection: str, resource_id: str, subject: SubjectDep, service: ServiceDep
    ):
        if collection not in COLLECTIONS.get(system, ()):
            raise HTTPException(404, "unknown_collection")
        result = service.store.snapshot(subject.tenant)[collection].get(resource_id)
        if result is None:
            raise HTTPException(404, "resource_not_found")
        return result

    @app.post("/v1/identity/users", tags=["identity"])
    def create_user(
        body: CreateUser, subject: SubjectDep, options: OptionsDep, service: ServiceDep
    ):
        return write(service, subject, options, "identity.create_user", body)

    @app.post("/v1/identity/mailboxes", tags=["identity"])
    def create_mailbox(
        body: CreateMailbox, subject: SubjectDep, options: OptionsDep, service: ServiceDep
    ):
        return write(service, subject, options, "identity.create_mailbox", body)

    @app.post("/v1/identity/users/{user_id}/password", tags=["identity"])
    def password(
        user_id: str,
        body: ResetPassword,
        subject: SubjectDep,
        options: OptionsDep,
        service: ServiceDep,
    ):
        return write(service, subject, options, "identity.reset_password", body, user_id=user_id)

    @app.post("/v1/identity/users/{user_id}/disable", tags=["identity"])
    def disable(
        user_id: str, body: Input, subject: SubjectDep, options: OptionsDep, service: ServiceDep
    ):
        return write(service, subject, options, "identity.disable_user", body, user_id=user_id)

    @app.post("/v1/code/repos", tags=["code"])
    def create_repo(
        body: CreateRepo, subject: SubjectDep, options: OptionsDep, service: ServiceDep
    ):
        return write(service, subject, options, "code.create_repo", body)

    @app.post("/v1/code/repos/{repo_id}/members", tags=["code"])
    def invite(
        repo_id: str,
        body: InviteMember,
        subject: SubjectDep,
        options: OptionsDep,
        service: ServiceDep,
    ):
        return write(service, subject, options, "code.invite_member", body, repo_id=repo_id)

    @app.patch("/v1/code/repos/{repo_id}/members/{user_id}", tags=["code"])
    def permission(
        repo_id: str,
        user_id: str,
        body: SetPermission,
        subject: SubjectDep,
        options: OptionsDep,
        service: ServiceDep,
    ):
        return write(
            service, subject, options, "code.set_permission", body, repo_id=repo_id, user_id=user_id
        )

    @app.post("/v1/ticket/projects", tags=["ticket"])
    def create_project(
        body: CreateProject, subject: SubjectDep, options: OptionsDep, service: ServiceDep
    ):
        return write(service, subject, options, "ticket.create_project", body)

    @app.post("/v1/ticket/tickets", tags=["ticket"])
    def create_ticket(
        body: CreateTicket, subject: SubjectDep, options: OptionsDep, service: ServiceDep
    ):
        return write(service, subject, options, "ticket.create_ticket", body)

    @app.post("/v1/ticket/tickets/assign", tags=["ticket"])
    def assign(body: AssignTickets, subject: SubjectDep, options: OptionsDep, service: ServiceDep):
        return write(service, subject, options, "ticket.assign_tickets", body)

    @app.post("/v1/money/orders", tags=["money"])
    def order(body: CreateOrder, subject: SubjectDep, options: OptionsDep, service: ServiceDep):
        return write(service, subject, options, "money.create_order", body)

    @app.post("/v1/money/seats", tags=["money"])
    def seat(body: BindSeat, subject: SubjectDep, options: OptionsDep, service: ServiceDep):
        return write(service, subject, options, "money.bind_seat", body)

    @app.post("/v1/money/refunds", tags=["money"])
    def refund(body: Refund, subject: SubjectDep, options: OptionsDep, service: ServiceDep):
        return write(service, subject, options, "money.refund", body)

    return app


app = create_app()
