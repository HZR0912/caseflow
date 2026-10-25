"""Layered recall integrated into planning, with field provenance frozen into approval."""

from core.graph.onboarding import OnboardingFacts
from core.memory.organization import OrganizationService
from core.memory.sessions import SessionService


class RecallInterpreter:
    def __init__(self, base, memory, subject):
        self.base, self.memory, self.subject = base, memory, subject
        self.name = base.name
        self.context = None

    async def understand(self, request):
        current = await self.base.understand(request)
        self.name = self.base.name
        selection = request.memory
        if selection is None:
            return current
        # Model fallback dumps defaults, so use only values actually present;
        # explicit facts (including null/empty) are always authoritative.
        explicit = {
            k: v for k, v in current.model_dump(mode="json").items() if v is not None and v != []
        }
        explicit.update(request.facts.model_dump(mode="json", exclude_unset=True))
        sessions, preferences = {}, []
        if selection.session_id is not None:
            sessions = await SessionService(self.memory).facts(
                self.subject, str(selection.session_id), selection.session_version
            )
        if selection.use_preferences:
            preferences = await self.memory.preferences(self.subject, request.text)
        pref = {p["key"]: p["value"] for p in preferences}
        dept = explicit.get(
            "department_id", sessions.get("department_id", pref.get("department_id"))
        )
        organization = None
        if selection.use_organization:
            organization = await OrganizationService(self.memory).context(self.subject, dept)
        values, sources = {}, []
        layers = [
            (
                "organization",
                (
                    {**organization["defaults"], "department_id": organization["department_id"]}
                    if organization
                    else {}
                ),
            ),
            ("preference", pref),
            ("session", sessions),
            ("explicit", explicit),
        ]
        for source, fields in layers:
            for key, value in fields.items():
                values[key] = value
                sources = [s for s in sources if s["field"] != key]
                sources.append({"field": key, "source": source})
        result = OnboardingFacts.model_validate(values)
        self.context = {
            "sources": sources,
            "session": {"id": str(selection.session_id), "version": selection.session_version}
            if selection.session_id
            else None,
            "preferences": [
                {
                    "key": p["key"],
                    "version": p["version"],
                    "score": p["score"],
                    "embedding_model": p["embedding_model"],
                }
                for p in preferences
            ],
            "organization": organization,
            "history": await self.memory.history(self.subject),
            "organization_history": (
                await self.memory.history(self.subject, organization["department_id"])
                if organization and organization["department_id"]
                else []
            ),
            "notice": "记忆仅补全缺失信息；不授予权限，不跳过预演或审批。",
        }
        return result
