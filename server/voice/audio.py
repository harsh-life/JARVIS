"""Raw audio, held in request memory only (docs/27 §2, PRD #27, LIFE-002).

`AudioBuffer` is the one container server-side audio lives in. It is never
written to disk, a log, a trace, the audit trail, memory or the vault. When the
request ends — success or failure — `release()` zero-fills it. Copies the HTTP
client makes while sending it to a configured provider are released with that
request; nothing here keeps a reference to one.
"""

from __future__ import annotations

# Media types a server STT provider is sent. Anything else is refused before a
# byte leaves the process.
ALLOWED_AUDIO_TYPES = frozenset({
    "audio/wav", "audio/x-wav", "audio/wave", "audio/mpeg", "audio/mp3", "audio/mp4", "audio/m4a",
    "audio/x-m4a", "audio/webm", "audio/ogg", "audio/flac", "audio/x-flac", "audio/amr",
})

_EXTENSIONS = {
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/mpeg": "mp3", "audio/mp3": "mp3",
    "audio/mp4": "m4a", "audio/m4a": "m4a", "audio/x-m4a": "m4a", "audio/webm": "webm", "audio/ogg": "ogg",
    "audio/flac": "flac", "audio/x-flac": "flac", "audio/amr": "amr",
}


class AudioTooLarge(Exception):
    pass


class AudioBuffer:
    """Bounded, in-memory, zeroed on release."""

    def __init__(self, *, media_type: str, max_bytes: int) -> None:
        self.media_type = media_type
        self._max = max_bytes
        self._data = bytearray()
        self._released = False

    def append(self, chunk: bytes) -> None:
        if self._released:
            raise RuntimeError("audio buffer already released")
        if len(self._data) + len(chunk) > self._max:
            self.release()
            raise AudioTooLarge()
        self._data.extend(chunk)

    def try_append(self, chunk: bytes) -> bool:
        """`append`, reporting an over-limit chunk as False (the buffer is then
        already released)."""

        try:
            self.append(chunk)
        except AudioTooLarge:
            return False
        return True

    def __len__(self) -> int:
        return len(self._data)

    @property
    def released(self) -> bool:
        return self._released

    @property
    def extension(self) -> str:
        return _EXTENSIONS.get(self.media_type, "bin")

    def payload(self) -> bytes:
        """The bytes, for the one outbound request to the configured provider."""

        if self._released:
            raise RuntimeError("audio buffer already released")
        return bytes(self._data)

    def release(self) -> None:
        for index in range(len(self._data)):
            self._data[index] = 0
        self._data = bytearray()
        self._released = True

    def __repr__(self) -> str:  # never the bytes
        return f"AudioBuffer({self.media_type}, {len(self._data)} bytes, released={self._released})"


def normalize_media_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    media = content_type.split(";", 1)[0].strip().lower()
    return media if media in ALLOWED_AUDIO_TYPES else None


__all__ = ["ALLOWED_AUDIO_TYPES", "AudioBuffer", "AudioTooLarge", "normalize_media_type"]
