import sqlite3
import unittest
from pathlib import Path

from api.main import app
from api.categories import CategoryCreate
from database.schema import initialize_schema


class ApiTests(unittest.TestCase):
    def test_category_model(self):
        item = CategoryCreate(marketplace="US", node_id="123", name="Toys", level=1)
        self.assertEqual(item.level, 1)

    def test_categories_schema_exists(self):
        db = Path("data") / "test_api.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.unlink(missing_ok=True)
        conn = sqlite3.connect(db)
        initialize_schema(conn)
        row = conn.execute("SELECT name FROM sqlite_master WHERE name='categories'").fetchone()
        conn.close()
        db.unlink(missing_ok=True)
        self.assertEqual(row[0], "categories")

    def test_routes_registered(self):
        paths = {getattr(route, "path", "") for route in app.routes if hasattr(route, "path")}
        self.assertIn("/", paths)
        self.assertIn("/categories", paths)
        self.assertTrue(any(route.path.startswith("/api/categories") for route in app.routes if hasattr(route, "path")) or any("categories" in str(route) for route in app.routes))
        self.assertIn("/api/status", paths)


if __name__ == "__main__":
    unittest.main()
