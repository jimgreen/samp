import sqlite3
import calendar
import pytest
from fastapi.testclient import TestClient

from samp.app import create_app
from samp.store import Store


def test_definition_and_yearly_result_layout(tmp_path):
    definition_db = tmp_path / "samp_define.db"
    source_db = tmp_path / "rtdb.db"
    store = Store(definition_db, source_db, tmp_path / "results")
    store.replace_source_rows("meter", [{"row_key": "A", "values": {"voltage": 220.5, "current": 3.2}}], source_db)
    definition = store.upsert_definition({"name": "电表A", "source_database": str(source_db), "source_table": "meter", "fields": ["voltage", "current"], "interval_seconds": 60})
    assert set(definition) >= {"id", "samp_name", "source_database", "source_table", "fields", "interval_seconds"}
    result = store.run_sample(definition["id"], calendar.timegm((2026, 1, 2, 0, 0, 0)))
    assert result["table"] == "samp_电表A_2026"
    db = sqlite3.connect(result["database"])
    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert tables == [("samp_电表A_2026",)]
    rows = store.query_samples(definition["id"], result["sampled_at"] - 1, result["sampled_at"] + 1)
    assert rows["rows"][0]["voltage"] == 220.5
    assert sqlite3.connect(source_db).execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("meter",)]


def test_primary_key_filter_and_selected_columns(tmp_path):
    definition_db = tmp_path / "samp_define.db"
    source_db = tmp_path / "source.db"
    with sqlite3.connect(source_db) as conn:
        conn.execute("CREATE TABLE meter (id INTEGER PRIMARY KEY, voltage REAL, status INTEGER, note TEXT)")
        conn.executemany("INSERT INTO meter(id,voltage,status,note) VALUES(?,?,?,?)", [(7, 220.1, 1, "keep"), (8, 221.2, 0, "skip")])
    store = Store(definition_db, source_db, tmp_path / "results")
    definition = store.upsert_definition({"name": "meter_primary", "source_database": str(source_db), "source_table": "meter", "fields": ["voltage"], "filter": "status=1", "interval_seconds": 0})
    result = store.run_sample(definition["id"], calendar.timegm((2026, 2, 1, 0, 0, 0)))
    assert result["rows_written"] == 1
    rows = store.query_samples(definition["id"], result["sampled_at"] - 1, result["sampled_at"] + 1)
    assert rows["rows"] == [{"sample_time": result["sampled_at"], "row_key": "7", "voltage": 220.1}]


def test_query_crosses_year_and_rejects_unknown_field(tmp_path):
    source_db = tmp_path / "source.db"
    store = Store(tmp_path / "samp_define.db", source_db, tmp_path / "results")
    store.replace_source_rows("meter", [{"row_key": "A", "values": {"voltage": 220.5}}], source_db)
    definition = store.upsert_definition({"name": "cross_year", "source_database": str(source_db), "source_table": "meter", "fields": ["voltage"], "interval_seconds": 0})
    first = store.run_sample(definition["id"], calendar.timegm((2025, 12, 31, 23, 59, 59)))
    second = store.run_sample(definition["id"], calendar.timegm((2026, 1, 1, 0, 0, 1)))
    result = store.query_samples(definition["id"], first["sampled_at"], second["sampled_at"])
    assert len(result["rows"]) == 2
    assert set(result["tables"]) == {"samp_cross_year_2025", "samp_cross_year_2026"}
    with pytest.raises(ValueError, match="查询字段"):
        store.query_samples(definition["id"], first["sampled_at"], second["sampled_at"], ["unknown"])


def test_query_pagination_crosses_year_without_stalling(tmp_path):
    source_db = tmp_path / "source.db"
    store = Store(tmp_path / "samp_define.db", source_db, tmp_path / "results")
    store.replace_source_rows("meter", [{"row_key": "A", "values": {"voltage": 220.5}}], source_db)
    definition = store.upsert_definition({"name": "paged", "source_database": str(source_db), "source_table": "meter", "fields": ["voltage"], "interval_seconds": 0})
    for sampled_at in (
        calendar.timegm((2025, 12, 31, 23, 59, 58)),
        calendar.timegm((2025, 12, 31, 23, 59, 59)),
        calendar.timegm((2026, 1, 1, 0, 0, 0)),
    ):
        store.run_sample(definition["id"], sampled_at)
    start = calendar.timegm((2025, 1, 1, 0, 0, 0))
    end = calendar.timegm((2026, 12, 31, 23, 59, 59))
    first = store.query_samples(definition["id"], start, end, ["voltage"], limit=2, offset=0)
    second = store.query_samples(definition["id"], start, end, ["voltage"], limit=2, offset=2)
    assert first["total_rows"] == 3
    assert len(first["rows"]) == 2
    assert len(second["rows"]) == 1
    assert second["rows"][0]["sample_time"] == calendar.timegm((2026, 1, 1, 0, 0, 0))


def test_status_counts_only_result_databases(tmp_path):
    root = tmp_path / "sqlite"
    root.mkdir()
    store = Store(root / "samp_define.db", root / "source.db", root)
    assert store.status()["result_databases"] == 0
    (root / "samp_point_2026.db").touch()
    assert store.status()["result_databases"] == 1


def test_composite_primary_key_is_preserved(tmp_path):
    source_db = tmp_path / "source.db"
    with sqlite3.connect(source_db) as conn:
        conn.execute("CREATE TABLE meter (site TEXT, channel INTEGER, value REAL, PRIMARY KEY(site, channel)) WITHOUT ROWID")
        conn.executemany("INSERT INTO meter VALUES(?,?,?)", [("north", 1, 10.0), ("north", 2, 20.0)])
    store = Store(tmp_path / "samp_define.db", source_db, tmp_path / "results")
    definition = store.upsert_definition({"name": "composite", "source_database": str(source_db), "source_table": "meter", "fields": ["value"], "interval_seconds": 0})
    result = store.run_sample(definition["id"], calendar.timegm((2026, 3, 1, 0, 0, 0)))
    rows = store.query_samples(definition["id"], result["sampled_at"], result["sampled_at"])
    assert [row["row_key"] for row in rows["rows"]] == ["north|1", "north|2"]


def test_web_api_definition_sample_and_query_flow(tmp_path):
    source_db = tmp_path / "source.db"
    with sqlite3.connect(source_db) as conn:
        conn.execute("CREATE TABLE meter (id INTEGER PRIMARY KEY, voltage REAL)")
        conn.execute("INSERT INTO meter VALUES(1, 220.5)")
    app = create_app(tmp_path / "samp_define.db", source_db, tmp_path / "results")
    with TestClient(app) as client:
        inspect = client.get("/api/source/inspect", params={"source_database": str(source_db), "source_table": "meter"})
        assert inspect.status_code == 200
        definition = client.post("/api/definitions", json={"name": "api_meter", "source_database": str(source_db), "source_table": "meter", "fields": ["voltage"], "interval_seconds": 0}).json()
        sampled = client.post(f"/api/sample/{definition['id']}").json()
        query = client.get(f"/api/query/{definition['id']}", params={"start": sampled["sampled_at"] - 1, "end": sampled["sampled_at"] + 1, "fields": "voltage"})
        assert query.status_code == 200
        assert query.json()["rows"][0]["voltage"] == 220.5
        assert client.get(f"/api/query/{definition['id']}", params={"start": 0, "end": 1, "fields": "missing"}).status_code == 400


def test_web_api_query_pagination_metadata(tmp_path):
    source_db = tmp_path / "source.db"
    with sqlite3.connect(source_db) as conn:
        conn.execute("CREATE TABLE meter (id INTEGER PRIMARY KEY, voltage REAL)")
        conn.executemany("INSERT INTO meter VALUES (?, ?)", [(1, 220.1), (2, 220.2), (3, 220.3)])
    app = create_app(tmp_path / "samp_define.db", source_db, tmp_path / "results")
    with TestClient(app) as client:
        definition = client.post("/api/definitions", json={"name": "api_paged", "source_database": str(source_db), "source_table": "meter", "fields": ["voltage"], "interval_seconds": 0}).json()
        sampled = client.post(f"/api/sample/{definition['id']}").json()
        result = client.get(f"/api/query/{definition['id']}", params={"start": sampled["sampled_at"], "end": sampled["sampled_at"], "fields": "voltage", "limit": 2, "offset": 1})
        assert result.status_code == 200
        body = result.json()
        assert body["total_rows"] == 3
        assert body["limit"] == 2 and body["offset"] == 1
        assert len(body["rows"]) == 2

