"""Verified XAuth transport and bounded, non-sliding token validation cache.

Shared verbatim by Gateway and OLS. No credentials or deployment host are defaults.
"""
import hashlib
import json
import math
import os
import socket
import ssl
import threading
import time
from datetime import datetime
from http.client import HTTPConnection, HTTPSConnection
from urllib.parse import urlsplit



class AuthError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class NamedHTTPSConnection(HTTPSConnection):
    def __init__(self, host, port, server_name, context, timeout):
        super().__init__(host, port, timeout=timeout, context=context)
        self.server_name = server_name

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), self.timeout,
                                             self.source_address)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.server_name)


class XAuthClient:
    def __init__(self, base_url=None, ca_certs=None, server_name=None,
                 expected_key_id=None, timeout=5.0, config=None):
        settings = config.get("auth", {}) if isinstance(config, dict) else {}
        self.base_url = (base_url if base_url is not None else
                         os.getenv("XAUTH_BASE_URL", settings.get("xauth_base_url", ""))).rstrip("/")
        self.ca_certs = ca_certs if ca_certs is not None else os.getenv("XAUTH_CA_CERTS", settings.get("xauth_ca_certs"))
        self.server_name = server_name if server_name is not None else os.getenv("XAUTH_SERVER_NAME", settings.get("xauth_server_name"))
        self.expected_key_id = (expected_key_id if expected_key_id is not None else
                                os.getenv("XAUTH_EXPECTED_KEY_ID", settings.get("xauth_expected_key_id")))
        self.timeout = timeout

    def _request(self, method, endpoint, body=None):
        parts = urlsplit(self.base_url)
        if (not parts.hostname or parts.username or parts.password or parts.query or parts.fragment
                or parts.scheme not in ("https", "http")):
            raise AuthError(503, "AUTH_UNAVAILABLE", "Authentication service is not configured")
        # HTTP is only supported for local test/development XAuth instances.
        if parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise AuthError(503, "AUTH_UNAVAILABLE", "Authentication upstream requires HTTPS")
        conn = None
        try:
            if parts.scheme == "https":
                context = ssl.create_default_context()
                if self.ca_certs:
                    context.load_verify_locations(cafile=self.ca_certs)
                conn = NamedHTTPSConnection(parts.hostname, parts.port or 443,
                                            self.server_name or parts.hostname,
                                            context, self.timeout)
            else:
                conn = HTTPConnection(parts.hostname, parts.port or 80, timeout=self.timeout)
            headers = {"Accept": "application/json"}
            data = None
            if body is not None:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                headers["Content-Type"] = "application/json"
            conn.request(method, parts.path.rstrip("/") + endpoint, body=data, headers=headers)
            response = conn.getresponse()
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError("oversized auth response")
            result = json.loads(raw.decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError("invalid auth response")
            if response.status != 200:
                error = result.get("error")
                if response.status in (400, 401, 403, 413, 429) and isinstance(error, dict):
                    code = error.get("code")
                    message = error.get("message")
                    if isinstance(code, str) and isinstance(message, str):
                        raise AuthError(response.status, code, message)
                raise AuthError(503, "AUTH_UNAVAILABLE", "Authentication service is unavailable")
            return result
        except AuthError:
            raise
        except Exception as exc:
            # Never expose endpoint, credentials, TLS diagnostics or upstream bodies.
            raise AuthError(503, "AUTH_UNAVAILABLE", "Authentication service is unavailable") from exc
        finally:
            if conn is not None:
                conn.close()

    def health(self):
        result = self._request("GET", "/health")
        info = result.get("password_encryption")
        if result.get("status") != "ok" or not isinstance(info, dict):
            raise AuthError(503, "AUTH_UNAVAILABLE", "Authentication service is unavailable")
        if self.expected_key_id and info.get("key_id") != self.expected_key_id:
            raise AuthError(503, "AUTH_UNAVAILABLE", "Authentication public key mismatch")
        return result

    def login(self, user, password_encrypted):
        return self._request("POST", "/login", {"user": user,
                                                "password_encrypted": password_encrypted})

    def check(self, token):
        return self._request("POST", "/is_available", {"token": token})


def bearer_token(header):
    if not isinstance(header, str):
        raise AuthError(401, "AUTH_REQUIRED", "Authentication required")
    parts = header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthError(401, "AUTH_REQUIRED", "Bearer authentication required")
    token = parts[1]
    if len(token) > 4096:
        raise AuthError(401, "AUTH_INVALID", "Invalid authentication token")
    return token


class TokenVerifier:
    """Caches only valid identities; hits never move either deadline forward."""

    def __init__(self, client, *, clock=time.time, monotonic=time.monotonic,
                 max_cache_seconds=600.0, max_cache_entries=4096):
        self.client = client
        self.clock, self.monotonic = clock, monotonic
        self._cache = {}
        self._lock = threading.Lock()
        self._token_locks = {}
        self.max_cache_seconds = max(0.0, float(max_cache_seconds))
        self.max_cache_entries = max(1, int(max_cache_entries))

    def verify(self, token):
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise AuthError(401, "AUTH_REQUIRED", "Authentication required")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._lock:
            token_lock = self._token_locks.setdefault(digest, threading.Lock())
        # Serialize misses for one credential, but never hold a process-wide lock while
        # contacting XAuth. A slow token must not stall valid tokens' cache hits.
        with token_lock:
            with self._lock:
                now, monotonic = self.clock(), self.monotonic()
                entry = self._cache.get(digest)
                if entry is not None and monotonic < entry[0] and now < entry[1]:
                    return json.loads(entry[2])
                self._cache.pop(digest, None)
            result = self.client.check(token)
            if not isinstance(result, dict):
                raise AuthError(503, "AUTH_UNAVAILABLE", "Invalid authentication service response")
            if result.get("available") is False:
                raise AuthError(401, "AUTH_INVALID", "Authentication expired or invalid")
            try:
                user, session = result["user"], result["session"]
                uid = user["user_id"]
                expiry = datetime.fromisoformat(session["expires_at"].replace("Z", "+00:00"))
                remaining = session["expires_in"]
                if (result.get("available") is not True or type(uid) is not int or uid < 1
                        or user.get("is_active") is not True or expiry.tzinfo is None
                        or isinstance(remaining, bool) or not isinstance(remaining, (int, float))
                        or not math.isfinite(remaining) or remaining <= 0):
                    raise ValueError("malformed identity")
                expires_at = expiry.timestamp()
                now, monotonic = self.clock(), self.monotonic()
                if expires_at <= now:
                    raise AuthError(401, "AUTH_INVALID", "Authentication expired or invalid")
                ttl = min(self.max_cache_seconds, remaining, expires_at - now)
                serialized = json.dumps(result, ensure_ascii=False)
                with self._lock:
                    # Keep memory bounded even under many distinct valid credentials.
                    if len(self._cache) >= self.max_cache_entries:
                        self._cache = {key: value for key, value in self._cache.items()
                                       if monotonic < value[0] and now < value[1]}
                        if len(self._cache) >= self.max_cache_entries:
                            self._cache.pop(next(iter(self._cache)))
                    self._cache[digest] = (monotonic + ttl, expires_at, serialized)
                return json.loads(serialized)
            except AuthError:
                raise
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise AuthError(503, "AUTH_UNAVAILABLE", "Invalid authentication service response") from exc
