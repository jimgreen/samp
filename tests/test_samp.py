import sqlite3
import calendar

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

