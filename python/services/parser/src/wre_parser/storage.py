"""S3 access for immutable raw artifacts and derived ETL outputs."""

from __future__ import annotations

import asyncio
import io
import json
from typing import Any
from urllib.parse import urlparse

from minio import Minio


class ObjectStore:
    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        region: str,
        raw_bucket: str,
        parsed_bucket: str,
    ) -> None:
        parsed = urlparse(endpoint)
        if not parsed.hostname:
            raise ValueError(f"S3_ENDPOINT is not a URL: {endpoint!r}")
        host = parsed.hostname if parsed.port is None else f"{parsed.hostname}:{parsed.port}"
        self._client = Minio(
            host,
            access_key=access_key,
            secret_key=secret_key,
            secure=parsed.scheme == "https",
            region=region,
        )
        self._raw_bucket = raw_bucket
        self._parsed_bucket = parsed_bucket

    async def ping(self) -> None:
        await asyncio.to_thread(self._client.bucket_exists, self._raw_bucket)
        await asyncio.to_thread(self._client.bucket_exists, self._parsed_bucket)

    async def read_uri(self, uri: str) -> bytes:
        bucket, key = split_uri(uri)
        return await asyncio.to_thread(self._read, bucket, key)

    async def put_json(self, key: str, value: dict[str, Any]) -> str:
        body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        await asyncio.to_thread(
            self._client.put_object,
            self._parsed_bucket,
            key,
            io.BytesIO(body),
            len(body),
            content_type="application/json",
        )
        return f"s3://{self._parsed_bucket}/{key}"

    def _read(self, bucket: str, key: str) -> bytes:
        response = self._client.get_object(bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()


def split_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise ValueError(f"not an s3 uri: {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")
