import configparser
import datetime as dt
import hashlib
import hmac
import os
from pathlib import Path
import urllib.parse
import xml.etree.ElementTree as ET

import requests


DEFAULT_CONFIG_PATH = os.getenv("ARCHIVE_S3_CONFIG", str(Path.home() / ".s3"))
DEFAULT_PREFIX = os.getenv("ARCHIVE_RAW_OBJECT_PREFIX", "raw-originals")


class S3ConfigError(ValueError):
    pass


class S3UploadError(RuntimeError):
    pass


def _load_s3cmd_config(path=DEFAULT_CONFIG_PATH):
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(path):
        raise S3ConfigError(f"S3 config not found: {path}")
    section = parser["default"]
    access_key = section.get("access_key", "").strip()
    secret_key = section.get("secret_key", "").strip()
    host_base = section.get("host_base", "").strip()
    use_https = section.getboolean("use_https", fallback=True)
    if not access_key or not secret_key or not host_base:
        raise S3ConfigError("S3 config requires access_key, secret_key, and host_base")
    return {
        "access_key": access_key,
        "secret_key": secret_key,
        "host_base": host_base,
        "scheme": "https" if use_https else "http",
    }


def _sign(key, message):
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signature_key(secret_key, date_stamp, region, service):
    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, "aws4_request")


def _hash_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def object_key(storage_key, prefix=DEFAULT_PREFIX):
    storage_key = str(storage_key).lstrip("/")
    prefix = str(prefix or "").strip("/")
    if prefix:
        return f"{prefix}/{storage_key}"
    return storage_key


class S3Client:
    def __init__(self, bucket=None, config_path=DEFAULT_CONFIG_PATH, region=None):
        self.bucket = bucket or os.getenv("ARCHIVE_S3_BUCKET") or os.getenv("ARCHIVE_RAW_OBJECT_BUCKET")
        if not self.bucket:
            raise S3ConfigError("Set ARCHIVE_S3_BUCKET or ARCHIVE_RAW_OBJECT_BUCKET")
        config = _load_s3cmd_config(config_path)
        self.access_key = config["access_key"]
        self.secret_key = config["secret_key"]
        self.host_base = config["host_base"]
        self.scheme = config["scheme"]
        self.region = region or os.getenv("ARCHIVE_S3_REGION", "us-east-1")

    def _url(self, key):
        quoted_key = urllib.parse.quote(key, safe="/")
        return f"{self.scheme}://{self.bucket}.{self.host_base}/{quoted_key}"

    def _headers(self, method, key, payload_hash, content_length=None, content_type=None, metadata=None):
        now = dt.datetime.utcnow()
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        host = f"{self.bucket}.{self.host_base}"
        canonical_uri = "/" + urllib.parse.quote(key, safe="/")

        headers = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if content_length is not None:
            headers["content-length"] = str(content_length)
        if content_type:
            headers["content-type"] = content_type
        for name, value in (metadata or {}).items():
            if value is not None:
                headers[f"x-amz-meta-{name.lower()}"] = str(value)

        signed_header_names = sorted(headers)
        canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in signed_header_names)
        signed_headers = ";".join(signed_header_names)
        canonical_request = "\n".join([
            method,
            canonical_uri,
            "",
            canonical_headers,
            signed_headers,
            payload_hash,
        ])
        credential_scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])
        signing_key = _signature_key(self.secret_key, date_stamp, self.region, "s3")
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        headers["Authorization"] = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self.access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )
        return headers

    def put_file(self, path, key, content_type=None, metadata=None):
        path = Path(path)
        payload_hash = _hash_file(path)
        headers = self._headers(
            "PUT",
            key,
            payload_hash,
            content_length=path.stat().st_size,
            content_type=content_type,
            metadata=metadata,
        )
        with path.open("rb") as handle:
            response = requests.put(self._url(key), data=handle, headers=headers, timeout=(10, 300))
        if response.status_code not in (200, 201):
            raise S3UploadError(f"S3 PUT failed HTTP {response.status_code}: {response.text[:500]}")
        return {
            "uri": f"s3://{self.bucket}/{key}",
            "sha256": payload_hash,
            "bytes": path.stat().st_size,
            "etag": response.headers.get("ETag"),
        }


def list_buckets(config_path=DEFAULT_CONFIG_PATH, region=None):
    config = _load_s3cmd_config(config_path)
    region = region or os.getenv("ARCHIVE_S3_REGION", "us-east-1")
    now = dt.datetime.utcnow()
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(b"").hexdigest()
    host = config["host_base"]
    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    signed_header_names = sorted(headers)
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in signed_header_names)
    signed_headers = ";".join(signed_header_names)
    canonical_request = "\n".join(["GET", "/", "", canonical_headers, signed_headers, payload_hash])
    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signing_key = _signature_key(config["secret_key"], date_stamp, region, "s3")
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        "AWS4-HMAC-SHA256 "
        f"Credential={config['access_key']}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )
    response = requests.get(f"{config['scheme']}://{host}/", headers=headers, timeout=(10, 60))
    if response.status_code != 200:
        raise S3UploadError(f"S3 bucket list failed HTTP {response.status_code}: {response.text[:500]}")
    root = ET.fromstring(response.text)
    return [
        node.text
        for node in root.findall(".//{http://s3.amazonaws.com/doc/2006-03-01/}Name")
        if node.text
    ]
