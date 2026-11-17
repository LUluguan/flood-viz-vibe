# -*- coding: utf-8 -*-
"""store.py — JSON 持久化 store(reports/subscribers/users) + 频控 + 原子写 + 文本消毒。
从 app.py 切出(2026-10 结构债), 边界: 纯文件 I/O + 内存锁, 不依赖 FastAPI/栅格。"""
import collections
import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid

_log = logging.getLogger("flood.store")
ROOT = os.path.dirname(os.path.abspath(__file__))
# 启动时设置 FLOOD_DATA_DIR 即可将三个 store 放入 Docker 命名卷; 不在导入时创建文件。
DATA_DIR = os.path.abspath(os.environ.get("FLOOD_DATA_DIR") or ROOT)


class StoreUnavailable(RuntimeError):
    """存储未配置、损坏或 I/O 失败; HTTP 层统一转换为 503。"""


def demo_enabled():
    """仅显式 FLOOD_DEMO=1 开启演示; 每次读取, 不缓存环境开关。"""
    return os.environ.get("FLOOD_DEMO") == "1"


# ==== 报讯 store ====
_REPORT_DIR = os.path.join(DATA_DIR, "reports")
_REPORTS_FILE = os.path.join(_REPORT_DIR, "reports.json")
_reports_degraded_from = None   # 隔离现场路径(非 None 时拒绝覆盖写)
_report_lock = threading.RLock()
_report_rl = {}                 # key -> {hits: deque, expires_at: monotonic time}
_state_quarantines = [0]        # app.py /health 读取

# ==== 订阅 store ====
_SUBSCRIBERS_FILE = os.path.join(_REPORT_DIR, "subscribers.json")
_subscribers_degraded_from = None
_subscribers_lock = threading.RLock()
SUBSCRIBER_CAP = 500

# ==== 用户 store ====
_AUTH_FILE = os.path.join(DATA_DIR, "web_users.json")
_users_degraded_from = None
_users_lock = threading.RLock()

_TRUST_PROXY = os.environ.get("FLOOD_TRUST_PROXY", "").strip() == "1"


def _atomic_write_json(path, data):
    """同目录独占临时文件 → flush/fsync → replace; 仅清理本次创建的临时文件。
    底层保留 OSError, 由 store 边界转换为 StoreUnavailable(口令升级可降级跳过)。"""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=os.path.basename(path) + ".", suffix=".tmp",
                                         delete=False) as f:
            tmp = f.name
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            except OSError:
                _log.warning("本次 JSON 写入的临时文件清理失败: %s", tmp)


def _client_ip(request):
    """客户端 IP: 默认取 socket 对端(恶意客户端可伪造 XFF 绕过频控);
    仅当 FLOOD_TRUST_PROXY=1(部署在可信反代之后)时才取 X-Forwarded-For 首值。"""
    if _TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


_rate_lock = threading.Lock()   # check-then-act 原子性
RATE_KEY_CAP = 10_000
_RATE_GC_INTERVAL = 60.0
_rate_gc_next = 0.0


def _rate_ok(key, limit=20, window=60.0):
    """有界滑动窗口频控; 满表仅清理过期键, 绝不驱逐活跃键让轮换攻击绕过。
    hits/expiry 同存 _report_rl, 替换/清空该字典不会遗留另一张过期索引。"""
    global _rate_gc_next
    if limit <= 0 or window <= 0:
        return False
    with _rate_lock:
        now = time.monotonic()
        if now >= _rate_gc_next or (key not in _report_rl and len(_report_rl) >= RATE_KEY_CAP):
            expired = [k for k, entry in _report_rl.items() if entry["expires_at"] <= now]
            for k in expired:
                del _report_rl[k]
            _rate_gc_next = now + _RATE_GC_INTERVAL
        entry = _report_rl.get(key)
        if entry is None:
            if len(_report_rl) >= RATE_KEY_CAP:
                return False
            entry = {"hits": collections.deque(), "expires_at": now + window}
            _report_rl[key] = entry
        dq = entry["hits"]
        while dq and now - dq[0] >= window:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        entry["expires_at"] = max(entry["expires_at"], now + window)
        return True


def _clean_text(v, maxlen):
    """用户自由文本: 必须是字符串, 剥除 <...> 标签片段(存储型 XSS 加固), 截断。"""
    if not isinstance(v, str):
        raise ValueError("文本字段必须为字符串")
    v = re.sub(r"<[^>]*>", "", v)
    return v.strip()[:maxlen]


# ==== 最小 schema: 不拒绝历史附加字段, 但不把坏元素当空记录 ====
def _valid_report(row):
    return (isinstance(row, dict) and isinstance(row.get("id"), str)
            and isinstance(row.get("status"), str))


def _valid_subscriber(row):
    return (isinstance(row, dict) and isinstance(row.get("email"), str)
            and isinstance(row.get("zone"), str))


def _validate_table(value, valid_row):
    if not isinstance(value, list):
        raise ValueError("存储顶层必须为数组")
    if any(not valid_row(row) for row in value):
        raise ValueError("存储含非法记录")
    return value


def _read_table(path, valid_row):
    with open(path, encoding="utf-8") as f:
        return _validate_table(json.load(f), valid_row)


def _write_table(path, value, valid_row):
    try:
        _validate_table(value, valid_row)
        _atomic_write_json(path, value)
    except (OSError, ValueError, TypeError, RecursionError) as e:
        raise StoreUnavailable("存储不可用, 拒绝覆盖写入") from e


# ==== 报讯 store 读写 ====
def _reports_file():
    return _REPORTS_FILE


def _load_reports():
    """坏表移入 UUID 隔离件并兼容返回 []; 隔离失败/现场丢失也不能解除拒写保护。"""
    global _reports_degraded_from
    with _report_lock:
        if _reports_degraded_from == _reports_file():
            raise StoreUnavailable("报讯存储隔离失败, 已原地保护, 拒绝覆盖写入")
        if _reports_degraded_from is not None and os.path.isfile(_reports_degraded_from):
            try:
                return _read_table(_reports_degraded_from, _valid_report)
            except (OSError, ValueError, RecursionError) as exc:
                raise StoreUnavailable("报讯隔离存储不可读, 已拒绝覆盖写入") from exc
        if _reports_degraded_from is not None:
            raise StoreUnavailable("报讯存储现场丢失, 需人工恢复, 拒绝覆盖写入")
        p = _reports_file()
        try:
            return _read_table(p, _valid_report)
        except FileNotFoundError:
            return []
        except (ValueError, RecursionError):
            # 先立保护标记, 即使 replace 失败也不得继续按缺表初始化。
            _reports_degraded_from = p
            q = p + ".corrupt-" + uuid.uuid4().hex
            try:
                os.replace(p, q)
                _reports_degraded_from = q
            except OSError:
                _log.warning("reports.json 损坏隔离失败, 已原地保护: %s", p)
            _state_quarantines[0] += 1
            _log.warning("报讯存储受损, 已保护现场: %s", _reports_degraded_from)
            return []
        except OSError as e:
            _reports_degraded_from = p
            raise StoreUnavailable("报讯存储不可读, 已拒绝覆盖写入") from e


def _save_reports(lst):
    """仅在完整历史通过 schema 后合并恢复; 不丢弃坏元素, 不因隔离件消失而解锁。"""
    global _reports_degraded_from
    with _report_lock:
        try:
            _validate_table(lst, _valid_report)
        except ValueError as e:
            raise StoreUnavailable("报讯记录非法, 拒绝覆盖写入") from e
        if _reports_degraded_from is None:
            _load_reports()  # 直接调用 save 也必须先探测已有坏表。
        recovering = _reports_degraded_from is not None
        if recovering:
            try:
                old = _read_table(_reports_degraded_from, _valid_report)
                if _reports_degraded_from != _reports_file():
                    # 修复期间主文件若重新出现, 同样验证/合并, 不覆盖另一份现场。
                    try:
                        current = _read_table(_reports_file(), _valid_report)
                    except FileNotFoundError:
                        current = []
                    current_ids = {x["id"] for x in current}
                    old = [x for x in old if x["id"] not in current_ids] + current
            except (OSError, ValueError, RecursionError) as e:
                raise StoreUnavailable("报讯存储受损, 已保护现场, 拒绝覆盖写入") from e
            known = {x["id"] for x in lst}
            lst = [x for x in old if x["id"] not in known] + lst
        _write_table(_reports_file(), lst, _valid_report)
        if recovering:
            _log.warning("已从隔离件恢复合法历史报讯并合并写入")
            _reports_degraded_from = None


# ==== 订阅 store ====
def _load_subscribers():
    global _subscribers_degraded_from
    with _subscribers_lock:
        if _subscribers_degraded_from is not None:
            raise StoreUnavailable("订阅存储受损, 已原地保护, 拒绝覆盖写入")
        try:
            return _read_table(_SUBSCRIBERS_FILE, _valid_subscriber)
        except FileNotFoundError:
            return []
        except (OSError, ValueError, RecursionError) as e:
            _subscribers_degraded_from = _SUBSCRIBERS_FILE
            raise StoreUnavailable("订阅存储不可用, 已原地保护, 拒绝覆盖写入") from e


def _save_subscribers(lst):
    with _subscribers_lock:
        _load_subscribers()
        _write_table(_SUBSCRIBERS_FILE, lst, _valid_subscriber)


# ==== 用户 store ====
# 保留既有 12 万轮参数兼容当前登录时序; 这不是 OWASP 的最低推荐值。
_PBKDF2_ITERS = 120_000
_PBKDF2_MAX_ITERS = 2_000_000
_PW_DIGEST_RE = re.compile(r"[0-9a-fA-F]{64}")


def _pbkdf2_parts(value, salt):
    """在执行昂贵哈希前验证存储格式、盐一致性及迭代上限。"""
    prefix, iters_s, embedded_salt, digest = value.split("$")
    if (prefix != "pbkdf2" or not re.fullmatch(r"[0-9]{1,7}", iters_s)
            or not 1 <= int(iters_s) <= _PBKDF2_MAX_ITERS
            or embedded_salt != salt or not _PW_DIGEST_RE.fullmatch(digest)):
        raise ValueError("非法 PBKDF2 存储格式")
    return int(iters_s), salt, digest.lower()


def _valid_user(row):
    if (not isinstance(row, dict) or not isinstance(row.get("username"), str)
            or not row["username"].strip() or not isinstance(row.get("salt"), str)
            or not isinstance(row.get("hash"), str) or row.get("role") not in ("admin", "public")):
        return False
    if row["hash"].startswith("pbkdf2$"):
        try:
            _pbkdf2_parts(row["hash"], row["salt"])
        except ValueError:
            return False
        return True
    # 历史 SHA256 允许空字符串盐, 登录升级时同时生成盐并改写哈希。
    return _PW_DIGEST_RE.fullmatch(row["hash"]) is not None


def _hash_pw(pw, salt):
    """PBKDF2-HMAC-SHA256; 新哈希与校验共用合法轮数范围和嵌入盐格式。"""
    if not 1 <= _PBKDF2_ITERS <= _PBKDF2_MAX_ITERS or "$" in salt:
        raise ValueError("非法 PBKDF2 参数")
    dig = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"),
                              salt.encode("utf-8"), _PBKDF2_ITERS)
    return "pbkdf2$%d$%s$%s" % (_PBKDF2_ITERS, salt, dig.hex())


def _verify_pw(pw, user):
    """常数时间比较; 兼容旧 SHA256, 非法记录/迭代次数直接拒绝而非执行哈希。"""
    if (not isinstance(pw, str) or not isinstance(user, dict)
            or not isinstance(user.get("salt"), str) or not isinstance(user.get("hash"), str)):
        return False
    value, salt = user["hash"], user["salt"]
    try:
        if value.startswith("pbkdf2$"):
            iters, salt, digest = _pbkdf2_parts(value, salt)
            calc = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("utf-8"), iters).hex()
            return hmac.compare_digest(calc, digest)
        if not _PW_DIGEST_RE.fullmatch(value):
            return False
        legacy = hashlib.sha256((salt + pw).encode("utf-8")).hexdigest()
        return hmac.compare_digest(legacy, value.lower())
    except (ValueError, OverflowError):
        return False


def _upgrade_pw(users, idx, pw):
    """验证后的尽力升级: 持用户锁重读最新表, 不用登录快照覆盖并发修改。
    仅 OSError 记录升级失败并继续已验证的登录; 损坏表仍抛 StoreUnavailable。"""
    global _users_degraded_from
    with _users_lock:
        if _users_degraded_from is not None:
            raise StoreUnavailable("用户存储受损, 已原地保护, 拒绝覆盖写入")
        u = users[idx]
        if u["hash"].startswith("pbkdf2$"):
            return
        try:
            # 不走缺表播种分支, 避免并发删除用户表时重新创建账号。
            latest = _read_table(_AUTH_FILE, _valid_user)
        except OSError as e:
            _log.warning("口令升级读取失败, 保留已验证登录: %s", type(e).__name__)
            return
        except (ValueError, RecursionError) as e:
            _users_degraded_from = _AUTH_FILE
            raise StoreUnavailable("用户存储受损, 已原地保护, 拒绝覆盖写入") from e
        for i, current in enumerate(latest):
            if current["username"] != u["username"]:
                continue
            if current["hash"].startswith("pbkdf2$") or not _verify_pw(pw, current):
                return
            # 升级时轮换盐, 也兼容旧 SHA256 盐含 PBKDF2 分隔符的合法历史。
            salt = uuid.uuid4().hex[:12]
            updated = dict(current, salt=salt, hash=_hash_pw(pw, salt))
            latest[i] = updated
            try:
                _atomic_write_json(_AUTH_FILE, latest)
            except OSError as e:
                _log.warning("口令升级写入失败, 保留已验证登录: %s", type(e).__name__)
                return
            # 落盘成功才同步快照, 失败时旧 salt/hash 仍可用于已验证登录。
            u["salt"], u["hash"] = updated["salt"], updated["hash"]
            return


def _load_users():
    """已有用户表不重置; 缺表仅显式演示或合格管理员初始化口令可创建。"""
    global _users_degraded_from
    with _users_lock:
        if _users_degraded_from is not None:
            raise StoreUnavailable("用户存储受损, 已原地保护, 拒绝覆盖写入")
        try:
            return _read_table(_AUTH_FILE, _valid_user)
        except FileNotFoundError:
            if demo_enabled():
                accounts = (("admin", "admin123", "admin"), ("public", "123456", "public"))
            else:
                pw = os.environ.get("FLOOD_ADMIN_PASSWORD", "")
                if len(pw.strip()) < 12 or pw.strip() in ("admin123", "123456"):
                    raise StoreUnavailable("用户表缺失, 需设置至少 12 字符的非默认 FLOOD_ADMIN_PASSWORD") from None
                accounts = (("admin", pw, "admin"),)
            users = []
            for uname, pw, role in accounts:
                salt = uuid.uuid4().hex[:12]
                users.append({"username": uname, "salt": salt,
                              "hash": _hash_pw(pw, salt), "role": role})
            _write_table(_AUTH_FILE, users, _valid_user)
            return users
        except (OSError, ValueError, RecursionError) as e:
            _users_degraded_from = _AUTH_FILE
            raise StoreUnavailable("用户存储不可用, 已原地保护, 拒绝覆盖写入") from e
