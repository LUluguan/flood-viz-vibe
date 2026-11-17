"""订阅确认及收件人筛选。令牌只存摘要，浏览器链接使用 fragment 避免访问日志记录。"""
import hashlib
import hmac
import math
import re
import secrets
import time
from urllib.parse import urlsplit

import store


def normalize_zone(value):
    value = str(value or '全部').strip()
    if value == '全部':
        return value
    match = re.fullmatch(r'区?([1-9])', value)
    if not match:
        raise ValueError('分区须为区1至区9或全部')
    return '区' + match.group(1)


def public_base_url(value):
    url = value.rstrip('/')
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('PUBLIC_BASE_URL 不得包含凭据、查询或片段')
    if not parsed.hostname or (parsed.scheme != 'https' and not (
            parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1'))):
        raise ValueError('PUBLIC_BASE_URL 须为 HTTPS（本机开发可用 HTTP）')
    return url


def prepare_registration(email, zone):
    """写入待确认变更；已确认地址的分区在新确认前保持原样。"""
    confirm, unsubscribe = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with store._subscribers_lock:
        rows = store._load_subscribers()
        row = next((x for x in rows if x['email'].casefold() == email.casefold()), None)
        if row is None:
            if len(rows) >= store.SUBSCRIBER_CAP:
                raise OverflowError('订阅数已达上限')
            row = {'email': email, 'zone': zone, 'confirmed': False,
                   'created_at': time.strftime('%Y-%m-%d %H:%M:%S')}
            rows.append(row)
        row.update(pending_zone=zone, confirm_hash=hashlib.sha256(confirm.encode()).hexdigest(),
                   confirm_expires=time.time() + 86400)
        # 旧的退订链接必须在新确认前继续有效，防代登记使原链接失效。
        row['pending_unsubscribe_hash'] = hashlib.sha256(unsubscribe.encode()).hexdigest()
        store._save_subscribers(rows)
    return confirm, unsubscribe


def consume_token(token, unsubscribe=False):
    if not isinstance(token, str) or not 16 <= len(token) <= 128:
        return False
    digest = hashlib.sha256(token.encode()).hexdigest()
    field = 'unsubscribe_hash' if unsubscribe else 'confirm_hash'
    with store._subscribers_lock:
        rows = store._load_subscribers()
        for row in rows:
            saved = row.get(field, '')
            if not isinstance(saved, str) or not hmac.compare_digest(saved, digest):
                continue
            if unsubscribe:
                rows.remove(row)
            else:
                expiry = row.get('confirm_expires', 0)
                if not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or time.time() > expiry:
                    return False
                row['zone'] = normalize_zone(row.pop('pending_zone', row['zone']))
                row['confirmed'] = True
                row.pop('confirm_hash', None)
                row.pop('confirm_expires', None)
                if row.get('pending_unsubscribe_hash'):
                    row['unsubscribe_hash'] = row.pop('pending_unsubscribe_hash')
            store._save_subscribers(rows)
            return True
    return False


def confirmed_recipients(warning, subscribers):
    affected = {'区' + str(z['zone']) for z in warning.get('zones', []) if z.get('level') not in (None, '无')}
    recipients = []
    for row in subscribers:
        if row.get('confirmed') is not True:
            continue
        try:
            zone = normalize_zone(row.get('zone'))
        except ValueError:
            continue
        if zone == '全部' or zone in affected:
            recipients.append(row['email'])
    return sorted(set(recipients))
