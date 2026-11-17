"""服务层基础设施：有界 single-flight 缓存、请求体限制与安全响应头。"""
import json
import logging
import threading
import uuid
from collections import OrderedDict
from concurrent.futures import Future

from starlette.datastructures import MutableHeaders
from starlette.formparsers import MultiPartException
from starlette.responses import JSONResponse

_log = logging.getLogger('flood')


class Memo:
    """相同键仅一个计算者；等待者得到同一结果或异常，异常不进入缓存。"""
    def __init__(self, limit=64):
        if limit is not None and limit < 1:
            raise ValueError('缓存容量必须为正整数')
        self._limit = limit or 64
        self._data = OrderedDict()
        self._inflight = {}
        self._lock = threading.Lock()
        self._hits = self._misses = 0

    def stats(self):
        with self._lock:
            return self._hits, self._misses

    def get_or_compute(self, key, fn):
        with self._lock:
            if key in self._data:
                self._hits += 1
                self._data.move_to_end(key)
                return self._data[key]
            future = self._inflight.get(key)
            leader = future is None
            if leader:
                self._misses += 1
                future = self._inflight[key] = Future()
            else:
                self._hits += 1
        if not leader:
            return future.result()
        try:
            value = fn()
        except BaseException as exc:
            # 即使计算者取消/异常，等待者也必须被唤醒。
            with self._lock:
                self._inflight.pop(key, None)
                future.set_exception(exc)
            raise
        with self._lock:
            self._data[key] = value
            while len(self._data) > self._limit:
                self._data.popitem(last=False)
            self._inflight.pop(key, None)
            future.set_result(value)
        return value


class _BodyTooLarge(MultiPartException):
    """让 Starlette multipart 解析器沿自身异常路径关闭已打开的临时文件。"""


class RequestGuardMiddleware:
    """在 multipart 解析前累计 receive 字节，不信任单独的 Content-Length。"""
    def __init__(self, app, upload_limit=65 * 1024 * 1024, body_limit=4 * 1024 * 1024):
        self.app = app
        self.upload_limit = upload_limit  # 64MiB 文件 + 有界 multipart 元数据
        self.body_limit = body_limit

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        request_id = uuid.uuid4().hex
        scope.setdefault('state', {})['request_id'] = request_id
        path = scope.get('path', '')
        cap = self.upload_limit if path == '/api/predict' else self.body_limit
        seen = 0
        exceeded = False
        started = False
        rewritten = False

        async def safe_send(message):
            nonlocal started, rewritten
            if message['type'] == 'http.response.start':
                started = True
                if exceeded:
                    message = dict(message, status=413, headers=[])
                    rewritten = True
                headers = MutableHeaders(scope=message)
                headers.setdefault('X-Content-Type-Options', 'nosniff')
                headers.setdefault('X-Frame-Options', 'DENY')
                headers.setdefault('Referrer-Policy', 'no-referrer')
                headers.setdefault('Permissions-Policy', 'geolocation=(self), camera=(self), microphone=()')
                headers.setdefault('Content-Security-Policy', "frame-ancestors 'none'; base-uri 'self'; object-src 'none'")
                headers['X-Request-ID'] = request_id
                if path.startswith(('/api/auth', '/api/report', '/api/subscribe', '/reports/')):
                    headers['Cache-Control'] = 'no-store'
                if rewritten:
                    headers['Content-Type'] = 'application/json; charset=utf-8'
            elif message['type'] == 'http.response.body' and rewritten:
                message = {'type': 'http.response.body', 'body': json.dumps({
                    'error': '请求体超过上限', 'request_id': request_id}, ensure_ascii=False).encode(), 'more_body': False}
                rewritten = False
            await send(message)

        async def reject(status, text):
            await JSONResponse({'error': text, 'request_id': request_id}, status_code=status)(scope, receive, safe_send)

        async def limited_receive():
            nonlocal seen, exceeded
            message = await receive()
            if message['type'] == 'http.request':
                seen += len(message.get('body', b''))
                if seen > cap:
                    exceeded = True
                    raise _BodyTooLarge('请求体超过上限')
            return message

        lengths = [value for name, value in scope.get('headers', []) if name.lower() == b'content-length']
        if lengths:
            try:
                if len(lengths) != 1 or int(lengths[0]) < 0:
                    raise ValueError
                if int(lengths[0]) > cap:
                    return await reject(413, '请求体超过上限')
            except ValueError:
                return await reject(400, 'Content-Length 格式不正确')
        try:
            await self.app(scope, limited_receive, safe_send)
        except Exception as exc:
            if started:
                raise
            if exceeded:
                await reject(413, '请求体超过上限')
                return
            import store
            if isinstance(exc, getattr(store, 'StoreUnavailable', ())):
                _log.warning('存储不可用 request_id=%s path=%s', request_id, path)
                await reject(503, '存储暂时不可用，请联系维护者')
                return
            _log.exception('未处理异常 request_id=%s path=%s', request_id, path)
            await reject(500, '服务内部错误，请联系维护者')
