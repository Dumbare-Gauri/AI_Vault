from vault_shared.storage.errors import StorageContentTooLargeError
from vault_shared.storage.models import ByteStream


def read_bounded(stream: ByteStream, *, max_bytes: int) -> bytes:
    """Collects a stream into memory, but never more than `max_bytes`.

    The stream is always closed, including when the limit is hit, so an
    oversized provider response is abandoned mid-transfer instead of being
    downloaded in full. Callers that can process chunk by chunk should iterate
    the stream directly and not use this at all.
    """
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in stream:
            total += len(chunk)
            if total > max_bytes:
                raise StorageContentTooLargeError(
                    f"The content is larger than the {max_bytes}-byte limit for this operation."
                )
            chunks.append(chunk)
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    return b"".join(chunks)
