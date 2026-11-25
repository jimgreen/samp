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



