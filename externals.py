# -*- coding: utf-8 -*-
"""
externals.py — 全部出网集成(唯一出口): Open-Meteo 雨情 / LLM 问答 / 预警触达。

设计约定(2026-10 对标§四9/10/17/21):
- 竞赛可离线运行: 任何配置缺失/网络失败一律返回 None 或 (False, 原因), 由 app.py
  回退到原有演示逻辑, 服务绝不因外网不可用而 5xx;
- 一切凭据走 .env(WECOM_WEBHOOK_URL / SMTP_* / LLM_API_KEY), 不入前端可见文件;
- 本模块零 FastAPI 依赖, 便于单测打桩(requests/smtplib)。
"""
import json
import logging
import smtplib
import threading
import time
from email.header import Header
from email.mime.text import MIMEText

import requests

_log = logging.getLogger("flood.externals")

_HTTP_TIMEOUT = 8          # 出网默认超时(s): 快失败, 不拖端点
_LLM_TIMEOUT = 25          # LLM 允许更长(生成+工具回合)
_UA = {"User-Agent": "C2132-flood-webgis/1.6 (competition demo)"}


# ================= Open-Meteo(免费无key): 实况(近24h) + 预报(未来72h) =================
OM_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
# 研究区中心(珠江新城); 站点演示值由 app 层做确定性扰动
OM_LAT, OM_LON = 23.11, 113.32

_om_cache = {"ts": 0.0, "data": None}
_om_lock = threading.Lock()


def fetch_openmeteo(past_days=1, forecast_days=3, cache_s=600):
    """取研究区小时降雨(实况回看+预报)。返回
    {"times": [...], "precip": [...], "fetched_at": epoch} 或 None(失败/超时)。
    结果缓存 cache_s 秒(接口免费档限速, 演示页 60s 轮询不打穿)。"""
    now = time.time()
    with _om_lock:
        if _om_cache["data"] is not None and now - _om_cache["ts"] < cache_s:
            return _om_cache["data"]
    try:
        r = requests.get(OM_FORECAST_URL, params={
            "latitude": OM_LAT, "longitude": OM_LON,
            "hourly": "precipitation",
            "past_days": past_days, "forecast_days": forecast_days,
            "timezone": "Asia/Shanghai",
        }, timeout=_HTTP_TIMEOUT, headers=_UA)
        r.raise_for_status()
        h = r.json().get("hourly") or {}
        times, precip = h.get("time") or [], h.get("precipitation") or []
        if len(times) < 24 or len(times) != len(precip):
            return None
        out = {"times": times, "precip": [0.0 if p is None else float(p) for p in precip],
               "fetched_at": now}
        with _om_lock:
            _om_cache.update(ts=now, data=out)
        return out
    except Exception as e:
        _log.warning("Open-Meteo 获取失败(回退演示雨情): %s", e)
        return None


# ================= LLM 问答(OpenAI 兼容 /chat/completions; 智谱 GLM-4-Flash 免费档) =================
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
        if d.get("choices"):
            return d
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
        if r.status_code == 200 and d.get("errcode") in (0, None):
            return True, ""
        return False, "HTTP %s %s" % (r.status_code, d.get("errmsg", ""))
    except Exception as e:
        return False, str(e)


def send_smtp(cfg, subject, body, recipients):
    """SMTP 文本邮件(预警订阅触达)。cfg: {host,port,user,password,use_tls}。
    返回 (成功数, 失败原因列表); 未配置返回 (0, ["未配置 SMTP_*"])。"""
    if not (cfg.get("host") and cfg.get("user") and cfg.get("password")):
        return 0, ["未配置 SMTP_*"]
    recipients = [x for x in (recipients or []) if x][:20]   # 单批上限, 防误发
    if not recipients:
        return 0, ["收件人为空"]
    ok, errs = 0, []
    try:
        cls = smtplib.SMTP_SSL if cfg.get("use_tls", True) else smtplib.SMTP
        with cls(cfg["host"], int(cfg.get("port") or 465), timeout=_HTTP_TIMEOUT) as s:
            s.login(cfg["user"], cfg["password"])
            for to in recipients:
                msg = MIMEText(body, "plain", "utf-8")
                msg["Subject"] = Header(subject, "utf-8")
                msg["From"] = cfg["user"]
                msg["To"] = to
                try:
                    s.sendmail(cfg["user"], [to], msg.as_string())
                    ok += 1
                except Exception as e:
                    errs.append("%s: %s" % (to, e))
    except Exception as e:
        errs.append("SMTP 连接/登录失败: %s" % e)
    return ok, errs


def notify_async(wecom_url, smtp_cfg, subject, wecom_md, mail_body, recipients):
    """后台线程触达(端点不被 webhook/SMTP 阻塞); 全失败仅记日志。"""
    def _run():
        results = []
        if wecom_url:
            ok, err = send_wecom(wecom_url, wecom_md)
            results.append(("wecom", ok, err))
        if smtp_cfg and smtp_cfg.get("host"):
            ok, errs = send_smtp(smtp_cfg, subject, mail_body, recipients)
            results.append(("smtp", ok > 0, "; ".join(errs)))
        for name, ok, err in results:
            _log.info("预警触达 %s: %s %s", name, "OK" if ok else "FAIL", err or "")
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# ================= 触达去重冷却(本地 JSON 状态, 断电重启不重复轰炸) =================
def notify_due(state_path, key, cooldown_s=1800):
    """冷却判断+登记: 同一 key(情景+等级) cooldown 秒内只触达一次。"""
    now = time.time()
    last = {}
    try:
        with open(state_path, encoding="utf-8") as f:
            last = json.load(f)
        if not isinstance(last, dict):
            last = {}
    except Exception:
        pass
    if now - float(last.get(key, 0) or 0) < cooldown_s:
        return False
    last[key] = now
    try:
        import os
        os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(last, f, ensure_ascii=False)
    except OSError as e:
        _log.warning("触达冷却状态写盘失败(仍发送): %s", e)
    return True
