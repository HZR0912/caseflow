"""Process-only integration helper. No policy or tool network dependencies."""

import argparse
import asyncio
import json
import os
import selectors
import sys

from core.graph.approval_service import ApprovalService
from core.graph.onboarding import PlanningRequest
from core.policy.opa_client import PolicySubject

from tests.test_approval_integration import ApprovalPolicy, choice
from tests.test_planning import TEXT, TODAY, invoke


async def run(args):
    service = ApprovalService(os.environ["DATABASE_URL"], ApprovalPolicy())
    owner = PolicySubject(id="requester", role="admin", tenant=args.tenant)
    reviewer = PolicySubject(id="reviewer", role="admin", tenant=args.tenant)
    if args.command == "start":

        async def prepare():
            return (await invoke())[0]

        result = await service.create(
            PlanningRequest(text=TEXT, today=TODAY), owner, args.key, prepare
        )
        print(json.dumps(result, default=str), flush=True)
        await asyncio.Event().wait()  # Parent kills us after the persisted interrupt is observed.
    else:
        before = await service.get(args.request_id, reviewer)
        after = await service.decide(args.request_id, choice(before), reviewer)
        print(json.dumps({"before": before, "after": after}, default=str), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["start", "resume"])
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--key", default="")
    parser.add_argument("--request-id", default="")
    args = parser.parse_args()
    factory = (
        (lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
        if sys.platform == "win32"
        else None
    )
    asyncio.run(run(args), loop_factory=factory)
