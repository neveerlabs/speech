from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TypeVar

from audio_capture import AudioCaptureError


Result = TypeVar("Result")


class ApiKeyFailoverError(RuntimeError):
    pass


def parse_api_keys(environ: Mapping[str, str]) -> tuple[str, ...]:
    configured_keys = environ.get("GEMINI_API_KEYS", "")
    keys = tuple(
        dict.fromkeys(key.strip() for key in configured_keys.split(",") if key.strip())
    )
    if keys:
        return keys

    legacy_key = environ.get("GEMINI_API_KEY", "").strip()
    return (legacy_key,) if legacy_key else ()


def _safe_error_message(error: Exception, api_keys: Sequence[str]) -> str:
    message = str(error) or error.__class__.__name__
    for api_key in api_keys:
        message = message.replace(api_key, "[API key disamarkan]")
    return message[:500]


async def run_with_api_key_fallback(
    api_keys: Sequence[str],
    attempt: Callable[[str], Awaitable[Result]],
    on_fallback: Callable[[int, int], None],
    is_stopping: Callable[[], bool],
) -> Result | None:
    failures: list[str] = []
    for index, api_key in enumerate(api_keys):
        if is_stopping():
            return None

        try:
            return await attempt(api_key)
        except AudioCaptureError:
            raise
        except Exception as error:
            failures.append(
                f"API key ke-{index + 1}: {_safe_error_message(error, api_keys)}"
            )
            if index + 1 < len(api_keys) and not is_stopping():
                on_fallback(index + 2, len(api_keys))

    if not is_stopping():
        details = "\n".join(failures)
        raise ApiKeyFailoverError(
            f"Semua {len(api_keys)} API key Gemini gagal.\n{details}"
        )
    return None
