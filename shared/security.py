"""Local-service security controls. No broker or credential files are imported."""
import asyncio
import hmac
import ipaddress
import re
import logging
from urllib.parse import urlsplit
from fastapi import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

# HTTPX normally logs complete request URLs, which can contain webhook secrets.
for _name in ('httpx', 'httpcore'):
    logging.getLogger(_name).setLevel(logging.CRITICAL)
    logging.getLogger(_name).disabled = True


def valid_secret(value):
    if not isinstance(value, str) or len(value) < 32 or len(value) > 256:
        return False
    if any(word in value.lower() for word in ('replace', 'your-', 'your_', 'paste', 'changeme', 'example')):
        return False
    return value.isascii() and not any(ch.isspace() for ch in value)


def matches_secret(received, expected):
    return hmac.compare_digest((received or '').encode('utf-8'), expected.encode('utf-8'))


async def read_body(request, limit=65536):
    """Bound memory while consuming chunked bodies; stop stalled requests."""
    length = request.headers.get('content-length')
    if length:
        try:
            if int(length) < 0 or int(length) > limit:
                raise HTTPException(413, detail='payload too large')
        except ValueError:
            raise HTTPException(400, detail='invalid content length')
    content = bytearray()
    try:
        async with asyncio.timeout(10):
            async for chunk in request.stream():
                if len(content) + len(chunk) > limit:
                    raise HTTPException(413, detail='payload too large')
                content.extend(chunk)
    except TimeoutError:
        raise HTTPException(408, detail='request timed out')
    return bytes(content)


def validate_local_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1') or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Internal service URL must use HTTP loopback without credentials/query')
    return url


def validate_router_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Order router URL must use HTTPS without embedded user credentials')
    return url


def signal_summary(signal):
    """Allowlist log fields; never persist arbitrary extras or request headers."""
    return {key: signal[key] for key in ('ticker','action','quantity','order_type','stop_loss','take_profit','limit_price') if key in signal}


class LocalOnlyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            client = scope.get('client')
            try:
                allowed = bool(client) and ipaddress.ip_address(client[0]).is_loopback
            except ValueError:
                allowed = False
            if not allowed:
                await send({'type':'http.response.start','status':403,'headers':[(b'content-type',b'application/json')]})
                await send({'type':'http.response.body','body':b'{"detail":"local access only"}'})
                return
        async def secured_send(message):
            if message['type'] == 'http.response.start':
                message['headers'] = list(message.get('headers',[])) + [
                    (b'cache-control',b'no-store'),(b'x-content-type-options',b'nosniff'),
                    (b'content-security-policy',b"default-src 'none'; frame-ancestors 'none'")]
            await send(message)
        await self.app(scope,receive,secured_send)


def secure_app(app):
    app.add_middleware(LocalOnlyMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost','127.0.0.1','[::1]'])
