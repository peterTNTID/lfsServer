"""
Minimal Git LFS server — GCS signed URLs, API key auth for writes.

Implements the Git LFS Batch API:
https://github.com/git-lfs/git-lfs/blob/main/docs/api/batch.md

Flow:
  1. git-lfs POSTs to /objects/batch with a list of OIDs + operation
  2. For uploads: server checks API key, returns GCS signed upload URLs
  3. For downloads: no auth needed, returns GCS signed download URLs
  4. git-lfs uploads/downloads directly to/from GCS (server never touches file data)
"""

import base64
import os
from datetime import timedelta

from flask import Flask, request, jsonify
from google.cloud import storage

app = Flask(__name__)

BUCKET_NAME = os.environ.get("GCS_BUCKET", "")
API_KEY = os.environ.get("LFS_WRITE_API_KEY", "")
SIGNED_URL_EXPIRY = timedelta(hours=1)
OBJECT_PREFIX = "lfs/objects/"

gcs_client = storage.Client()
bucket = gcs_client.bucket(BUCKET_NAME)


def _check_write_auth() -> bool:
    """Check HTTP Basic auth — API key is the password, username is ignored."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        _, password = decoded.split(":", 1)
        return password == API_KEY
    except (ValueError, UnicodeDecodeError):
        return False


def _object_path(oid: str) -> str:
    """OID-based path with fan-out: lfs/objects/ab/cd/abcdef123456..."""
    return f"{OBJECT_PREFIX}{oid[:2]}/{oid[2:4]}/{oid}"


@app.route("/objects/batch", methods=["POST"])
def batch():
    """Git LFS Batch API endpoint."""
    data = request.get_json()
    operation = data.get("operation")  # "upload" or "download"
    objects = data.get("objects", [])

    # Uploads require authentication
    if operation == "upload" and not _check_write_auth():
        return jsonify({
            "message": "Authentication required for uploads",
        }), 401

    response_objects = []
    for obj in objects:
        oid = obj["oid"]
        size = obj["size"]
        blob = bucket.blob(_object_path(oid))

        if operation == "download":
            if not blob.exists():
                response_objects.append({
                    "oid": oid,
                    "size": size,
                    "error": {"code": 404, "message": "Object not found"},
                })
                continue

            url = blob.generate_signed_url(
                version="v4",
                expiration=SIGNED_URL_EXPIRY,
                method="GET",
            )
            response_objects.append({
                "oid": oid,
                "size": size,
                "authenticated": True,
                "actions": {
                    "download": {
                        "href": url,
                        "expires_in": 3600,
                    }
                },
            })

        elif operation == "upload":
            # Skip if object already exists with correct size
            if blob.exists():
                blob.reload()
                if blob.size == size:
                    response_objects.append({
                        "oid": oid,
                        "size": size,
                        "authenticated": True,
                    })
                    continue

            url = blob.generate_signed_url(
                version="v4",
                expiration=SIGNED_URL_EXPIRY,
                method="PUT",
                content_type="application/octet-stream",
            )

            verify_url = f"{request.url_root.rstrip('/')}/objects/verify"

            response_objects.append({
                "oid": oid,
                "size": size,
                "authenticated": True,
                "actions": {
                    "upload": {
                        "href": url,
                        "header": {"Content-Type": "application/octet-stream"},
                        "expires_in": 3600,
                    },
                    "verify": {
                        "href": verify_url,
                        "header": {
                            "Authorization": request.headers.get("Authorization", ""),
                        },
                    },
                },
            })

    return jsonify({
        "transfer": "basic",
        "objects": response_objects,
    }), 200, {"Content-Type": "application/vnd.git-lfs+json"}


@app.route("/objects/verify", methods=["POST"])
def verify():
    """Verify an uploaded object exists in GCS with the expected size."""
    if not _check_write_auth():
        return jsonify({"message": "Unauthorized"}), 401

    data = request.get_json()
    oid = data.get("oid", "")
    size = data.get("size", 0)

    blob = bucket.blob(_object_path(oid))
    if blob.exists():
        blob.reload()
        if blob.size == size:
            return "", 200

    return jsonify({"message": "Verification failed"}), 422


@app.route("/", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok", "service": "git-lfs-gcs"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
