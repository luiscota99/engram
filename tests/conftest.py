import pytest
import os
from src.database import get_connection, init_db

@pytest.fixture
def test_db(tmp_path):
    """Fixture to provide a clean, temporary database file for testing."""
    db_file = tmp_path / "test_memory.db"
    db_path = str(db_file)
    
    os.environ["ENGRAM_DB_PATH"] = db_path
    init_db(db_path)
    
    with get_connection(db_path) as conn:
        yield {"conn": conn, "path": db_path}
