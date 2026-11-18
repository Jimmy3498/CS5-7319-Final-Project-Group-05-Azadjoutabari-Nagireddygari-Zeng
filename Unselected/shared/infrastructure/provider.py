import json
from collections import deque
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from ipaddress import ip_address
from threading import Lock
from urllib.parse import quote, urlsplit

from ..codec import load_json
from ..errors import ApplicationError, ErrorCode
from ..models.ai import ProviderCapture, ProviderMessage, ProviderResponse


class CompatibleHttpProvider:
    mode = "chat-completions-compatible"
    protocols = {
        "chat-completions-compatible",
        "anthropic-messages",
        "google-generate-content",
    }

    def __init__(self, connect_timeout: float = 15, read_timeout: float = 120):
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout

    def complete(
        self, messages: tuple[ProviderMessage, ...], capture: ProviderCapture
    ) -> ProviderResponse:
        profile = capture.profile
        key = capture.credential.value
        if (
            profile.protocol not in self.protocols
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

        endpoint = profile.endpoint
        if profile.protocol == "google-generate-content":
            endpoint = endpoint.replace("{model}", quote(profile.model, safe="-_./"))

        try:
            url = urlsplit(endpoint)
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
                and not any(ord(c) <= 32 or ord(c) == 127 for c in endpoint)
                and (url.scheme == "https" or (url.scheme == "http" and loopback))
            )
        except ValueError:
            valid = False

        if not valid:
            raise ApplicationError(ErrorCode.INVALID, "Invalid provider endpoint")

        payload, headers = self._request(profile.protocol, profile.model, messages, key)
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")

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
                headers=headers,
            )
            response = connection.getresponse()

            if not 200 <= response.status < 300:
                code = (
                    ErrorCode.PROVIDER_UNCERTAIN if response.status >= 500 else ErrorCode.PROVIDER
                )
                raise ApplicationError(code, f"Provider returned HTTP {response.status}")

            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ApplicationError(ErrorCode.PROVIDER, "Provider response exceeds 1 MB")

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
            content, input_tokens, output_tokens, reason = self._response(profile.protocol, data)

            if not isinstance(content, str) or (reason is not None and not isinstance(reason, str)):
                raise ValueError("Invalid response shape")

            for value in (input_tokens, output_tokens):
                if value is not None and (type(value) is not int or value < 0):
                    raise ValueError("Invalid usage")

            return ProviderResponse(content, input_tokens, output_tokens, reason)

        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise ApplicationError(
                ErrorCode.PROVIDER, "Invalid or oversized provider response"
            ) from None

    @staticmethod
    def _request(
        protocol: str,
        model: str,
        messages: tuple[ProviderMessage, ...],
        key: str,
    ) -> tuple[dict, dict[str, str]]:
        common = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if protocol == "chat-completions-compatible":
            return (
                {
                    "model": model,
                    "messages": [
                        {"role": message.role, "content": message.content} for message in messages
                    ],
                    "stream": False,
                },
                {**common, "Authorization": f"Bearer {key}"},
            )
        if protocol == "anthropic-messages":
            system = "\n\n".join(
                message.content for message in messages if message.role == "system"
            )
            conversation = [
                {"role": message.role, "content": message.content}
                for message in messages
                if message.role != "system"
            ]
            payload = {"model": model, "max_tokens": 4096, "messages": conversation}
            if system:
                payload["system"] = system
            return (
                payload,
                {**common, "x-api-key": key, "anthropic-version": "2023-06-01"},
            )
        return (
            {
                "systemInstruction": {
                    "parts": [
                        {"text": message.content}
                        for message in messages
                        if message.role == "system"
                    ]
                },
                "contents": [
                    {
                        "role": "model" if message.role == "assistant" else "user",
                        "parts": [{"text": message.content}],
                    }
                    for message in messages
                    if message.role != "system"
                ],
            },
            {**common, "x-goog-api-key": key},
        )

    @staticmethod
    def _response(protocol: str, data: dict) -> tuple[str, int | None, int | None, str | None]:
        if protocol == "chat-completions-compatible":
            choice = data["choices"][0]
            message = choice["message"]
            if message.get("role") != "assistant":
                raise ValueError("Invalid response role")
            usage = data.get("usage") or {}
            return (
                message["content"],
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                choice.get("finish_reason"),
            )
        if protocol == "anthropic-messages":
            content = "".join(
                part["text"] for part in data["content"] if part.get("type") == "text"
            )
            usage = data.get("usage") or {}
            reason = data.get("stop_reason")
            return (
                content,
                usage.get("input_tokens"),
                usage.get("output_tokens"),
                "stop" if reason in {None, "end_turn", "stop_sequence"} else reason,
            )
        candidate = data["candidates"][0]
        content = "".join(part["text"] for part in candidate["content"]["parts"])
        usage = data.get("usageMetadata") or {}
        reason = candidate.get("finishReason")
        return (
            content,
            usage.get("promptTokenCount"),
            usage.get("candidatesTokenCount"),
            "stop" if reason in {None, "STOP"} else reason,
        )


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
