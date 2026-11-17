# -*- coding: utf-8 -*-
"""
externals.py — 全部出网集成(唯一出口): Open-Meteo 雨情 / LLM 问答 / 预警触达。

设计约定(2026-10 对标§四9/10/17/21):
- 竞赛可离线运行: 任何配置缺失/网络失败一律返回 None 或 (False, 原因), 由 app.py
  回退到原有演示逻辑, 服务绝不因外网不可用而 5xx;
- 一切凭据走 .env(WECOM_WEBHOOK_URL / SMTP_* / LLM_API_KEY), 不入前端可见文件;
- 本模块零 FastAPI 依赖, 便于单测打桩(requests/smtplib)。
"""
import datetime
import json
import logging
import math
import os
import smtplib
import ssl
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from email.header import Header
from email.mime.text import MIMEText

import requests

_log = logging.getLogger("flood.externals")

_HTTP_TIMEOUT = 8          # 出网默认超时(s): 快失败, 不拖端点
_LLM_TIMEOUT = 25          # LLM 允许更长(生成+工具回合)
_UA = {"User-Agent": "C2132-flood-viz-vibe/1.6 (competition demo)"}


# ================= Open-Meteo(免费无key): 实况(近24h) + 预报(未来72h) =================
OM_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
# 研究区中心(珠江新城); 站点演示值由 app 层做确定性扰动
OM_LAT, OM_LON = 23.11, 113.32

_OM_CACHE_MAX = 16
_om_cache = OrderedDict()  # (past_days, forecast_days) -> monotonic TTL + data
_om_lock = threading.Lock()


def _copy_om(data):
    # 调用者可修改列表, 但不能污染其他请求的缓存。
    return {"times": list(data["times"]), "precip": list(data["precip"]),
            "fetched_at": data["fetched_at"]}


def _validated_hourly(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("hourly"), dict):
        raise ValueError("hourly 必须为对象")
    hourly = payload["hourly"]
    times, precip = hourly.get("time"), hourly.get("precipitation")
    if not isinstance(times, list) or not isinstance(precip, list) or \
            len(times) < 24 or len(times) != len(precip):
        raise ValueError("小时序列须为等长数组且至少含 24 小时")
    previous = None
    for stamp in times:
        if not isinstance(stamp, str) or len(stamp) != 16:
            raise ValueError("小时戳须为 YYYY-MM-DDTHH:MM 字符串")
        parsed = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M")
        if parsed.minute != 0 or (previous is not None and
                                  parsed - previous != datetime.timedelta(hours=1)):
            raise ValueError("小时戳必须逐小时递增且对齐整点")
        previous = parsed
    # None 是缺测, 不能伪装成 0mm 降雨；bool/数字字符串同样不是有效数值。
    if any(type(p) not in (int, float) or not math.isfinite(p) or p < 0 for p in precip):
        raise ValueError("降水必须为有限非负数, 不允许缺测")
    return {"times": times, "precip": [float(p) for p in precip], "fetched_at": time.time()}


def fetch_openmeteo(past_days=1, forecast_days=3, cache_s=600):
    """取研究区小时降雨。返回 {times: list[str], precip: list[float], fetched_at: epoch}
    或 None(失败/超时/畸形)。时间为 Asia/Shanghai 的本地小时串, 降水单位 mm。
    按整数天数对缓存, monotonic TTL + 16 项 LRU；cache_s=0 强制刷新。
    持锁覆盖获取与填充以防并发首击；每次 HTTP 请求均设置超时。"""
    try:
        key = (int(past_days), int(forecast_days))
        ttl = float(cache_s)
        if not math.isfinite(ttl) or ttl < 0:
            raise ValueError("cache_s 必须为有限非负数")
        with _om_lock:
            now = time.monotonic()
            for expired in [k for k, v in _om_cache.items() if now >= v["expires"]]:
                del _om_cache[expired]
            entry = _om_cache.get(key)
            if entry is not None and now - entry["ts"] < ttl:
                _om_cache.move_to_end(key)
                return _copy_om(entry["data"])
            r = requests.get(OM_FORECAST_URL, params={
                "latitude": OM_LAT, "longitude": OM_LON,
                "hourly": "precipitation",
                "past_days": key[0], "forecast_days": key[1],
                "timezone": "Asia/Shanghai",
            }, timeout=_HTTP_TIMEOUT, headers=_UA)
            r.raise_for_status()
            out = _validated_hourly(r.json())
            fetched = time.monotonic()
            _om_cache[key] = {"ts": fetched, "expires": fetched + ttl, "data": out}
            _om_cache.move_to_end(key)
            while len(_om_cache) > _OM_CACHE_MAX:
                _om_cache.popitem(last=False)
            return _copy_om(out)
    except Exception as e:
        _log.warning("Open-Meteo 获取失败(回退演示雨情): %s", e)
        return None


# ================= LLM 问答(OpenAI 兼容 /chat/completions; 智谱 GLM-4-Flash 免费档) =================
def _reject_json_constant(value):
    raise ValueError("JSON 不允许非有限常量 %s" % value)


def _valid_chat_response(data):
    if not isinstance(data, dict):
        return False
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    for choice in choices:
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            return False
        msg = choice["message"]
        content, calls = msg.get("content"), msg.get("tool_calls")
        if content is not None and not isinstance(content, str):
            return False
        if calls is not None and not isinstance(calls, list):
            return False
        if not calls and not (content and content.strip()):
            return False
        for call in calls or []:
            if not isinstance(call, dict) or call.get("type") != "function" or \
                    not isinstance(call.get("id"), str) or not call["id"]:
                return False
            fn = call.get("function")
            if not isinstance(fn, dict) or not isinstance(fn.get("name"), str) or not fn["name"]:
                return False
            args = fn.get("arguments")
            if not isinstance(args, str):
                return False
            # app 工具执行器需要对象参数；坏 JSON/数组/null 均回退规则问答, 不调用工具。
            if not isinstance(json.loads(args, parse_constant=_reject_json_constant), dict):
                return False
    return True


def llm_chat(base_url, api_key, model, messages, tools=None):
    """单次对话补全。返回响应 dict 或 None(未配置/网络失败)。
    app.py 负责工具回合编排; 本函数只做传输与最小校验。"""
    if not api_key or not base_url:
        return None
    try:
        r = requests.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + api_key,
                     "Content-Type": "application/json", **_UA},
            json={"model": model, "messages": messages,
                  "tools": tools or [], "tool_choice": "auto", "temperature": 0.3},
            timeout=_LLM_TIMEOUT)
        r.raise_for_status()
        d = r.json()
        if _valid_chat_response(d):
            return d
        _log.warning("LLM 响应结构无效(回退规则问答)")
        return None
    except Exception as e:
        _log.warning("LLM 调用失败(回退规则问答): %s", e)
        return None


# ================= 预警触达: 企业微信群机器人 webhook + SMTP 邮件 =================
def send_wecom(webhook_url, markdown):
    """企业微信群机器人 markdown 消息。返回 (ok, err)。"""
    if not webhook_url:
        return False, "未配置 WECOM_WEBHOOK_URL"
    try:
        r = requests.post(webhook_url, json={"msgtype": "markdown",
                                             "markdown": {"content": markdown[:3800]}},
                          timeout=_HTTP_TIMEOUT, headers=_UA)
        d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if not isinstance(d, dict) or type(d.get("errcode")) is not int:
            return False, "HTTP %s: 企业微信响应缺少有效 errcode" % r.status_code
        if r.status_code == 200 and d["errcode"] == 0:
            return True, ""
        return False, "HTTP %s errcode=%s %s" % (r.status_code, d["errcode"], d.get("errmsg", ""))
    except Exception as e:
        return False, str(e)


_NOTIFY_RECIPIENT_MAX = 20


def _notification_recipients(recipients):
    targets, seen = [], set()
    for value in recipients or []:
        if not isinstance(value, str):
            continue
        address = value.strip()
        normalized = address.casefold()
        if not address or normalized in seen:
            continue
        seen.add(normalized)
        targets.append(address)
        if len(targets) >= _NOTIFY_RECIPIENT_MAX:
            break
    return targets


def send_smtp(cfg, subject, body, recipients):
    """SMTP 文本邮件。cfg 支持 security='ssl'|'starttls'|'none' (优先于 use_tls)。
    未指定 security 时保留旧 use_tls=True -> SSL, False -> 明文及默认 465 端口。
    显式 security 的默认端口分别为 465/587/25；TLS 使用系统受信证书校验。
    返回 (SMTP 接受的收件人数, 失败原因列表), 不代表最终投递成功。单批去重后最多 20 人。"""
    if not (cfg.get("host") and cfg.get("user") and cfg.get("password")):
        return 0, ["未配置 SMTP_*"]
    recipients = _notification_recipients(recipients)
    if not recipients:
        return 0, ["收件人为空"]
    ok, errs = 0, []
    try:
        security = cfg.get("security")
        legacy = security is None
        if legacy:
            security = "ssl" if cfg.get("use_tls", True) else "none"
        if security not in ("ssl", "starttls", "none"):
            return 0, ["SMTP security 必须为 ssl/starttls/none"]
        default_port = 465 if legacy else {"ssl": 465, "starttls": 587, "none": 25}[security]
        cls = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
        kwargs = {"timeout": _HTTP_TIMEOUT}
        if security == "ssl":
            kwargs["context"] = ssl.create_default_context()
        with cls(cfg["host"], int(cfg.get("port") or default_port), **kwargs) as s:
            if security == "starttls":
                s.ehlo()
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
            s.login(cfg["user"], cfg["password"])
            for to in recipients:
                try:
                    msg = MIMEText(body, "plain", "utf-8")
                    msg["Subject"] = Header(subject, "utf-8")
                    msg["From"] = cfg["user"]
                    msg["To"] = to
                    refused = s.sendmail(cfg["user"], [to], msg.as_string())
                    if refused:
                        errs.append("%s: SMTP 拒收" % to)
                    else:
                        ok += 1
                except Exception as e:
                    errs.append("%s: %s" % (to, e))
    except Exception as e:
        errs.append("SMTP 连接/登录失败: %s" % e)
    return ok, errs


_notify_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="flood-notify")
_notify_slots = threading.BoundedSemaphore(16)  # 正在执行 + 待执行总量, 不使用无界提交


def notify_async(wecom_url, smtp_cfg, subject, wecom_md, mail_body, recipients):
    """非阻塞提交。返回 {queued: bool, reason: str, future: Future | None}, 不代表已送达。
    Future.result() 返回各已配置渠道的结果: wecom={ok,error},
    smtp={ok,accepted,requested,errors}；accepted 仅指 SMTP 接受, 部分失败 ok=False。
    调用方对外只序列化 queued/reason, 不能把 Future 放进 JSON 或将 queued 当 sent。
    最多 4 个工作线程、16 个在途任务, 满载/提交失败不发送并明确返回拒绝原因。"""
    smtp_cfg = dict(smtp_cfg or {})
    recipients = _notification_recipients(recipients)
    if not wecom_url and not smtp_cfg.get("host"):
        return {"queued": False, "reason": "未配置通知渠道", "future": None}
    # 捕获本次提交使用的信号量, 回调不依赖未来可能被替换的全局变量。
    slots = _notify_slots
    if not slots.acquire(blocking=False):
        _log.warning("预警触达未排队: 队列已满")
        return {"queued": False, "reason": "通知队列已满", "future": None}

    def _run():
        results = {}
        if wecom_url:
            try:
                ok, err = send_wecom(wecom_url, wecom_md)
            except Exception as e:
                ok, err = False, str(e)
            results["wecom"] = {"ok": bool(ok), "error": err}
        if smtp_cfg.get("host"):
            try:
                accepted, errors = send_smtp(smtp_cfg, subject, mail_body, recipients)
            except Exception as e:
                accepted, errors = 0, [str(e)]
            results["smtp"] = {"ok": bool(recipients) and accepted == len(recipients) and not errors,
                               "accepted": accepted, "requested": len(recipients), "errors": errors}
        for name, result in results.items():
            log = _log.info if result["ok"] else _log.warning
            log("预警触达 %s: %s %s", name, "ACCEPTED" if result["ok"] else "FAIL",
                result.get("error") or "; ".join(result.get("errors", [])))
        return results

    try:
        future = _notify_executor.submit(_run)
    except Exception as e:
        slots.release()
        _log.warning("预警触达未排队: %s", e)
        return {"queued": False, "reason": str(e), "future": None}
    future.add_done_callback(lambda _future: slots.release())  # 异常/取消也释放额度
    _log.info("预警触达已排队(尚未确认发送)")
    return {"queued": True, "reason": "", "future": future}


# ================= 触达去重冷却(本地 JSON 状态, 断电重启不重复轰炸) =================
_notify_lock = threading.Lock()
_NOTIFY_MAX_KEYS = 4096
_NOTIFY_MAX_BYTES = 4 * 1024 * 1024
_NOTIFY_RETENTION_S = 30 * 24 * 3600
_NOTIFY_KEY_MAX = 128


def notify_due(state_path, key, cooldown_s=1800):
    """进程内串行冷却判断+原子登记, 成功持久化才允许发送。磁盘 schema 仍为 {key: epoch}。
    损坏/读写失败均关闭发送并记录日志, 不重置坏状态。保留最多 4096 项/30 天；
    不驱逐保留期内的键, 容量满时拒绝新 key。cooldown_s 支持 0..30 天。
    仅保证本进程互斥；多进程部署须在外层使用统一锁/持久化去重服务。
    登记不是发送成功凭证: 后续排队/发送失败仍保留冷却, 优先避免重复轰炸。"""
    with _notify_lock:
        tmp = None
        try:
            cooldown = float(cooldown_s)
            if not isinstance(key, str) or not key or len(key) > _NOTIFY_KEY_MAX or \
                    not math.isfinite(cooldown) or not 0 <= cooldown <= _NOTIFY_RETENTION_S:
                raise ValueError("无效 key 或 cooldown_s(须在 0..30 天内)")
            now = time.time()
            try:
                with open(state_path, encoding="utf-8") as f:
                    raw = f.read(_NOTIFY_MAX_BYTES + 1)
                if len(raw) > _NOTIFY_MAX_BYTES:
                    raise ValueError("冷却状态过大")
                last = json.loads(raw)
            except FileNotFoundError:
                last = {}
            if not isinstance(last, dict) or any(
                    not k or len(k) > _NOTIFY_KEY_MAX or type(ts) not in (int, float) or
                    not math.isfinite(ts) or ts < 0 for k, ts in last.items()):
                raise ValueError("冷却状态结构或时间戳损坏")
            if key in last and now - last[key] < cooldown:
                return False
            last = {k: ts for k, ts in last.items() if now - ts < _NOTIFY_RETENTION_S}
            if len(last) > _NOTIFY_MAX_KEYS or (key not in last and len(last) >= _NOTIFY_MAX_KEYS):
                _log.warning("触达冷却状态已满(不发送)")
                return False
            last[key] = now
            directory = os.path.dirname(os.path.abspath(state_path))
            os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                             prefix=".notify-", suffix=".tmp", delete=False) as f:
                tmp = f.name
                json.dump(last, f, ensure_ascii=False, allow_nan=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, state_path)
            return True
        except Exception as e:
            _log.warning("触达冷却状态读取/登记失败(不发送): %s", e)
            return False
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass  # replace 成功后源文件已不存在
                except OSError as e:
                    _log.warning("触达冷却临时文件清理失败: %s", e)
