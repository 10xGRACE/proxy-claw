"""MinIO storage client for request/response logs and API key management."""

import io
import json
import logging
import os
import secrets
import time
from datetime import datetime
from typing import Any

from minio import Minio
from minio.error import S3Error

log = logging.getLogger("proxy.storage")

# Bucket names
BUCKET_LOGS = "proxy-logs"
BUCKET_KEYS = "proxy-keys"
BUCKET_CONFIG = "proxy-config"

# Default MinIO connection
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
MINIO_SECURE = os.environ.get("MINIO_SECURE", "false").lower() == "true"


class StorageClient:
    """Wraps MinIO client for proxy-specific storage operations."""

    def __init__(self):
        self._client: Minio | None = None

    async def init(self):
        """Initialize MinIO client and ensure buckets exist."""
        try:
            self._client = Minio(
                MINIO_ENDPOINT,
                access_key=MINIO_ACCESS_KEY,
                secret_key=MINIO_SECRET_KEY,
                secure=MINIO_SECURE,
            )
            for bucket in (BUCKET_LOGS, BUCKET_KEYS, BUCKET_CONFIG):
                if not self._client.bucket_exists(bucket):
                    self._client.make_bucket(bucket)
                    log.info("created bucket: %s", bucket)
                else:
                    log.info("bucket exists: %s", bucket)

            log.info("minio storage ready (%s)", MINIO_ENDPOINT)
        except Exception as e:
            log.error("minio init failed: %s", e)
            self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    # ── Request/Response Logging ──────────────────────────

    def store_request_log(
        self,
        request_id: str,
        method: str,
        path: str,
        client_ip: str,
        request_headers: dict,
        request_body: bytes,
        response_status: int,
        response_headers: dict,
        response_body: bytes,
        duration_ms: float,
        error: str | None = None,
    ):
        """Store a request/response pair in MinIO."""
        if not self._client:
            return

        try:
            ts = datetime.utcnow()
            # Organize by date: YYYY/MM/DD/request_id.json
            prefix = ts.strftime("%Y/%m/%d")
            obj_name = f"{prefix}/{request_id}.json"

            # Try to decode bodies as text, fall back to base64
            req_body_str = _safe_decode(request_body)
            resp_body_str = _safe_decode(response_body)

            data = {
                "id": request_id,
                "timestamp": ts.isoformat() + "Z",
                "method": method,
                "path": path,
                "client_ip": client_ip,
                "request": {
                    "headers": request_headers,
                    "body": req_body_str,
                    "size": len(request_body),
                },
                "response": {
                    "status": response_status,
                    "headers": response_headers,
                    "body": resp_body_str,
                    "size": len(response_body),
                },
                "duration_ms": round(duration_ms, 1),
                "error": error,
            }

            payload = json.dumps(data, indent=2).encode()
            self._client.put_object(
                BUCKET_LOGS,
                obj_name,
                io.BytesIO(payload),
                len(payload),
                content_type="application/json",
            )
        except Exception as e:
            log.error("failed to store request log %s: %s", request_id, e)

    def get_request_log(self, request_id: str) -> dict | None:
        """Retrieve a stored request/response by ID. Searches recent days."""
        if not self._client:
            return None

        # Search last 7 days
        for days_ago in range(7):
            ts = datetime.utcnow()
            from datetime import timedelta
            d = ts - timedelta(days=days_ago)
            prefix = d.strftime("%Y/%m/%d")
            obj_name = f"{prefix}/{request_id}.json"
            try:
                resp = self._client.get_object(BUCKET_LOGS, obj_name)
                data = json.loads(resp.read().decode())
                resp.close()
                resp.release_conn()
                return data
            except S3Error:
                continue
            except Exception as e:
                log.error("error fetching request log %s: %s", request_id, e)
                return None
        return None

    def list_request_logs(self, limit: int = 100, date: str | None = None) -> list[dict]:
        """List recent request logs. date format: YYYY/MM/DD or None for today."""
        if not self._client:
            return []

        try:
            if date is None:
                date = datetime.utcnow().strftime("%Y/%m/%d")

            results = []
            objects = self._client.list_objects(BUCKET_LOGS, prefix=f"{date}/", recursive=True)
            for obj in objects:
                try:
                    resp = self._client.get_object(BUCKET_LOGS, obj.object_name)
                    data = json.loads(resp.read().decode())
                    resp.close()
                    resp.release_conn()
                    # Extract model and token usage from bodies
                    model, input_tokens, output_tokens = _extract_api_info(data)

                    # Return summary (without full bodies for listing)
                    results.append({
                        "id": data["id"],
                        "timestamp": data["timestamp"],
                        "method": data["method"],
                        "path": data["path"],
                        "client_ip": data["client_ip"],
                        "status": data["response"]["status"],
                        "duration_ms": data["duration_ms"],
                        "request_size": data["request"]["size"],
                        "response_size": data["response"]["size"],
                        "error": data.get("error"),
                        "model": model,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    })
                except Exception:
                    continue

            # Sort by timestamp descending
            results.sort(key=lambda x: x["timestamp"], reverse=True)
            return results[:limit]
        except Exception as e:
            log.error("error listing request logs: %s", e)
            return []

    # ── API Key Management ────────────────────────────────

    def create_api_key(self, name: str, created_by: str = "dashboard") -> dict:
        """Create a new API key and store it in MinIO."""
        if not self._client:
            raise RuntimeError("storage not available")

        key_id = secrets.token_hex(8)
        api_key = f"pck_{secrets.token_urlsafe(32)}"
        now = datetime.utcnow().isoformat() + "Z"

        key_data = {
            "id": key_id,
            "name": name,
            "key": api_key,
            "created_at": now,
            "created_by": created_by,
            "active": True,
            "last_used": None,
            "usage_count": 0,
        }

        payload = json.dumps(key_data, indent=2).encode()
        self._client.put_object(
            BUCKET_KEYS,
            f"keys/{key_id}.json",
            io.BytesIO(payload),
            len(payload),
            content_type="application/json",
        )
        log.info("created API key: %s (%s)", name, key_id)
        return key_data

    def list_api_keys(self) -> list[dict]:
        """List all API keys (with keys masked)."""
        if not self._client:
            return []

        results = []
        try:
            objects = self._client.list_objects(BUCKET_KEYS, prefix="keys/", recursive=True)
            for obj in objects:
                try:
                    resp = self._client.get_object(BUCKET_KEYS, obj.object_name)
                    data = json.loads(resp.read().decode())
                    resp.close()
                    resp.release_conn()
                    # Mask the key for listing
                    masked = data.copy()
                    k = masked["key"]
                    masked["key_masked"] = k[:7] + "…" + k[-4:]
                    results.append(masked)
                except Exception:
                    continue
            results.sort(key=lambda x: x["created_at"], reverse=True)
        except Exception as e:
            log.error("error listing API keys: %s", e)
        return results

    def get_api_key(self, key_id: str) -> dict | None:
        """Get a specific API key by ID."""
        if not self._client:
            return None
        try:
            resp = self._client.get_object(BUCKET_KEYS, f"keys/{key_id}.json")
            data = json.loads(resp.read().decode())
            resp.close()
            resp.release_conn()
            return data
        except S3Error:
            return None
        except Exception as e:
            log.error("error fetching API key %s: %s", key_id, e)
            return None

    def validate_api_key(self, api_key: str) -> dict | None:
        """Validate an API key and return its data if valid."""
        if not self._client:
            return None

        try:
            objects = self._client.list_objects(BUCKET_KEYS, prefix="keys/", recursive=True)
            for obj in objects:
                try:
                    resp = self._client.get_object(BUCKET_KEYS, obj.object_name)
                    data = json.loads(resp.read().decode())
                    resp.close()
                    resp.release_conn()
                    if data.get("key") == api_key and data.get("active"):
                        # Update usage
                        data["last_used"] = datetime.utcnow().isoformat() + "Z"
                        data["usage_count"] = data.get("usage_count", 0) + 1
                        payload = json.dumps(data, indent=2).encode()
                        self._client.put_object(
                            BUCKET_KEYS,
                            obj.object_name,
                            io.BytesIO(payload),
                            len(payload),
                            content_type="application/json",
                        )
                        return data
                except Exception:
                    continue
        except Exception as e:
            log.error("error validating API key: %s", e)
        return None

    def toggle_api_key(self, key_id: str, active: bool) -> dict | None:
        """Enable or disable an API key."""
        if not self._client:
            return None
        try:
            data = self.get_api_key(key_id)
            if not data:
                return None
            data["active"] = active
            payload = json.dumps(data, indent=2).encode()
            self._client.put_object(
                BUCKET_KEYS,
                f"keys/{key_id}.json",
                io.BytesIO(payload),
                len(payload),
                content_type="application/json",
            )
            log.info("API key %s %s", key_id, "enabled" if active else "disabled")
            return data
        except Exception as e:
            log.error("error toggling API key %s: %s", key_id, e)
            return None

    def delete_api_key(self, key_id: str) -> bool:
        """Delete an API key."""
        if not self._client:
            return False
        try:
            self._client.remove_object(BUCKET_KEYS, f"keys/{key_id}.json")
            log.info("deleted API key: %s", key_id)
            return True
        except Exception as e:
            log.error("error deleting API key %s: %s", key_id, e)
            return False

    def get_analytics(self, date: str | None = None) -> dict:
        """Aggregate usage analytics from stored logs for a given date."""
        logs = self.list_request_logs(limit=10000, date=date)

        by_model: dict[str, dict] = {}
        by_path: dict[str, dict] = {}
        by_client: dict[str, dict] = {}
        by_status: dict[str, dict] = {}
        total_input = 0
        total_output = 0
        total_duration = 0.0
        total_requests = len(logs)

        for entry in logs:
            model = entry.get("model") or "unknown"
            path = entry.get("path", "unknown")
            client = entry.get("client_ip", "unknown")
            status = str(entry.get("status", 0))
            inp = entry.get("input_tokens", 0)
            out = entry.get("output_tokens", 0)
            dur = entry.get("duration_ms", 0)

            total_input += inp
            total_output += out
            total_duration += dur

            # By model
            if model not in by_model:
                by_model[model] = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "total_ms": 0}
            by_model[model]["requests"] += 1
            by_model[model]["input_tokens"] += inp
            by_model[model]["output_tokens"] += out
            by_model[model]["total_ms"] += dur

            # By path
            if path not in by_path:
                by_path[path] = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
            by_path[path]["requests"] += 1
            by_path[path]["input_tokens"] += inp
            by_path[path]["output_tokens"] += out

            # By client
            if client not in by_client:
                by_client[client] = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
            by_client[client]["requests"] += 1
            by_client[client]["input_tokens"] += inp
            by_client[client]["output_tokens"] += out

            # By status
            if status not in by_status:
                by_status[status] = {"requests": 0}
            by_status[status]["requests"] += 1

        return {
            "date": date or datetime.utcnow().strftime("%Y/%m/%d"),
            "total_requests": total_requests,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_tokens": total_input + total_output,
            "avg_duration_ms": round(total_duration / total_requests, 1) if total_requests else 0,
            "by_model": by_model,
            "by_path": by_path,
            "by_client": by_client,
            "by_status": by_status,
        }

    def get_storage_stats(self) -> dict:
        """Get storage usage statistics."""
        if not self._client:
            return {"available": False}

        stats = {
            "available": True,
            "endpoint": MINIO_ENDPOINT,
            "secure": MINIO_SECURE,
            "buckets": {},
        }
        for bucket in (BUCKET_LOGS, BUCKET_KEYS, BUCKET_CONFIG):
            total_size = 0
            total_objects = 0
            try:
                objects = self._client.list_objects(bucket, recursive=True)
                for obj in objects:
                    total_size += obj.size
                    total_objects += 1
            except Exception:
                pass
            stats["buckets"][bucket] = {
                "objects": total_objects,
                "size": total_size,
            }
        return stats


def _extract_api_info(data: dict) -> tuple:
    """Extract model name and token usage from a stored request/response log."""
    model = None
    input_tokens = 0
    output_tokens = 0

    # Model from request body
    req_body = data.get("request", {}).get("body", "")
    if req_body:
        try:
            parsed = json.loads(req_body) if isinstance(req_body, str) else req_body
            model = parsed.get("model")
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    # Token usage from response body — handle both regular JSON and SSE streams
    resp_body = data.get("response", {}).get("body", "")
    if resp_body and isinstance(resp_body, str):
        # Try direct JSON parse first (non-streaming response)
        try:
            parsed = json.loads(resp_body)
            usage = parsed.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            if not model and parsed.get("model"):
                model = parsed["model"]
        except (json.JSONDecodeError, TypeError, ValueError):
            # SSE streaming response — scan for usage in event chunks
            import re
            for line in resp_body.split("\n"):
                if not line.startswith("data: "):
                    continue
                chunk_str = line[6:].strip()
                if not chunk_str or chunk_str == "[DONE]":
                    continue
                try:
                    chunk = json.loads(chunk_str)
                    if not model and chunk.get("model"):
                        model = chunk["model"]
                    usage = chunk.get("usage")
                    if usage:
                        input_tokens = usage.get("input_tokens", input_tokens)
                        output_tokens = usage.get("output_tokens", output_tokens)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue

    return model, input_tokens, output_tokens


def _safe_decode(data: bytes) -> str:
    """Decode bytes to string, truncating large payloads."""
    if not data:
        return ""
    # Cap stored body at 512KB
    max_size = 512 * 1024
    truncated = len(data) > max_size
    chunk = data[:max_size]
    try:
        text = chunk.decode("utf-8")
        if truncated:
            text += f"\n\n… [truncated, total {len(data)} bytes]"
        return text
    except UnicodeDecodeError:
        import base64
        encoded = base64.b64encode(chunk).decode("ascii")
        if truncated:
            encoded += f"\n\n… [truncated, total {len(data)} bytes]"
        return f"[base64] {encoded}"
