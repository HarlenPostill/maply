import os
import tempfile

# Tests use their own database and a throwaway data folder.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://maply:maply@127.0.0.1:5433/maply_test")
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="maply-test-")
