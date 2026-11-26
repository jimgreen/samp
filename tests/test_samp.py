import sqlite3
import calendar
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from samp.app import create_app
from samp.rtdb_schema import REGISTRY_TABLES
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
    with closing(sqlite3.connect(result["database"])) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {"samp_电表A_2026", *REGISTRY_TABLES}
        assert db.execute("SELECT storage_format FROM rtdb_schema_database").fetchone() == ("native",)
        assert embedded_columns(db, result["table"]) == [
            ("sample_time", "int", 1), ("row_key", "str", 1),
            ("voltage", "double", 0), ("current", "double", 0),
        ]
    rows = store.query_samples(definition["id"], result["sampled_at"] - 1, result["sampled_at"] + 1)
    assert rows["rows"][0]["voltage"] == 220.5
    assert store.source_tables(source_db) == ["meter"]


def embedded_columns(db, table):
    return db.execute(
        "SELECT eng_name,data_type,primary_key FROM rtdb_schema_columns WHERE table_name=? ORDER BY ordinal",
        (table,),
    ).fetchall()


def test_legacy_definition_and_result_databases_are_registered_in_place(tmp_path):
    definition_db = tmp_path / "samp_define.db"
    result_db = tmp_path / "samp_meter_2025.db"
    with closing(sqlite3.connect(definition_db)) as db, db:
        db.execute("CREATE TABLE samp_define(id INTEGER PRIMARY KEY, samp_name TEXT NOT NULL)")
        db.execute("INSERT INTO samp_define VALUES (7, 'meter')")
    with closing(sqlite3.connect(result_db)) as db, db:
        db.execute("CREATE TABLE samp_meter_2025(sample_time INTEGER, row_key TEXT, voltage REAL, PRIMARY KEY(sample_time,row_key))")
        db.execute("INSERT INTO samp_meter_2025 VALUES (1735689600, 'A', 220.5)")

    Store(definition_db, tmp_path / "missing.db", tmp_path)
    Store(definition_db, tmp_path / "missing.db", tmp_path)
    with closing(sqlite3.connect(definition_db)) as db:
        assert embedded_columns(db, "samp_define") == [
            ("id", "int", 1), ("samp_name", "str", 0), ("source_database", "str", 0),
        ]
        assert db.execute("SELECT * FROM samp_define").fetchall() == [(7, "meter", "")]
    with closing(sqlite3.connect(result_db)) as db:
        assert embedded_columns(db, "samp_meter_2025")[-1] == ("voltage", "double", 0)
        assert db.execute("SELECT * FROM samp_meter_2025").fetchall() == [(1735689600, "A", 220.5)]
    assert not (tmp_path / "missing.db").exists()


def test_added_fields_sync_schema_and_preserve_old_years(tmp_path):
    source = tmp_path / "source.db"
    store = Store(tmp_path / "samp_define.db", source, tmp_path)
    store.replace_source_rows("meter", [{"row_key": "A", "values": {"voltage": 220.5}}])
    payload = {"name": "电表", "source_table": "meter", "fields": ["voltage"], "interval_seconds": 0}
    definition = store.upsert_definition(payload)
    old_time = calendar.timegm((2025, 12, 31, 23, 59, 59))
    new_time = old_time + 1
    store.run_sample(definition["id"], old_time)
    current = store.run_sample(definition["id"], new_time)

    store.replace_source_rows("meter", [{"row_key": "A", "values": {"电流": 1.5, "voltage": 221}}])
    store.upsert_definition({**payload, "fields": ["电流"]}, definition["id"])
    store.run_sample(definition["id"], new_time)

    with closing(sqlite3.connect(source)) as db:
        assert embedded_columns(db, "meter")[-1] == ("电流", "double", 0)
    with closing(sqlite3.connect(current["database"])) as db:
        assert embedded_columns(db, current["table"])[-2:] == [
            ("voltage", "double", 0), ("电流", "double", 0),
        ]
        assert db.execute(f'SELECT voltage, 电流 FROM "{current["table"]}"').fetchall() == [(220.5, 1.5)]
    rows = store.query_samples(definition["id"], old_time, new_time)["rows"]
    assert [row["电流"] for row in rows] == [None, 1.5]


def test_source_import_rolls_back_schema_and_registry_on_failed_write(tmp_path):
    source = tmp_path / "source.db"
    store = Store(tmp_path / "samp_define.db", source, tmp_path)
    store.replace_source_rows("meter", [{"row_key": "A", "values": {"voltage": 220.5}}])
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("CREATE TRIGGER reject_import BEFORE INSERT ON meter BEGIN SELECT RAISE(ABORT, 'rejected'); END")

    with pytest.raises(sqlite3.IntegrityError, match="rejected"):
        store.replace_source_rows("meter", [{"row_key": "A", "values": {"電流": 1.5}}])

    with closing(sqlite3.connect(source)) as db:
        assert [row[1] for row in db.execute("PRAGMA table_info(meter)")] == ["row_key", "voltage"]
        assert embedded_columns(db, "meter") == [("row_key", "str", 1), ("voltage", "double", 0)]


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
        assert client.get("/api/health").json()["result_databases"] == 1
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


def test_web_api_batch_sample_enabled_definitions(tmp_path):
    source_db = tmp_path / "source.db"
    with sqlite3.connect(source_db) as conn:
        conn.execute("CREATE TABLE meter (id INTEGER PRIMARY KEY, voltage REAL)")
        conn.execute("INSERT INTO meter VALUES (1, 220.5)")
    app = create_app(tmp_path / "samp_define.db", source_db, tmp_path / "results")
    with TestClient(app) as client:
        enabled = client.post("/api/definitions", json={"name": "enabled_point", "source_database": str(source_db), "source_table": "meter", "fields": ["voltage"], "interval_seconds": 0, "enabled": True}).json()
        client.post("/api/definitions", json={"name": "disabled_point", "source_database": str(source_db), "source_table": "meter", "fields": ["voltage"], "interval_seconds": 0, "enabled": False})
        result = client.post("/api/sample/all")
        assert result.status_code == 200
        body = result.json()
        assert body["requested"] == 1
        assert [item["samp_id"] for item in body["completed"]] == [enabled["id"]]
        assert body["failed"] == []


def test_web_pages_are_separated(tmp_path):
    app = create_app(tmp_path / "samp_define.db", tmp_path / "source.db", tmp_path / "results")
    with TestClient(app) as client:
        root = client.get("/", follow_redirects=False)
        assert root.status_code == 307
        assert root.headers["location"] == "/definitions"
        assert client.get("/storage", follow_redirects=False).headers["location"] == "/runtime#storage"
        assert "采样定义" in client.get("/definitions").text
        assert "运行监控" in client.get("/runtime").text
        assert "采样查询" in client.get("/query").text
        for asset in (
            "/static/samp.css",
            "/static/i18n.js",
            "/static/common.js",
            "/static/chart.js",
            "/static/definitions.js",
            "/static/runtime.js",
            "/static/query.js",
        ):
            assert client.get(asset).status_code == 200


def test_source_column_types_and_extended_filters_are_preserved(tmp_path):
    source_db = tmp_path / "source.db"
    with sqlite3.connect(source_db) as conn:
        conn.execute(
            "CREATE TABLE device (id INTEGER PRIMARY KEY, voltage REAL, status REAL, code INTEGER, label TEXT)"
        )
        conn.executemany(
            "INSERT INTO device VALUES (?,?,?,?,?)",
            [
                (1, 220.5, 1.0, 10, "ok"),
                (2, 185.0, 1.0, 20, "low"),
                (3, 230.0, 0.0, 30, "off"),
            ],
        )
    store = Store(tmp_path / "samp_define.db", source_db, tmp_path / "results")
    # 过滤条件测试 REAL 值 1.0 与整数写法 status=1 的兼容性，以及 >= 和 IN
    preview = store.inspect_source(source_db, "device", filter_expr="status=1 and voltage>=200 and code in (10, 20)")
    assert preview["total_rows"] == 3
    assert preview["matched_rows"] == 1
    assert preview["field_types"]["code"] == "INTEGER"
    assert preview["field_types"]["label"] == "TEXT"

    definition = store.upsert_definition(
        {
            "name": "typed_dev",
            "source_database": str(source_db),
            "source_table": "device",
            "fields": ["voltage", "code", "label"],
            "filter": "status=1 and voltage>=200 and label != 'off'",
            "interval_seconds": 0,
        }
    )
    sampled = store.run_sample(definition["id"], calendar.timegm((2026, 5, 1, 0, 0, 0)))
    assert sampled["rows_written"] == 1
    with closing(sqlite3.connect(sampled["database"])) as db:
        assert embedded_columns(db, sampled["table"]) == [
            ("sample_time", "int", 1),
            ("row_key", "str", 1),
            ("voltage", "double", 0),
            ("code", "int", 0),
            ("label", "str", 0),
        ]


def test_query_keys_series_desc_order_and_storage_cleanup(tmp_path):
    source_db = tmp_path / "source.db"
    result_dir = tmp_path / "results"
    store = Store(tmp_path / "samp_define.db", source_db, result_dir)
    definition = store.upsert_definition(
        {"name": "bus_a", "source_database": str(source_db), "source_table": "meter", "fields": ["voltage"], "interval_seconds": 0}
    )
    base = calendar.timegm((2026, 6, 1, 0, 0, 0))
    for i in range(4):
        store.replace_source_rows(
            "meter",
            [
                {"row_key": "K1", "values": {"voltage": 220.0 + i}},
                {"row_key": "K2", "values": {"voltage": 210.0 + i * 2}},
            ],
            source_db,
        )
        store.run_sample(definition["id"], base + i * 60)

    # 非结果库命名的备份文件不应计入 result_databases
    (result_dir / "samp_define_bak.db").touch()
    assert store.summary()["result_databases"] == 1

    keys = store.query_keys(definition["id"], base, base + 300)
    assert keys["keys"] == ["K1", "K2"]

    desc_res = store.query_samples(definition["id"], base, base + 300, ["voltage"], limit=2, offset=0, row_keys=["K1"], order="desc")
    assert desc_res["total_rows"] == 4
    assert [row["voltage"] for row in desc_res["rows"]] == [223.0, 222.0]

    series = store.query_series(definition["id"], base, base + 300, ["voltage"], row_keys=["K1"], bucket_seconds=120, agg="avg")
    assert len(series["series"]) == 1
    assert series["series"][0]["row_key"] == "K1"
    assert series["series"][0]["stats"]["voltage"]["max"] >= 222.0

    # 删除定义后，结果库应识别为孤儿库并可安全删除
    store.delete_definition(definition["id"])
    dbs = store.list_result_databases()
    assert len(dbs) == 1
    assert dbs[0]["is_orphan"] is True
    assert dbs[0]["row_count"] == 8
    assert store.delete_result_database(dbs[0]["filename"]) is True
    assert store.list_result_databases() == []
    with pytest.raises(ValueError):
        store.delete_result_database("../samp_define.db")


def test_web_api_new_endpoints_series_export_batch_and_storage(tmp_path):
    source_db = tmp_path / "source.db"
    result_dir = tmp_path / "results"
    with sqlite3.connect(source_db) as conn:
        conn.execute("CREATE TABLE meter (id INTEGER PRIMARY KEY, voltage REAL, status INTEGER)")
        conn.executemany("INSERT INTO meter VALUES (?,?,?)", [(1, 220.5, 1), (2, 225.0, 1)])
    app = create_app(tmp_path / "samp_define.db", source_db, result_dir, enable_scheduler=False)
    with TestClient(app) as client:
        cfg = client.get("/api/config")
        assert cfg.status_code == 200
        assert cfg.json()["scheduler_enabled"] is False
        assert client.get("/api/source/databases").status_code == 200

        d1 = client.post(
            "/api/definitions",
            json={"name": "p1", "source_database": str(source_db), "source_table": "meter", "fields": ["voltage"], "interval_seconds": 60, "enabled": True},
        ).json()
        d2 = client.post(
            "/api/definitions",
            json={"name": "p2", "source_database": str(source_db), "source_table": "meter", "fields": ["voltage"], "interval_seconds": 0, "enabled": False},
        ).json()

        batch = client.post("/api/definitions/batch-enable", json={"ids": [d1["id"], d2["id"]], "enabled": True})
        assert batch.status_code == 200 and batch.json()["updated"] == 2

        sampled = client.post(f"/api/sample/{d1['id']}").json()
        ts = sampled["sampled_at"]

        assert client.get(f"/api/query/{d1['id']}/keys", params={"start": ts - 1, "end": ts + 1}).json()["keys"] == ["1", "2"]
        series_resp = client.get(f"/api/query/{d1['id']}/series", params={"start": ts - 1, "end": ts + 1, "fields": "voltage", "agg": "raw"})
        assert series_resp.status_code == 200 and len(series_resp.json()["series"]) == 2

        csv_resp = client.get(f"/api/query/{d1['id']}/export", params={"start": ts - 1, "end": ts + 1, "fields": "voltage"})
        assert csv_resp.status_code == 200
        assert "sample_time,row_key,voltage" in csv_resp.text

        status_resp = client.get("/api/status").json()
        p1_item = next(item for item in status_resp["items"] if item["id"] == d1["id"])
        assert p1_item["last_status"] == "ok"
        assert p1_item["last_rows_written"] == 2
        assert len(client.get("/api/runtime/logs").json()["logs"]) >= 1

        storage = client.get("/api/storage/databases").json()
        assert storage["total"] == 1
        del_resp = client.delete(f"/api/storage/databases/{storage['items'][0]['filename']}")
        assert del_resp.status_code == 200 and del_resp.json()["ok"] is True


def test_series_last_in_sql_raw_truncation_and_meta_cache(tmp_path):
    source_db = tmp_path / "source.db"
    result_dir = tmp_path / "results"
    store = Store(tmp_path / "samp_define.db", source_db, result_dir)
    definition = store.upsert_definition(
        {"name": "series_dev", "source_database": str(source_db), "source_table": "meter", "fields": ["voltage"], "interval_seconds": 0}
    )
    base = calendar.timegm((2026, 7, 1, 0, 0, 0))
    for i in range(120):
        store.replace_source_rows("meter", [{"row_key": "A", "values": {"voltage": 200.0 + i}}], source_db)
        store.run_sample(definition["id"], base + i * 10)

    # 库内 GROUP BY last 模式应准确拿到桶内最后一条记录
    last_res = store.query_series(definition["id"], base, base + 1200, ["voltage"], bucket_seconds=600, agg="last")
    assert last_res["truncated"] is False
    assert len(last_res["series"]) == 1
    assert last_res["series"][0]["values"]["voltage"][-1] == 319.0

    # raw 模式触发 max_raw_points 保护并标记 truncated=True
    raw_res = store.query_series(definition["id"], base, base + 1200, ["voltage"], agg="raw", max_raw_points=100)
    assert raw_res["truncated"] is True
    assert len(raw_res["series"][0]["times"]) == 100

    # 元数据缓存命中验证
    first_meta = store.runtime_status(definition["id"])
    assert first_meta["total_rows"] == 120
    assert len(store._result_meta_cache) == 1
    second_meta = store.runtime_status(definition["id"])
    assert second_meta["total_rows"] == 120


def test_rename_migrates_result_dbs_and_json_import_export(tmp_path):
    source_db = tmp_path / "source.db"
    result_dir = tmp_path / "results"
    store = Store(tmp_path / "samp_define.db", source_db, result_dir)
    store.replace_source_rows("meter", [{"row_key": "R1", "values": {"voltage": 221.5}}], source_db)
    d = store.upsert_definition(
        {"name": "old_meter", "source_database": str(source_db), "source_table": "meter", "fields": ["voltage"], "interval_seconds": 60}
    )
    ts = calendar.timegm((2026, 8, 1, 0, 0, 0))
    store.run_sample(d["id"], ts)

    updated = store.upsert_definition(
        {"name": "new_meter", "source_database": str(source_db), "source_table": "meter", "fields": ["voltage"], "interval_seconds": 60},
        d["id"],
        rename_results=True,
    )
    assert updated["migrated_results"] == ["samp_new_meter_2026.db"]
    assert not (result_dir / "samp_old_meter_2026.db").exists()
    assert (result_dir / "samp_new_meter_2026.db").exists()
    # 迁移后可直接查到旧数据且 RTDB 表名同步更新
    q = store.query_samples(d["id"], ts - 1, ts + 1)
    assert len(q["rows"]) == 1 and q["rows"][0]["voltage"] == 221.5
    with closing(sqlite3.connect(result_dir / "samp_new_meter_2026.db")) as db:
        assert embedded_columns(db, "samp_new_meter_2026")[-1] == ("voltage", "double", 0)

    exported = store.export_definitions()
    assert exported["count"] == 1
    # skip 模式跳过同名
    res_skip = store.import_definitions(exported["items"], mode="skip")
    assert res_skip == {"created": 0, "updated": 0, "skipped": 1, "errors": []}
    # overwrite 模式更新同名并新增新点
    items = [
        {**exported["items"][0], "interval_seconds": 120},
        {**exported["items"][0], "name": "extra_meter", "interval_seconds": 30},
    ]
    res_ow = store.import_definitions(items, mode="overwrite")
    assert res_ow == {"created": 1, "updated": 1, "skipped": 0, "errors": []}


def test_cross_process_runtime_store_and_scheduler_lease(tmp_path):
    from samp.scheduler import SamplingScheduler

    source_db = tmp_path / "source.db"
    def_db = tmp_path / "samp_define.db"
    res_dir = tmp_path / "results"
    store = Store(def_db, source_db, res_dir)
    store.replace_source_rows("meter", [{"row_key": "A", "values": {"voltage": 220.0}}], source_db)
    d = store.upsert_definition(
        {"name": "lease_pt", "source_database": str(source_db), "source_table": "meter", "fields": ["voltage"], "interval_seconds": 60}
    )

    # 模拟独立进程执行采样并持有调度租约
    standalone = SamplingScheduler(store, mode="standalone")
    standalone._pid = 10001
    assert standalone.tick(force_once=True) is True

    # 另一进程尝试抢占应被拦截，防止重复采样
    rival = SamplingScheduler(store, mode="web")
    rival._pid = 20002
    assert rival.tick(force_once=False) is False

    # Web 禁用内嵌调度器时仍可通过持久化 RuntimeStore 看到独立进程的状态和日志
    app = create_app(def_db, source_db, res_dir, enable_scheduler=False)
    with TestClient(app) as client:
        st = client.get("/api/status").json()
        assert st["scheduler"]["mode"] == "standalone"
        assert st["scheduler"]["sampled"] >= 1
        item = next(i for i in st["items"] if i["id"] == d["id"])
        assert item["last_status"] == "ok"
        assert len(client.get("/api/runtime/logs").json()["logs"]) >= 1

        # 同时验证定义导出/导入 API
        exp = client.get("/api/definitions/export").json()
        imp = client.post("/api/definitions/import", json={"items": exp["items"], "mode": "skip"}).json()
        assert imp["skipped"] == 1


def test_source_tables_summary_rtdb_labels_and_sample_values(tmp_path):
    source_db = tmp_path / "source.db"
    with sqlite3.connect(source_db) as conn:
        conn.execute("CREATE TABLE bus (id INTEGER PRIMARY KEY, ua REAL, remark TEXT, status INTEGER)")
        conn.executemany(
            "INSERT INTO bus VALUES (?,?,?,?)",
            [(1, 220.5, "北站", 1), (2, 221.0, "南站", 0), (3, 219.8, "北站", 1)],
        )
        conn.execute("CREATE TABLE rtdb_schema_tables (table_name TEXT PRIMARY KEY, chn_name TEXT)")
        conn.execute("INSERT INTO rtdb_schema_tables VALUES ('bus', '母线表')")
        conn.execute("CREATE TABLE rtdb_schema_columns (table_name TEXT, eng_name TEXT, chn_name TEXT)")
        conn.executemany(
            "INSERT INTO rtdb_schema_columns VALUES (?,?,?)",
            [("bus", "ua", "A相电压"), ("bus", "status", "运行状态")],
        )

    app = create_app(tmp_path / "samp_define.db", source_db, tmp_path / "results", enable_scheduler=False)
    with TestClient(app) as client:
        summary = client.get("/api/source/tables/summary", params={"source_database": str(source_db)})
        assert summary.status_code == 200
        tables = summary.json()["tables"]
        assert len(tables) == 1
        bus = tables[0]
        assert bus["table"] == "bus"
        assert bus["chn_name"] == "母线表"
        assert bus["row_count"] == 3
        assert bus["numeric_fields"] == ["id", "ua", "status"]
        assert bus["field_labels"] == {"ua": "A相电压", "status": "运行状态"}
        assert bus["existing_definitions"] == []

        inspect = client.get("/api/source/inspect", params={"source_database": str(source_db), "source_table": "bus"}).json()
        assert inspect["field_labels"]["ua"] == "A相电压"
        assert set(inspect["sample_values"]["remark"]) == {"北站", "南站"}
        assert set(inspect["sample_values"]["status"]) == {0, 1}


def test_root_dir_and_relative_paths_across_store_and_api(tmp_path):
    root = tmp_path / "workspace"
    sub_dir = root / "realtime"
    sub_dir.mkdir(parents=True)
    src_db = sub_dir / "meter.db"
    with sqlite3.connect(src_db) as conn:
        conn.execute("CREATE TABLE feeder (id INTEGER PRIMARY KEY, power REAL)")
        conn.execute("INSERT INTO feeder VALUES (1, 128.5)")

    app = create_app(
        definition_db="samp/samp_define.db",
        source_db="realtime/meter.db",
        result_dir="samp",
        enable_scheduler=False,
        root_dir=root,
    )
    with TestClient(app) as client:
        cfg = client.get("/api/config").json()
        assert cfg["root_dir"] == root.resolve().as_posix()
        assert cfg["default_source_db"] == "realtime/meter.db"
        assert cfg["sqlite_dir"] == "samp"
        assert "realtime/meter.db" in cfg["source_databases"]

        # 即使提交绝对路径，入库后也自动规范化为相对路径
        created = client.post(
            "/api/definitions",
            json={
                "name": "rel_feeder",
                "source_database": str(src_db),
                "source_table": "feeder",
                "fields": ["power"],
                "interval_seconds": 0,
            },
        ).json()
        assert created["source_database"] == "realtime/meter.db"

        sampled = client.post(f"/api/sample/{created['id']}").json()
        assert sampled["rows_written"] == 1
        assert sampled["relative_path"].startswith("samp/samp_rel_feeder_")

        # 使用相对路径可直接预览与查询
        insp = client.get(
            "/api/source/inspect",
            params={"source_database": "realtime/meter.db", "source_table": "feeder"},
        ).json()
        assert insp["source_database"] == "realtime/meter.db"
        assert insp["total_rows"] == 1


def test_custom_target_dir_template_single_db_migration_and_delete(tmp_path):
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    src_db = root / "src" / "grid.db"
    with sqlite3.connect(src_db) as conn:
        conn.execute("CREATE TABLE line (id INTEGER PRIMARY KEY, p REAL, q REAL)")
        conn.execute("INSERT INTO line VALUES (1, 101.5, 22.3)")

    app = create_app(
        definition_db="samp/samp_define.db",
        source_db="src/grid.db",
        result_dir="samp",
        enable_scheduler=False,
        root_dir=root,
    )
    store = app.state.store
    t_2025 = calendar.timegm((2025, 12, 31, 23, 59, 0, 0, 0, 0))
    t_2026 = calendar.timegm((2026, 1, 1, 0, 1, 0, 0, 0, 0))

    with TestClient(app) as client:
        # 1. 自定义目录 + 前缀（按年分库）
        d1 = client.post(
            "/api/definitions",
            json={
                "name": "line_a",
                "source_database": "src/grid.db",
                "source_table": "line",
                "target_dir": "archive/grid",
                "target_name": "grid_a",
                "fields": ["p"],
                "interval_seconds": 0,
            },
        )
        assert d1.status_code == 200, d1.text
        d1_data = d1.json()
        assert d1_data["target_dir"] == "archive/grid"
        assert d1_data["target_name"] == "grid_a"
        assert d1_data["target_preview"] == "archive/grid/grid_a_{year}.db"

        res1 = store.run_sample(d1_data["id"], t_2026)
        assert res1["relative_path"] == "archive/grid/grid_a_2026.db"
        assert (root / "archive/grid/grid_a_2026.db").is_file()

        # 2. 自定义含 {year} 模板
        d2 = client.post(
            "/api/definitions",
            json={
                "name": "line_b",
                "source_database": "src/grid.db",
                "source_table": "line",
                "target_dir": "archive/tpl",
                "target_name": "plant_{year}_his.db",
                "fields": ["p"],
                "interval_seconds": 0,
            },
        )
        assert d2.status_code == 200, d2.text
        res2 = store.run_sample(d2.json()["id"], t_2026)
        assert res2["relative_path"] == "archive/tpl/plant_2026_his.db"

        # 3. 单库模式（不分年，跨年写入同一文件且查询无重复）
        d3 = client.post(
            "/api/definitions",
            json={
                "name": "line_single",
                "source_database": "src/grid.db",
                "source_table": "line",
                "target_dir": "archive/single",
                "target_name": "station_all.db",
                "fields": ["p", "q"],
                "interval_seconds": 0,
            },
        )
        assert d3.status_code == 200, d3.text
        d3_id = d3.json()["id"]
        assert store.run_sample(d3_id, t_2025)["relative_path"] == "archive/single/station_all.db"
        assert store.run_sample(d3_id, t_2026)["relative_path"] == "archive/single/station_all.db"
        q3 = client.get(f"/api/query/{d3_id}", params={"start": t_2025 - 60, "end": t_2026 + 60}).json()
        assert q3["total_rows"] == 2
        assert q3["tables"] == ["station_all"]

        # 4. 目标库冲突检测
        dup = client.post(
            "/api/definitions",
            json={
                "name": "line_conflict",
                "source_database": "src/grid.db",
                "source_table": "line",
                "target_dir": "archive/single",
                "target_name": "station_all.db",
                "fields": ["p"],
                "interval_seconds": 0,
            },
        )
        assert dup.status_code == 400

        # 5. 修改 target_dir / target_name 并同步迁移已有结果库与 RTDB 登记
        upd = client.put(
            f"/api/definitions/{d1_data['id']}",
            json={
                "name": "line_a",
                "source_database": "src/grid.db",
                "source_table": "line",
                "target_dir": "archive/grid_v2",
                "target_name": "grid_new",
                "fields": ["p"],
                "interval_seconds": 0,
                "rename_results": True,
            },
        )
        assert upd.status_code == 200, upd.text
        assert upd.json()["migrated_results"] == ["grid_new_2026.db"]
        new_db = root / "archive/grid_v2/grid_new_2026.db"
        assert new_db.is_file()
        assert not (root / "archive/grid/grid_a_2026.db").exists()
        with closing(sqlite3.connect(new_db)) as conn:
            tables = [r[0] for r in conn.execute("SELECT table_name FROM rtdb_schema_tables").fetchall()]
            assert tables == ["grid_new_2026"]

        # 6. 结果库列表包含自定义子目录相对路径，且支持按相对路径安全删除
        dbs = client.get("/api/storage/databases").json()
        paths = {it["path"]: it for it in dbs["items"]}
        assert "archive/grid_v2/grid_new_2026.db" in paths
        assert "archive/single/station_all.db" in paths
        assert paths["archive/single/station_all.db"]["year"] is None

        del_res = client.delete("/api/storage/databases/archive/grid_v2/grid_new_2026.db")
        assert del_res.status_code == 200
        assert not new_db.exists()


def test_multi_item_single_db_multi_table_crud_and_query(tmp_path):
    root = tmp_path / "workspace"
    (root / "rt").mkdir(parents=True)
    src1 = root / "rt" / "substation.db"
    src2 = root / "rt" / "env.db"
    with sqlite3.connect(src1) as conn:
        conn.execute("CREATE TABLE meter (id INTEGER PRIMARY KEY, ua REAL, ia REAL, status INTEGER)")
        conn.execute("INSERT INTO meter VALUES (1, 220.5, 12.1, 1), (2, 219.8, 11.4, 0)")
        conn.execute("CREATE TABLE inverter (dev TEXT PRIMARY KEY, kw REAL, temp REAL, flag INTEGER)")
        conn.execute("INSERT INTO inverter VALUES ('inv1', 88.2, 42.5, 1), ('inv2', 75.0, 39.0, 0)")
    with sqlite3.connect(src2) as conn:
        conn.execute("CREATE TABLE weather (site TEXT PRIMARY KEY, humidity REAL)")
        conn.execute("INSERT INTO weather VALUES ('north', 55.2)")

    app = create_app(
        definition_db="samp/samp_define.db",
        source_db="rt/substation.db",
        result_dir="samp",
        enable_scheduler=False,
        root_dir=root,
    )
    store = app.state.store
    ts = calendar.timegm((2026, 6, 15, 10, 0, 0, 0, 0, 0))

    with TestClient(app) as client:
        # 1. 创建同一采样点同时采集 meter(默认主表) + inverter(独立表)
        created = client.post(
            "/api/definitions",
            json={
                "name": "station_a",
                "interval_seconds": 0,
                "items": [
                    {
                        "source_database": "rt/substation.db",
                        "source_table": "meter",
                        "target_table": "",
                        "fields": ["ua", "ia"],
                        "filter": "status=1",
                    },
                    {
                        "source_database": "rt/substation.db",
                        "source_table": "inverter",
                        "target_table": "inverter",
                        "fields": ["kw"],
                        "filter": "",
                    },
                ],
            },
        )
        assert created.status_code == 200, created.text
        d = created.json()
        samp_id = d["id"]
        assert d["item_count"] == 2
        assert [it["item_id"] for it in d["items"]] == ["i1", "i2"]

        # 2. 一次采样写入同一 .db 的两张表，并全部同步 RTDB 结构表
        res = store.run_sample(samp_id, ts)
        assert res["relative_path"] == "samp/samp_station_a_2026.db"
        assert res["rows_written"] == 3  # meter 命中 1 行 + inverter 2 行
        assert res["tables"] == ["samp_station_a_2026", "inverter"]
        db_file = root / "samp/samp_station_a_2026.db"
        with closing(sqlite3.connect(db_file)) as conn:
            reg_tables = {r[0] for r in conn.execute("SELECT table_name FROM rtdb_schema_tables").fetchall()}
            assert reg_tables == {"samp_station_a_2026", "inverter"}

        # 3. 针对已定义采样点追加第 3 条采集项（跨另一源库 rt/env.db）
        added = client.post(
            f"/api/definitions/{samp_id}/items",
            json={
                "source_database": "rt/env.db",
                "source_table": "weather",
                "target_table": "env_log",
                "fields": ["humidity"],
                "filter": "",
            },
        )
        assert added.status_code == 200, added.text
        assert added.json()["item_count"] == 3
        third_id = added.json()["items"][2]["item_id"]

        # 4. 修改第 2 条采集项：增加 temp 字段并加过滤 flag=1
        updated = client.put(
            f"/api/definitions/{samp_id}/items/i2",
            json={
                "source_database": "rt/substation.db",
                "source_table": "inverter",
                "target_table": "inverter",
                "fields": ["kw", "temp"],
                "filter": "flag=1",
            },
        )
        assert updated.status_code == 200, updated.text
        res2 = store.run_sample(samp_id, ts + 60)
        assert res2["tables"] == ["samp_station_a_2026", "inverter", "env_log"]
        assert res2["rows_written"] == 3  # meter 1 + inverter(flag=1) 1 + weather 1

        # 5. 按 item_id 独立查询表、曲线、row_key 与 CSV 导出
        q_i1 = client.get(f"/api/query/{samp_id}", params={"start": ts - 10, "end": ts + 120, "item_id": "i1"}).json()
        assert q_i1["columns"] == ["sample_time", "row_key", "ua", "ia"]
        assert q_i1["total_rows"] == 2

        q_i2 = client.get(f"/api/query/{samp_id}", params={"start": ts - 10, "end": ts + 120, "item_id": "i2"}).json()
        assert q_i2["columns"] == ["sample_time", "row_key", "kw", "temp"]
        assert q_i2["total_rows"] == 3  # ts 时 2 行 + ts+60 时 1 行
        assert any(r["temp"] == 42.5 for r in q_i2["rows"])

        s_i3 = client.get(
            f"/api/query/{samp_id}/series",
            params={"start": ts - 10, "end": ts + 120, "item_id": third_id},
        ).json()
        assert s_i3["fields"] == ["humidity"]
        assert s_i3["row_keys"] == ["north"]

        csv_i3 = client.get(
            f"/api/query/{samp_id}/export",
            params={"start": ts - 10, "end": ts + 120, "item_id": third_id},
        )
        assert csv_i3.status_code == 200
        assert "humidity" in csv_i3.text and "55.2" in csv_i3.text

        # 结果库元数据汇总全部 3 张业务表
        storage = client.get("/api/storage/databases").json()
        entry = next(it for it in storage["items"] if it["filename"] == "samp_station_a_2026.db")
        assert entry["table_count"] == 3
        assert entry["row_count"] == 6  # 2 + 3 + 1

        # 6. 逐条删除采集项，删至最后 1 条时拦截返回 400
        del3 = client.delete(f"/api/definitions/{samp_id}/items/{third_id}")
        assert del3.status_code == 200
        assert del3.json()["item_count"] == 2

        del2 = client.delete(f"/api/definitions/{samp_id}/items/i2")
        assert del2.status_code == 200
        assert del2.json()["item_count"] == 1

        del1 = client.delete(f"/api/definitions/{samp_id}/items/i1")
        assert del1.status_code == 400


def test_multilevel_directory_scan_for_target_and_source_dropdowns(tmp_path):
    root = tmp_path / "workspace"
    deep_src = root / "rt" / "area1" / "station2"
    deep_src.mkdir(parents=True)
    (root / "archive" / "2026" / "grid").mkdir(parents=True)
    (root / ".hidden_dir").mkdir(parents=True)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    db_file = deep_src / "scada.db"
    with closing(sqlite3.connect(db_file)) as conn:
        conn.execute("CREATE TABLE bus (id INTEGER PRIMARY KEY, kv REAL)")

    app = create_app(
        definition_db="samp/samp_define.db",
        source_db="source.db",
        result_dir="samp",
        enable_scheduler=False,
        root_dir=root,
    )
    with TestClient(app) as client:
        cfg = client.get("/api/config").json()
        dirs = cfg["directories"]
        assert "." in dirs
        assert "rt" in dirs
        assert "rt/area1" in dirs
        assert "rt/area1/station2" in dirs
        assert "archive/2026/grid" in dirs
        assert ".hidden_dir" not in dirs
        assert "node_modules" not in dirs
        # 磁盘上真实存在的深层 .db 排在不存在的默认 source.db 之前
        assert cfg["source_databases"][0] == "rt/area1/station2/scada.db"
        assert "archive/2026/grid" in cfg["target_dirs"]
        assert "rt/area1/station2" in cfg["target_dirs"]







