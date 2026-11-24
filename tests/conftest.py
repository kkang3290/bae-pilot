import pytest

from tests.helpers import running


@pytest.fixture
async def runtime(tmp_path):
    async with running(tmp_path) as instance:
        yield instance
