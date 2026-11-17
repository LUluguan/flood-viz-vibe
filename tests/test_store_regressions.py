# -*- coding: utf-8 -*-
"""Pure store regressions: temporary files only; never import app or load .env."""
import hashlib
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

import store


@pytest.fixture(autouse=True)
def _isolate_stores(tmp_path, monkeypatch):
    # Override conftest's fixture before it imports app (and thus the real .env).
    root = tmp_path / "stores"
    root.mkdir()
    monkeypatch.setattr(store, "_REPORT_DIR", str(root))
    monkeypatch.setattr(store, "_REPORTS_FILE", str(root / "reports.json"))
    monkeypatch.setattr(store, "_SUBSCRIBERS_FILE", str(root / "subscribers.json"))
    monkeypatch.setattr(store, "_AUTH_FILE", str(root / "web_users.json"))
    for name in ("_reports_degraded_from", "_subscribers_degraded_from", "_users_degraded_from"):
        monkeypatch.setattr(store, name, None, raising=False)
    monkeypatch.setattr(store, "_state_quarantines", [0])
    monkeypatch.setattr(store, "_report_rl", {})
    monkeypatch.setattr(store, "_rate_gc_next", 0.0, raising=False)
    monkeypatch.delenv("FLOOD_DEMO", raising=False)
    monkeypatch.delenv("FLOOD_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("FLOOD_DATA_DIR", str(root))


def _legacy(username="legacy", password="old-password", salt="old-salt"):
    return {"username": username, "salt": salt,
            "hash": hashlib.sha256((salt + password).encode("utf-8")).hexdigest(),
            "role": "public"}


def _write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _assert_unavailable(call):
    with pytest.raises(RuntimeError) as exc:
        call()
    assert isinstance(exc.value, store.StoreUnavailable)
    return exc.value


@pytest.mark.parametrize("flag", [None, "0", "true", "yes", " 1 "])
def test_safe_mode_does_not_seed_public_passwords(monkeypatch, flag):
    if flag is not None:
        monkeypatch.setenv("FLOOD_DEMO", flag)
    _assert_unavailable(store._load_users)
    assert not Path(store._AUTH_FILE).exists()


def test_demo_flag_is_read_on_every_call(monkeypatch):
    assert store.demo_enabled() is False
    monkeypatch.setenv("FLOOD_DEMO", "1")
    assert store.demo_enabled() is True
    monkeypatch.delenv("FLOOD_DEMO")
    assert store.demo_enabled() is False


def test_explicit_demo_seeds_only_demo_accounts(monkeypatch):
    monkeypatch.setenv("FLOOD_DEMO", "1")
    users = store._load_users()
    assert [(u["username"], u["role"]) for u in users] == [("admin", "admin"), ("public", "public")]
    assert store._verify_pw("admin123", users[0])
    assert store._verify_pw("123456", users[1])
    assert not store._verify_pw("wrong", users[0])


@pytest.mark.parametrize("password", ["", "admin123", "123456", "short", "x" * 11, "admin123    "])
def test_safe_bootstrap_rejects_missing_short_or_padded_default_password(monkeypatch, password):
    monkeypatch.setenv("FLOOD_ADMIN_PASSWORD", password)
    _assert_unavailable(store._load_users)
    assert not Path(store._AUTH_FILE).exists()


def test_strong_bootstrap_stores_only_hash_and_does_not_log_password(monkeypatch, caplog):
    password = "fixture-admin-secret-2026"
    monkeypatch.setenv("FLOOD_ADMIN_PASSWORD", password)
    users = store._load_users()
    assert len(users) == 1
    assert users[0]["username"] == "admin" and users[0]["role"] == "admin"
    assert store._verify_pw(password, users[0])
    assert not store._verify_pw("admin123", users[0])
    assert password not in Path(store._AUTH_FILE).read_text(encoding="utf-8")
    assert password not in caplog.text


@pytest.mark.parametrize("demo", ["0", "1"])
def test_existing_users_are_never_reset_or_reseeded(monkeypatch, demo):
    monkeypatch.setenv("FLOOD_DEMO", demo)
    monkeypatch.setenv("FLOOD_ADMIN_PASSWORD", "different-bootstrap-password")
    users = [_legacy()]
    users[0]["extra"] = {"retained": True}
    _write(store._AUTH_FILE, users)
    before = Path(store._AUTH_FILE).read_bytes()
    assert store._load_users() == users
    assert Path(store._AUTH_FILE).read_bytes() == before
    _write(store._AUTH_FILE, [])
    assert store._load_users() == []


@pytest.mark.parametrize("override", [True, False])
def test_data_dir_controls_all_paths_without_initializing_files(tmp_path, monkeypatch, override):
    root = tmp_path / "named-volume"
    if override:
        monkeypatch.setenv("FLOOD_DATA_DIR", str(root))
    else:
        monkeypatch.delenv("FLOOD_DATA_DIR")
        root = Path(store.__file__).parent
    spec = importlib.util.spec_from_file_location("isolated_store", store.__file__)
    isolated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolated)
    assert Path(isolated._REPORT_DIR) == root / "reports"
    assert Path(isolated._REPORTS_FILE) == root / "reports" / "reports.json"
    assert Path(isolated._SUBSCRIBERS_FILE) == root / "reports" / "subscribers.json"
    assert Path(isolated._AUTH_FILE) == root / "web_users.json"
    if override:
        assert not root.exists()


@pytest.mark.parametrize("bad", ["{broken", "{}", '["x"]', '[{}]', '[{"id":"r1","status":1}]'])
def test_reports_corruption_is_preserved_and_repeated_writes_are_blocked(bad):
    path = Path(store._REPORTS_FILE)
    path.write_text(bad, encoding="utf-8")
    assert store._load_reports() == []
    quarantine = Path(store._reports_degraded_from)
    assert quarantine.read_text(encoding="utf-8") == bad
    for _ in range(2):
        _assert_unavailable(store._load_reports)  # 不重复解析坏现场；仅保存时走恢复合并
        _assert_unavailable(lambda: store._save_reports([{"id": "new", "status": "待核实"}]))
        assert not path.exists()
        assert quarantine.read_text(encoding="utf-8") == bad


@pytest.mark.parametrize("kind", ["users", "subscribers"])
@pytest.mark.parametrize("bad", ["{broken", "null", "{}", '["x"]', '[{}]'])
def test_user_and_subscriber_corruption_cannot_be_silently_overwritten(kind, bad):
    path = Path(store._AUTH_FILE if kind == "users" else store._SUBSCRIBERS_FILE)
    path.write_text(bad, encoding="utf-8")
    load = store._load_users if kind == "users" else store._load_subscribers
    save = (lambda: store._upgrade_pw([_legacy()], 0, "old-password")) if kind == "users" else (
        lambda: store._save_subscribers([{"email": "new@example.com", "zone": "全部"}]))
    _assert_unavailable(load)
    _assert_unavailable(save)
    assert path.read_text(encoding="utf-8") == bad
    # Losing the damaged file must not erase the in-process degraded guard.
    path.unlink()
    _assert_unavailable(load)
    _assert_unavailable(save)
    assert not path.exists()


@pytest.mark.parametrize("field,value", [("username", ""), ("salt", None), ("hash", "not-a-hash"),
                                          ("role", "superuser"), ("role", [])])
def test_invalid_user_fields_fail_closed(field, value):
    user = _legacy()
    user[field] = value
    _write(store._AUTH_FILE, [user])
    before = Path(store._AUTH_FILE).read_bytes()
    _assert_unavailable(store._load_users)
    assert Path(store._AUTH_FILE).read_bytes() == before


@pytest.mark.parametrize("row", [{"email": 3, "zone": "全部"}, {"email": "a@b.test", "zone": []}])
def test_invalid_subscriber_fields_fail_closed(row):
    _write(store._SUBSCRIBERS_FILE, [row])
    _assert_unavailable(store._load_subscribers)


@pytest.mark.parametrize("kind", ["reports", "subscribers", "users"])
def test_direct_writes_or_upgrades_also_detect_bad_existing_table(kind):
    if kind == "reports":
        path = Path(store._REPORTS_FILE)
        write = lambda: store._save_reports([{"id": "new", "status": "待核实"}])
    elif kind == "subscribers":
        path = Path(store._SUBSCRIBERS_FILE)
        write = lambda: store._save_subscribers([{"email": "a@b.test", "zone": "全部"}])
    else:
        path = Path(store._AUTH_FILE)
        write = lambda: store._upgrade_pw([_legacy()], 0, "old-password")
    path.write_text('["x"]', encoding="utf-8")
    _assert_unavailable(write)
    preserved = Path(store._reports_degraded_from) if kind == "reports" else path
    assert preserved.read_text(encoding="utf-8") == '["x"]'


@pytest.mark.parametrize("kind", ["reports", "subscribers"])
@pytest.mark.parametrize("bad", [["x"], [{}], {}])
def test_invalid_outgoing_tables_do_not_replace_valid_history(kind, bad):
    path, save = ((store._REPORTS_FILE, store._save_reports) if kind == "reports" else
                  (store._SUBSCRIBERS_FILE, store._save_subscribers))
    valid = ([{"id": "old", "status": "待核实"}] if kind == "reports" else
             [{"email": "a@b.test", "zone": "全部"}])
    _write(path, valid)
    before = Path(path).read_bytes()
    _assert_unavailable(lambda: save(bad))
    assert Path(path).read_bytes() == before


def test_valid_extra_fields_are_preserved():
    reports = [{"id": "r1", "status": "historical-status", "other": [1, 2]}]
    subscribers = [{"email": "a@b.test", "zone": "全部", "created_at": "old", "extra": 1}]
    store._save_reports(reports)
    store._save_subscribers(subscribers)
    assert store._load_reports() == reports
    assert store._load_subscribers() == subscribers


def test_failed_report_quarantine_still_blocks_writes(monkeypatch):
    path = Path(store._REPORTS_FILE)
    path.write_text("{broken", encoding="utf-8")
    with monkeypatch.context() as m:
        m.setattr(store.os, "replace", lambda *args: (_ for _ in ()).throw(PermissionError("fixture")))
        assert store._load_reports() == []
    assert store._reports_degraded_from == str(path)
    _assert_unavailable(lambda: store._save_reports([]))
    assert path.read_text(encoding="utf-8") == "{broken"


def test_missing_report_quarantine_does_not_allow_overwrite():
    Path(store._REPORTS_FILE).write_text("{broken", encoding="utf-8")
    store._load_reports()
    Path(store._reports_degraded_from).unlink()
    _assert_unavailable(lambda: store._save_reports([]))
    assert not Path(store._REPORTS_FILE).exists()


def test_report_quarantine_names_do_not_overwrite_earlier_evidence(monkeypatch):
    path = Path(store._REPORTS_FILE)
    for text in ("{first", "{second"):
        monkeypatch.setattr(store, "_reports_degraded_from", None)
        path.write_text(text, encoding="utf-8")
        store._load_reports()
    evidence = list(path.parent.glob(path.name + ".corrupt-*"))
    assert len(evidence) == 2
    assert {p.read_text(encoding="utf-8") for p in evidence} == {"{first", "{second"}


def test_report_salvage_requires_fully_valid_history_and_merges_ids():
    Path(store._REPORTS_FILE).write_text("{broken", encoding="utf-8")
    store._load_reports()
    quarantine = Path(store._reports_degraded_from)
    _write(quarantine, [{"id": "old", "status": "已核实"}, "bad"])
    _assert_unavailable(lambda: store._save_reports([{"id": "new", "status": "待核实"}]))
    old = [{"id": "old", "status": "已核实", "extra": 1}, {"id": "new", "status": "待核实"}]
    _write(quarantine, old)
    latest = {"id": "new", "status": "已处理"}
    store._save_reports([latest])
    assert store._load_reports() == [old[0], latest]
    assert store._reports_degraded_from is None
    assert json.loads(quarantine.read_text(encoding="utf-8")) == old


def test_report_salvage_does_not_overwrite_a_new_corrupt_primary():
    path = Path(store._REPORTS_FILE)
    path.write_text("{broken", encoding="utf-8")
    store._load_reports()
    quarantine = Path(store._reports_degraded_from)
    _write(quarantine, [{"id": "old", "status": "已核实"}])
    path.write_text('["new corruption"]', encoding="utf-8")
    _assert_unavailable(lambda: store._save_reports([{"id": "new", "status": "待核实"}]))
    assert path.read_text(encoding="utf-8") == '["new corruption"]'
    assert store._reports_degraded_from == str(quarantine)


def test_report_salvage_preserves_valid_primary_created_during_repair():
    path = Path(store._REPORTS_FILE)
    path.write_text("{broken", encoding="utf-8")
    store._load_reports()
    _write(store._reports_degraded_from, [{"id": "old", "status": "已核实"}])
    _write(path, [{"id": "recovered", "status": "待核实", "extra": 1}])
    store._save_reports([{"id": "new", "status": "待核实"}])
    assert store._load_reports() == [{"id": "old", "status": "已核实"},
                                     {"id": "recovered", "status": "待核实", "extra": 1},
                                     {"id": "new", "status": "待核实"}]


@pytest.mark.parametrize("operation", ["dump", "fsync", "replace"])
def test_atomic_failure_preserves_original_and_cleans_only_owned_temp(tmp_path, monkeypatch, operation):
    path = tmp_path / "atomic.json"
    path.write_bytes(b"old bytes")
    foreign = tmp_path / "atomic.json.tmp"
    foreign.write_bytes(b"another writer")

    def fail(*args, **kwargs):
        raise OSError("fixture disk failure")

    target = store.json if operation == "dump" else store.os
    monkeypatch.setattr(target, operation, fail)
    with pytest.raises(OSError):
        store._atomic_write_json(str(path), [{"new": True}])
    assert path.read_bytes() == b"old bytes"
    assert foreign.read_bytes() == b"another writer"
    assert set(tmp_path.iterdir()) == {path, foreign, tmp_path / "stores"}


def test_atomic_write_flushes_and_fsyncs_before_replace(tmp_path, monkeypatch):
    path = tmp_path / "atomic.json"
    path.write_bytes(b"old")
    fsync = store.os.fsync
    sizes = []

    def check_fsync(fd):
        assert path.read_bytes() == b"old"
        sizes.append(store.os.fstat(fd).st_size)
        fsync(fd)

    monkeypatch.setattr(store.os, "fsync", check_fsync)
    store._atomic_write_json(str(path), {"text": "洪涝"})
    assert sizes and sizes[0] > 0
    assert json.loads(path.read_text(encoding="utf-8")) == {"text": "洪涝"}


def test_parallel_atomic_writers_use_distinct_temp_files(tmp_path, monkeypatch):
    path = tmp_path / "atomic.json"
    barrier = threading.Barrier(2)
    replace = store.os.replace
    replace_lock = threading.Lock()
    sources = []

    def synchronized_replace(src, dst):
        sources.append(src)
        barrier.wait(timeout=5)
        # Exercise overlapping temp lifetimes, not Windows' concurrent MoveFileEx
        # behavior. Store-level locks also serialize same-store replacements.
        with replace_lock:
            replace(src, dst)

    monkeypatch.setattr(store.os, "replace", synchronized_replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda i: store._atomic_write_json(str(path), {"writer": i}), range(2)))
    assert len(set(sources)) == 2
    assert json.loads(path.read_text(encoding="utf-8")) in [{"writer": 0}, {"writer": 1}]
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("kind", ["reports", "subscribers", "users"])
def test_high_level_write_errors_are_store_unavailable(monkeypatch, kind):
    def fail(*args, **kwargs):
        raise OSError("fixture disk failure")

    monkeypatch.setattr(store, "_atomic_write_json", fail)
    monkeypatch.setenv("FLOOD_DEMO", "1")
    operation = {"reports": lambda: store._save_reports([]),
                 "subscribers": lambda: store._save_subscribers([]),
                 "users": store._load_users}[kind]
    _assert_unavailable(operation)


@pytest.mark.parametrize("salt", ["", "legacy$salt"])
def test_legacy_upgrade_synchronizes_generated_salt_and_hash(salt):
    user = _legacy(salt=salt)
    _write(store._AUTH_FILE, [user])
    users = store._load_users()
    assert store._verify_pw("old-password", users[0])
    store._upgrade_pw(users, 0, "old-password")
    upgraded = store._load_users()[0]
    assert upgraded["salt"]
    assert upgraded["hash"].split("$")[2] == upgraded["salt"]
    assert store._verify_pw("old-password", upgraded)
    assert users[0]["salt"] == upgraded["salt"] and users[0]["hash"] == upgraded["hash"]


def test_upgrade_rereads_latest_users_instead_of_overwriting_stale_snapshot():
    user = _legacy()
    _write(store._AUTH_FILE, [user])
    stale = store._load_users()
    current = dict(user, role="admin", retained="latest")
    other = _legacy("new-user", "another-password")
    _write(store._AUTH_FILE, [other, current])
    store._upgrade_pw(stale, 0, "old-password")
    latest = store._load_users()
    assert latest[0] == other
    assert latest[1]["role"] == "admin" and latest[1]["retained"] == "latest"
    assert latest[1]["hash"].startswith("pbkdf2$")
    assert store._verify_pw("old-password", latest[1])


@pytest.mark.parametrize("replacement", [[], [_legacy(password="changed-password")]])
def test_upgrade_does_not_resurrect_removed_user_or_restore_old_password(replacement):
    _write(store._AUTH_FILE, [_legacy()])
    stale = store._load_users()
    _write(store._AUTH_FILE, replacement)
    before = Path(store._AUTH_FILE).read_bytes()
    store._upgrade_pw(stale, 0, "old-password")
    assert Path(store._AUTH_FILE).read_bytes() == before


def test_upgrade_write_failure_does_not_invalidate_verified_login(monkeypatch, caplog):
    _write(store._AUTH_FILE, [_legacy(salt="")])
    users = store._load_users()
    before = Path(store._AUTH_FILE).read_bytes()
    assert store._verify_pw("old-password", users[0])

    def fail(*args, **kwargs):
        raise PermissionError("fixture upgrade failure")

    monkeypatch.setattr(store, "_atomic_write_json", fail)
    store._upgrade_pw(users, 0, "old-password")
    assert store._verify_pw("old-password", users[0])
    assert Path(store._AUTH_FILE).read_bytes() == before
    assert caplog.records and "old-password" not in caplog.text


@pytest.mark.parametrize("lock_name", ["_report_lock", "_subscribers_lock", "_users_lock"])
def test_store_locks_allow_nested_calls(lock_name):
    lock = getattr(store, lock_name)
    assert lock.acquire(timeout=1)
    try:
        acquired = lock.acquire(blocking=False)
        assert acquired
        lock.release()
    finally:
        lock.release()


@pytest.mark.parametrize("iters", ["0", "-1", "2000001", "999999999999999999999999", "1.5", "nan"])
def test_verify_rejects_bad_iterations_before_hashing(monkeypatch, iters):
    user = {"salt": "s", "hash": "pbkdf2$%s$s$%s" % (iters, "0" * 64)}

    def forbidden(*args, **kwargs):
        pytest.fail("invalid iteration count reached PBKDF2")

    monkeypatch.setattr(store.hashlib, "pbkdf2_hmac", forbidden)
    assert store._verify_pw("password", user) is False


@pytest.mark.parametrize("iters", [1, 120000, 2000000])
def test_verify_accepts_bounded_historical_iterations(iters):
    digest = hashlib.pbkdf2_hmac("sha256", b"fixture", b"salt", iters).hex()
    user = {"salt": "salt", "hash": "pbkdf2$%d$salt$%s" % (iters, digest)}
    assert store._verify_pw("fixture", user)


def test_verify_rejects_mismatched_embedded_salt():
    user = {"salt": "s", "hash": store._hash_pw("password", "s")}
    user["hash"] = user["hash"].replace("$s$", "$other$")
    assert store._verify_pw("password", user) is False


@pytest.mark.parametrize("bad_hash", ["pbkdf2$0$s$" + "0" * 64, "pbkdf2$2000001$s$" + "0" * 64])
def test_users_schema_rejects_out_of_range_iterations(bad_hash):
    user = dict(_legacy(), salt="s", hash=bad_hash)
    _write(store._AUTH_FILE, [user])
    _assert_unavailable(store._load_users)


def test_rate_keys_are_capped_without_evicting_active_limited_clients(monkeypatch):
    monkeypatch.setattr(store.time, "monotonic", lambda: 1000.0)
    cap = store.RATE_KEY_CAP
    assert store._rate_ok("protected", limit=1)
    for i in range(cap - 1):
        assert store._rate_ok("rotating:%d" % i, limit=1)
    for i in range(10):
        assert not store._rate_ok("overflow:%d" % i, limit=1)
    assert len(store._report_rl) == cap
    assert not store._rate_ok("protected", limit=1)


def test_rate_expiry_reclaims_keys_but_retains_longer_active_windows(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(store.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(store, "RATE_KEY_CAP", 2, raising=False)
    assert store._rate_ok("short", limit=1, window=2)
    assert store._rate_ok("long", limit=1, window=600)
    assert not store._rate_ok("overflow", limit=1)
    clock[0] += 3
    assert store._rate_ok("new", limit=1)
    assert "short" not in store._report_rl
    assert not store._rate_ok("long", limit=1, window=600)
    assert len(store._report_rl) == 2


def test_periodic_rate_gc_cleans_idle_keys_and_reset_has_no_stale_expiry(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(store.time, "monotonic", lambda: clock[0])
    for i in range(10):
        assert store._rate_ok("idle:%d" % i, window=2)
    clock[0] += 1000
    assert store._rate_ok("live", limit=1)
    assert set(store._report_rl) == {"live"}
    monkeypatch.setattr(store, "_report_rl", {})
    assert store._rate_ok("live", limit=1, window=600)
    clock[0] += 100
    assert store._rate_ok("another")
    assert not store._rate_ok("live", limit=1, window=600)


def test_rate_check_is_atomic_under_concurrent_calls():
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: store._rate_ok("one-client", limit=5), range(100)))
    assert sum(results) == 5
