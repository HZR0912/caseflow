"""Tenant relational directory. Organization relationships never grant API roles."""

from core.graph.approval_service import ApprovalError


class OrganizationService:
    def __init__(self, memory):
        self.memory = memory
        self.approvals = memory.approvals

    async def put(self, subject, body):
        await self.memory.require(subject, "organization.write")
        async with (
            self.approvals.session("organization:" + subject.tenant) as (conn, _),
            conn.transaction(),
        ):
            await conn.execute(
                "INSERT INTO org_versions VALUES (%s,0) ON CONFLICT DO NOTHING",
                (subject.tenant,),
            )
            row = await (
                await conn.execute(
                    "SELECT version FROM org_versions WHERE tenant=%s FOR UPDATE",
                    (subject.tenant,),
                )
            ).fetchone()
            if row["version"] != body.expected_version:
                raise ApprovalError(409, "organization_version_conflict")
            for table in ("org_systems", "org_members", "org_departments"):
                await conn.execute(f"DELETE FROM {table} WHERE tenant=%s", (subject.tenant,))
            for dept in body.departments:
                await conn.execute(
                    "INSERT INTO org_departments VALUES (%s,%s,%s,%s,%s)",
                    (subject.tenant, dept.id, dept.name, dept.parent_id, dept.active),
                )
            for member in body.members:
                await conn.execute(
                    "INSERT INTO org_members VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        subject.tenant,
                        member.id,
                        member.department_id,
                        member.manager_id,
                        member.mentor_id,
                        member.is_lead,
                        member.active,
                    ),
                )
            for system in body.systems:
                await conn.execute(
                    "INSERT INTO org_systems VALUES (%s,%s,%s,%s,%s)",
                    (
                        subject.tenant,
                        system.department_id,
                        system.kind,
                        system.resource_id,
                        system.owner_id,
                    ),
                )
            await conn.execute(
                "UPDATE org_versions SET version=version+1 WHERE tenant=%s", (subject.tenant,)
            )
        return {"status": "saved", "version": body.expected_version + 1}

    async def get(self, subject):
        await self.memory.require(subject, "organization.read")
        async with self.approvals.session("organization:" + subject.tenant) as (conn, _):
            version = await (
                await conn.execute(
                    "SELECT version FROM org_versions WHERE tenant=%s", (subject.tenant,)
                )
            ).fetchone()
            result = {"version": version["version"] if version else 0}
            for key, table in (
                ("departments", "org_departments"),
                ("members", "org_members"),
                ("systems", "org_systems"),
            ):
                rows = await (
                    await conn.execute(f"SELECT * FROM {table} WHERE tenant=%s", (subject.tenant,))
                ).fetchall()
                result[key] = [{k: v for k, v in row.items() if k != "tenant"} for row in rows]
            return result

    async def context(self, subject, department_id=None):
        await self.memory.require(subject, "organization.read")
        async with self.approvals.session("organization:" + subject.tenant) as (conn, _):
            member = await (
                await conn.execute(
                    "SELECT * FROM org_members WHERE tenant=%s AND id=%s AND active",
                    (subject.tenant, subject.id),
                )
            ).fetchone()
            dept = department_id or (member["department_id"] if member else None)
            version = await (
                await conn.execute(
                    "SELECT version FROM org_versions WHERE tenant=%s", (subject.tenant,)
                )
            ).fetchone()
            result = {
                "version": version["version"] if version else 0,
                "department_id": dept,
                "ancestors": [],
                "defaults": {},
                "sources": [],
                "mentor_id": None,
                "manager_chain": [],
            }
            if dept is None:
                return result
            # A visited path is an extra defence if data was changed outside the validated API.
            chain = await (
                await conn.execute(
                    "WITH RECURSIVE tree AS ("
                    "SELECT id,parent_id,name,active,0 AS depth,ARRAY[id] AS path "
                    "FROM org_departments "
                    "WHERE tenant=%s AND id=%s UNION ALL "
                    "SELECT d.id,d.parent_id,d.name,d.active,t.depth+1,t.path||d.id "
                    "FROM org_departments d JOIN tree t ON d.id=t.parent_id "
                    "WHERE d.tenant=%s AND NOT d.id=ANY(t.path) AND t.depth<99"
                    ") SELECT id,name,active,depth FROM tree ORDER BY depth",
                    (subject.tenant, dept, subject.tenant),
                )
            ).fetchall()
            if not chain or any(not d["active"] for d in chain):
                raise ApprovalError(409, "department_missing_or_inactive")
            result["ancestors"] = chain
            for department in chain:
                systems = await (
                    await conn.execute(
                        "SELECT s.kind,s.resource_id,s.owner_id FROM org_systems s "
                        "JOIN org_members m ON m.tenant=s.tenant AND m.id=s.owner_id "
                        "WHERE s.tenant=%s AND s.department_id=%s AND m.active",
                        (subject.tenant, department["id"]),
                    )
                ).fetchall()
                for system in systems:
                    if system["kind"] not in result["defaults"]:
                        result["defaults"][system["kind"]] = system["resource_id"]
                        result["sources"].append(
                            {
                                "field": system["kind"],
                                "department_id": department["id"],
                                "owner_id": system["owner_id"],
                            }
                        )
                if not result["mentor_id"]:
                    lead = await (
                        await conn.execute(
                            "SELECT id FROM org_members WHERE tenant=%s AND department_id=%s "
                            "AND active AND is_lead",
                            (subject.tenant, department["id"]),
                        )
                    ).fetchone()
                    result["mentor_id"] = lead["id"] if lead else None
            if member:
                managers = await (
                    await conn.execute(
                        "WITH RECURSIVE chain AS ("
                        "SELECT id,manager_id,0 AS depth,ARRAY[id] AS path FROM org_members "
                        "WHERE tenant=%s AND id=%s AND active UNION ALL "
                        "SELECT m.id,m.manager_id,c.depth+1,c.path||m.id FROM org_members m "
                        "JOIN chain c ON m.id=c.manager_id WHERE m.tenant=%s AND m.active "
                        "AND NOT m.id=ANY(c.path) AND c.depth<99"
                        ") SELECT id FROM chain WHERE depth>0 ORDER BY depth",
                        (subject.tenant, subject.id, subject.tenant),
                    )
                ).fetchall()
                result["manager_chain"] = [row["id"] for row in managers]
                if member["department_id"] == dept and member["mentor_id"]:
                    mentor = await (
                        await conn.execute(
                            "SELECT id FROM org_members WHERE tenant=%s AND id=%s AND active",
                            (subject.tenant, member["mentor_id"]),
                        )
                    ).fetchone()
                    if mentor:
                        result["mentor_id"] = mentor["id"]
            return result
