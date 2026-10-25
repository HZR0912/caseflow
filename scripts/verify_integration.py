"""Run database tests in a new isolated database, then remove only that database."""

import os
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
from apps.api.settings import get_settings
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo


def main():
    url = get_settings().database_url.replace("postgresql+psycopg://", "postgresql://")
    params = conninfo_to_dict(url)
    name = "caseflow_verify_" + uuid4().hex
    admin = make_conninfo(**{**params, "dbname": "postgres"})
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        env = {**os.environ, "DATABASE_URL": urlunsplit(urlsplit(url)._replace(path="/" + name))}
        for target in ("head", "base", "head"):
            command = "downgrade" if target == "base" else "upgrade"
            subprocess.run([sys.executable, "-m", "alembic", command, target], env=env, check=True)
        subprocess.run(
            [sys.executable, "-m", "pytest", *(sys.argv[1:] or ["-m", "integration", "-q"])],
            env=env,
            check=True,
        )
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        print("Isolated verification database removed.")


if __name__ == "__main__":
    main()
