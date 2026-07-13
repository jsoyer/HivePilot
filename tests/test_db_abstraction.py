"""Tests for hivepilot.services.db abstraction layer."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from hivepilot.services import db


class TestIsPostgres:
    def test_none_url_is_sqlite(self, monkeypatch):
        monkeypatch.setattr(db.settings, "database_url", None)
        assert db.is_postgres() is False

    def test_sqlite_url_is_sqlite(self, monkeypatch):
        monkeypatch.setattr(db.settings, "database_url", "sqlite:///foo.db")
        assert db.is_postgres() is False

    def test_postgres_url(self, monkeypatch):
        monkeypatch.setattr(db.settings, "database_url", "postgresql://user:pw@host/db")
        assert db.is_postgres() is True

    def test_postgres_short_url(self, monkeypatch):
        monkeypatch.setattr(db.settings, "database_url", "postgres://user:pw@host/db")
        assert db.is_postgres() is True


class TestPh:
    def test_sqlite_noop(self, monkeypatch):
        monkeypatch.setattr(db.settings, "database_url", None)
        assert db.ph("SELECT * WHERE id = ?") == "SELECT * WHERE id = ?"

    def test_postgres_translates(self, monkeypatch):
        monkeypatch.setattr(db.settings, "database_url", "postgresql://x/y")
        assert db.ph("SELECT * WHERE id = ? AND x = ?") == "SELECT * WHERE id = %s AND x = %s"


class TestAutoincrementPk:
    def test_sqlite_returns_autoincrement(self, monkeypatch):
        monkeypatch.setattr(db.settings, "database_url", None)
        assert db.autoincrement_pk() == "INTEGER PRIMARY KEY AUTOINCREMENT"

    def test_postgres_returns_bigserial(self, monkeypatch):
        monkeypatch.setattr(db.settings, "database_url", "postgresql://x/y")
        assert db.autoincrement_pk() == "BIGSERIAL PRIMARY KEY"


class TestColumnExists:
    def test_column_exists_sqlite(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db.settings, "database_url", None)
        conn = sqlite3.connect(tmp_path / "test.db")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE foo (id INTEGER PRIMARY KEY, name TEXT)")
        conn.commit()

        assert db.column_exists(conn, "foo", "name") is True
        assert db.column_exists(conn, "foo", "nonexistent") is False
        conn.close()

    def test_column_exists_case_sensitive_sqlite(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db.settings, "database_url", None)
        conn = sqlite3.connect(tmp_path / "test2.db")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE bar (id INTEGER PRIMARY KEY, MyCol TEXT)")
        conn.commit()

        assert db.column_exists(conn, "bar", "MyCol") is True
        assert db.column_exists(conn, "bar", "mycol") is False
        conn.close()


class TestConnect:
    def test_connect_sqlite_default(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db.settings, "database_url", None)
        monkeypatch.setattr(db, "_sqlite_path", lambda: tmp_path / "test.db")

        with db.connect() as conn:
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
            conn.execute("INSERT INTO t (v) VALUES (?)", ("hello",))

        with db.connect() as conn:
            row = conn.execute("SELECT v FROM t").fetchone()
            assert row["v"] == "hello"  # dict-accessible by name

    def test_connect_sqlite_row_factory(self, monkeypatch, tmp_path):
        """Rows returned from connect() are accessible by column name."""
        monkeypatch.setattr(db.settings, "database_url", None)
        monkeypatch.setattr(db, "_sqlite_path", lambda: tmp_path / "test.db")

        with db.connect() as conn:
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, val INTEGER)")
            conn.execute("INSERT INTO t (name, val) VALUES (?, ?)", ("alice", 42))

        with db.connect() as conn:
            row = conn.execute("SELECT * FROM t").fetchone()
            assert row["name"] == "alice"
            assert row["val"] == 42

    def test_connect_sqlite_rollback_on_exception(self, monkeypatch, tmp_path):
        """Exception inside with block causes rollback."""
        monkeypatch.setattr(db.settings, "database_url", None)
        monkeypatch.setattr(db, "_sqlite_path", lambda: tmp_path / "test.db")

        with db.connect() as conn:
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")

        with pytest.raises(RuntimeError):
            with db.connect() as conn:
                conn.execute("INSERT INTO t (v) VALUES (?)", ("will-be-rolled-back",))
                raise RuntimeError("test error")

        with db.connect() as conn:
            rows = conn.execute("SELECT * FROM t").fetchall()
            assert rows == []

    def test_connect_postgres_missing_psycopg(self, monkeypatch):
        monkeypatch.setattr(db.settings, "database_url", "postgresql://localhost/test")

        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "psycopg":
                raise ImportError("No module named 'psycopg'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            with pytest.raises(ImportError, match="psycopg is required"):
                with db.connect() as _:
                    pass


class TestInsertReturningId:
    def test_sqlite_returns_lastrowid(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db.settings, "database_url", None)
        monkeypatch.setattr(db, "_sqlite_path", lambda: tmp_path / "test.db")

        with db.connect() as conn:
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)")

        with db.connect() as conn:
            row_id = db.insert_returning_id(
                conn,
                "INSERT INTO items (name) VALUES (?)",
                ("first",),
            )
            assert isinstance(row_id, int)
            assert row_id >= 1

        with db.connect() as conn:
            row_id2 = db.insert_returning_id(
                conn,
                "INSERT INTO items (name) VALUES (?)",
                ("second",),
            )
            assert row_id2 > row_id

    def test_sqlite_ids_are_sequential(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db.settings, "database_url", None)
        monkeypatch.setattr(db, "_sqlite_path", lambda: tmp_path / "test.db")

        with db.connect() as conn:
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)")
            id1 = db.insert_returning_id(conn, "INSERT INTO items (name) VALUES (?)", ("a",))
            id2 = db.insert_returning_id(conn, "INSERT INTO items (name) VALUES (?)", ("b",))
            assert id2 == id1 + 1
