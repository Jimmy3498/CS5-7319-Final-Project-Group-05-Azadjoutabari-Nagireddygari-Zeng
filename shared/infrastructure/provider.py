import json
from collections import deque
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from ipaddress import ip_address
from threading import Lock
from urllib.parse import urlsplit

from ..codec import load_json
from ..errors import ApplicationError, ErrorCode
from ..models.ai import ProviderCapture, ProviderMessage, ProviderResponse


class CompatibleHttpProvider:
    mode = "chat-completions-compatible"

    def __init__(self, connect_timeout: float = 15, read_timeout: float = 120):
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout

    def complete(
        self, messages: tuple[ProviderMessage, ...], capture: ProviderCapture
    ) -> ProviderResponse:
        profile = capture.profile
        key = capture.credential.value
        if (
            profile.protocol != self.mode
            or not profile.model.strip()
            or not messages
            or not key.strip()
            or not key.isascii()
            or any(ord(c) < 32 or ord(c) == 127 for c in key)
            or any(
                message.role not in ("system", "user", "assistant")
                or not isinstance(message.content, str)
                for message in messages
            )
        ):
            raise ApplicationError(ErrorCode.INVALID, "Invalid provider request")

        try:
            url = urlsplit(profile.endpoint)
            host, port = url.hostname, url.port
            try:
                loopback = ip_address(host or "").is_loopback
            except ValueError:
                loopback = host == "localhost"

            valid = (
                bool(host)
                and port != 0
                and url.username is None
                and url.password is None
                and not url.fragment
                and not any(ord(c) <= 32 or ord(c) == 127 for c in profile.endpoint)
                and (url.scheme == "https" or (url.scheme == "http" and loopback))
            )
        except ValueError:
            valid = False

        if not valid:
            raise ApplicationError(ErrorCode.INVALID, "Invalid provider endpoint")

        body = json.dumps(
            {
                "model": profile.model,
                "messages": [
                    {"role": message.role, "content": message.content} for message in messages
                ],
                "stream": False,
            },
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

        path = url.path or "/"
        if url.query:
            path += "?" + url.query

        connection_type = HTTPSConnection if url.scheme == "https" else HTTPConnection
        connection = connection_type(host, port, timeout=self.connect_timeout)
        sent = False

        try:
            connection.connect()
            connection.sock.settimeout(self.read_timeout)
            sent = True
            connection.request(
                "POST",
                path,
                body=body,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
            )
            response = connection.getresponse()

            if not 200 <= response.status < 300:
                code = (
                    ErrorCode.PROVIDER_UNCERTAIN if response.status >= 500 else ErrorCode.PROVIDER
                )
                raise ApplicationError(code, f"Provider returned HTTP {response.status}")

            raw = response.read(1024 * 1024 + 1)

        except (OSError, HTTPException):
            code = ErrorCode.PROVIDER_UNCERTAIN if sent else ErrorCode.PROVIDER
            raise ApplicationError(code, "Provider connection failed or timed out") from None
        except (ValueError, UnicodeError):
            raise ApplicationError(
                ErrorCode.PROVIDER, "Invalid provider request encoding"
            ) from None
        finally:
            connection.close()

        try:
            data = load_json(raw.decode("utf-8"))
            choice = data["choices"][0]
            message = choice["message"]
            content = message["content"]
            reason = choice.get("finish_reason")

            if (
                message.get("role") != "assistant"
                or not isinstance(content, str)
                or (reason is not None and not isinstance(reason, str))
            ):
                raise ValueError("Invalid response shape")

            usage = data.get("usage") or {}
            input_tokens = usage.get("prompt_tokens")
            output_tokens = usage.get("completion_tokens")

            for value in (input_tokens, output_tokens):
                if value is not None and (type(value) is not int or value < 0):
                    raise ValueError("Invalid usage")

            return ProviderResponse(content, input_tokens, output_tokens, reason)

        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise ApplicationError(
                ErrorCode.PROVIDER, "Invalid or oversized provider response"
            ) from None


class ScriptedTestProvider:
    """Return supplied responses in order for deterministic tests."""

    mode = "scripted-test-double"

    def __init__(self, responses: tuple[ProviderResponse, ...]) -> None:
        self._responses = deque(responses)
        self._lock = Lock()
        self.calls = 0

    def complete(
        self, messages: tuple[ProviderMessage, ...], capture: ProviderCapture
    ) -> ProviderResponse:
        with self._lock:
            if not self._responses:
                raise ApplicationError(ErrorCode.PROVIDER, "Test-provider script exhausted")
            self.calls += 1
            return self._responses.popleft()
