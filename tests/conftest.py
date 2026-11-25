import os
import uuid

import psycopg
import pytest
from psycopg import sql

from tests.helpers import running


@pytest.fixture
def db_config():
    """Real PostgreSQL, with one owned disposable schema per test; never erase a database."""
    url = os.environ.get("BAEPILOT_TEST_DATABASE_URL")
    if not url:
        pytest.fail("Set BAEPILOT_TEST_DATABASE_URL to a dedicated PostgreSQL test database")
    schema = "baepilot_test_" + uuid.uuid4().hex
    owner = "baepilot-test:" + uuid.uuid4().hex
    with psycopg.connect(url) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        connection.execute(
            sql.SQL("COMMENT ON SCHEMA {} IS {}").format(sql.Identifier(schema), sql.Literal(owner))
        )
    try:
        yield {"database_url": url, "database_schema": schema}
    finally:
        with psycopg.connect(url) as connection:
            row = connection.execute(
                "SELECT obj_description(oid, 'pg_namespace') FROM pg_namespace WHERE nspname=%s",
                (schema,),
            ).fetchone()
            if row is None or row[0] != owner:
                raise RuntimeError("test_schema_ownership_lost")
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
async def runtime(db_config):
    async with running(db_config) as instance:
        yield instance
