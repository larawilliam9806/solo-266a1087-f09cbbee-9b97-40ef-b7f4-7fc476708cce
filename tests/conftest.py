import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "test.sqlite3"
    app = create_app(str(db))
    token = app.state.bootstrap_token
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {token}"})
        c.editor_token = token
        yield c


def auth(token):
    return {"Authorization": f"Bearer {token}"}
