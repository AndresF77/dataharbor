"""Object storage for landed source files.

The API only needs three operations, so they sit behind a small protocol:
`S3ObjectStore` talks to MinIO (or any S3-compatible store) and
`MemoryObjectStore` keeps objects in a dict for tests and local runs
without MinIO. Object keys are content-addressed, so writing the same
bytes twice is harmless.
"""

import base64
import hashlib
import io
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit


class StorageError(Exception):
    """The object store could not be reached or refused the request."""


@dataclass(frozen=True)
class StoredObject:
    key: str
    size_bytes: int
    sha256: str


class ObjectStore(Protocol):
    bucket: str

    def put(self, key: str, data: bytes, *, content_type: str, sha256: str) -> StoredObject: ...

    def head(self, key: str) -> StoredObject | None: ...

    def get(self, key: str) -> bytes: ...


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MemoryObjectStore:
    """In-process store; objects disappear with the process."""

    def __init__(self, bucket: str = "landing") -> None:
        self.bucket = bucket
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.fail = False  # tests flip this to simulate an outage

    def _check(self) -> None:
        if self.fail:
            raise StorageError("object store unavailable")

    def put(self, key: str, data: bytes, *, content_type: str, sha256: str) -> StoredObject:
        self._check()
        if sha256_hex(data) != sha256:
            raise StorageError("checksum mismatch while writing")
        self.objects[key] = (bytes(data), content_type)
        return StoredObject(key=key, size_bytes=len(data), sha256=sha256)

    def head(self, key: str) -> StoredObject | None:
        self._check()
        if key not in self.objects:
            return None
        data, _ = self.objects[key]
        return StoredObject(key=key, size_bytes=len(data), sha256=sha256_hex(data))

    def get(self, key: str) -> bytes:
        self._check()
        if key not in self.objects:
            raise StorageError("object not found")
        return self.objects[key][0]


def _client_errors() -> tuple[type[BaseException], ...]:
    """Errors the MinIO client raises when the store is down or refuses a request."""
    from minio.error import MinioException
    from urllib3.exceptions import HTTPError

    return (MinioException, HTTPError, OSError)


class S3ObjectStore:
    """MinIO / S3 store. The SHA-256 travels as `x-amz-checksum-sha256`, so the
    server rejects the upload if the bytes change in transit, and is kept as
    object metadata for later verification."""

    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str) -> None:
        from minio import Minio  # imported lazily: tests never need it

        parts = urlsplit(endpoint)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("object store endpoint must look like http(s)://host:port")
        self.bucket = bucket
        self._client = Minio(
            parts.netloc,
            access_key=access_key,
            secret_key=secret_key,
            secure=parts.scheme == "https",
        )

    def put(self, key: str, data: bytes, *, content_type: str, sha256: str) -> StoredObject:
        checksum = base64.b64encode(bytes.fromhex(sha256)).decode()
        try:
            self._client.put_object(
                self.bucket,
                key,
                io.BytesIO(data),
                length=len(data),
                content_type=content_type,
                metadata={"sha256": sha256, "x-amz-checksum-sha256": checksum},
            )
        except _client_errors() as exc:
            raise StorageError(type(exc).__name__) from exc
        return StoredObject(key=key, size_bytes=len(data), sha256=sha256)

    def head(self, key: str) -> StoredObject | None:
        from minio.error import S3Error

        try:
            stat = self._client.stat_object(self.bucket, key)
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchObject"):
                return None
            raise StorageError(exc.code) from exc
        except _client_errors() as exc:
            raise StorageError(type(exc).__name__) from exc
        metadata = {k.lower(): v for k, v in (stat.metadata or {}).items()}
        return StoredObject(
            key=key, size_bytes=stat.size or 0, sha256=metadata.get("x-amz-meta-sha256", "")
        )

    def get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(self.bucket, key)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()
        except _client_errors() as exc:
            raise StorageError(type(exc).__name__) from exc
