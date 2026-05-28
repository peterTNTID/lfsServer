"""
Minimal Git LFS server — GCS signed URLs, API key auth for writes.
Also serves as an IPFS HTTP Gateway, mapping IPFS CIDs to LFS objects in GCS.

Implements the Git LFS Batch API:
https://github.com/git-lfs/git-lfs/blob/main/docs/api/batch.md

IPFS Gateway:
  GET /ipfs/<cid>  — looks up CID → LFS OID, redirects to GCS signed URL

Flow:
  1. git-lfs POSTs to /objects/batch with a list of OIDs + operation
  2. For uploads: server checks API key, returns GCS signed upload URLs
  3. For downloads: no auth needed, returns GCS signed download URLs
  4. git-lfs uploads/downloads directly to/from GCS (server never touches file data)
  5. IPFS gateway: CID lookups redirect to GCS via the same signed URL mechanism
"""

import base64
import json
import logging
import os
import threading
from datetime import timedelta

from ipfs_cid import compute_cid_streaming

import google.auth
import google.auth.transport.requests
from flask import Flask, request, jsonify, redirect
from google.cloud import storage

app = Flask(__name__)

BUCKET_NAME = os.environ.get("GCS_BUCKET", "")
API_KEY = os.environ.get("LFS_WRITE_API_KEY", "")
SIGNED_URL_EXPIRY = timedelta(hours=1)
OBJECT_PREFIX = "lfs/objects/"
MANIFEST_PATH = "lfs/ipfs-manifest.json"  # CID→OID mapping stored in GCS

gcs_client = storage.Client()
bucket = gcs_client.bucket(BUCKET_NAME)

# On Cloud Run there's no private key — we use the IAM SignBlob API instead.
# This requires roles/iam.serviceAccountTokenCreator on the service account.
_credentials, _project = google.auth.default()
_auth_request = google.auth.transport.requests.Request()
_credentials.refresh(_auth_request)
_sa_email = _credentials.service_account_email


def _get_access_token() -> str:
    """Return a fresh access token, refreshing if needed."""
    if not _credentials.valid:
        _credentials.refresh(_auth_request)
    return _credentials.token


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


def _signed_url(blob, method: str, content_type: str | None = None) -> str:
    """Generate a V4 signed URL using the IAM SignBlob API."""
    kwargs = {
        "version": "v4",
        "expiration": SIGNED_URL_EXPIRY,
        "method": method,
        "service_account_email": _sa_email,
        "access_token": _get_access_token(),
    }
    if content_type:
        kwargs["content_type"] = content_type
    return blob.generate_signed_url(**kwargs)


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

            url = _signed_url(blob, "GET")
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

            url = _signed_url(blob, "PUT", content_type="application/octet-stream")

            # Build verify URL — respect X-Forwarded-Proto from Cloud Run's proxy
            scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
            verify_url = f"{scheme}://{request.host}/objects/verify"

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
            # Auto-compute IPFS CID in background
            threading.Thread(
                target=_auto_compute_cid, args=(oid, size),
                daemon=True,
            ).start()
            return "", 200

    return jsonify({"message": "Verification failed"}), 422


def _auto_compute_cid(oid: str, size: int) -> None:
    """Background task: compute CID for a newly uploaded object."""
    try:
        # Skip if already in manifest
        with _manifest_lock:
            already = any(info["oid"] == oid for info in _cid_to_oid.values())
        if already:
            return

        blob = bucket.blob(_object_path(oid))
        cid = compute_cid_streaming(blob, size)

        with _manifest_lock:
            _cid_to_oid[cid] = {"oid": oid, "size": size, "path": ""}

        _save_manifest()
        logging.info("Auto-computed CID for %s: %s", oid[:12], cid[:20])
    except Exception as e:
        logging.warning("Auto CID compute failed for %s: %s", oid[:12], e)


# =============================================================================
# IPFS Gateway — serve LFS objects by IPFS CID
# =============================================================================

# In-memory CID → OID lookup, loaded from GCS manifest
_cid_to_oid: dict[str, dict] = {}  # {cid: {"oid": str, "size": int, "path": str}}
_manifest_lock = threading.Lock()


def _load_manifest() -> None:
    """Load the CID→OID mapping from GCS into memory."""
    global _cid_to_oid
    blob = bucket.blob(MANIFEST_PATH)
    if not blob.exists():
        logging.info("No IPFS manifest found in GCS — gateway has no mappings.")
        return

    data = json.loads(blob.download_as_text())
    mapping = {}
    for entry in data:
        cid = entry.get("ipfs_cid", "")
        oid = entry.get("lfs_oid", "").removeprefix("sha256:")
        if cid and oid:
            mapping[cid] = {
                "oid": oid,
                "size": entry.get("size", 0),
                "path": entry.get("path", ""),
            }
    with _manifest_lock:
        _cid_to_oid = mapping
    logging.info("IPFS manifest loaded: %d CID→OID mappings.", len(mapping))


# Load manifest on startup (non-blocking — server starts even if manifest is missing)
try:
    _load_manifest()
except Exception as e:
    logging.warning("Failed to load IPFS manifest on startup: %s", e)


@app.route("/ipfs/<cid>", methods=["GET"])
def ipfs_gateway(cid: str):
    """
    IPFS HTTP Gateway — resolve a CID to a GCS signed URL.

    GET /ipfs/bafybei...
      → 302 redirect to GCS signed download URL

    This is not a full IPFS node — it's a stateless gateway that translates
    CID requests into GCS fetches via the LFS object store.
    """
    with _manifest_lock:
        entry = _cid_to_oid.get(cid)

    if not entry:
        return jsonify({
            "error": "CID not found",
            "cid": cid,
            "hint": "This gateway only serves CIDs listed in the IPFS manifest. "
                    "POST to /ipfs/manifest to update the mapping.",
        }), 404

    oid = entry["oid"]
    blob = bucket.blob(_object_path(oid))

    if not blob.exists():
        return jsonify({
            "error": "LFS object not found in storage",
            "cid": cid,
            "oid": oid,
        }), 404

    # Generate a signed download URL and redirect
    url = _signed_url(blob, "GET")
    return redirect(url, code=302)


@app.route("/ipfs/manifest", methods=["GET"])
def get_manifest():
    """
    Return the current CID→OID manifest.

    GET /ipfs/manifest
      → JSON array of {ipfs_cid, lfs_oid, size, path}
    """
    with _manifest_lock:
        entries = [
            {"ipfs_cid": cid, "lfs_oid": f"sha256:{info['oid']}",
             "size": info["size"], "path": info["path"]}
            for cid, info in _cid_to_oid.items()
        ]
    return jsonify(entries)


@app.route("/ipfs/manifest", methods=["POST"])
def update_manifest():
    """
    Upload/sync the CID→OID manifest.

    POST /ipfs/manifest
    Authorization: Basic (same API key as LFS uploads)
    Body: JSON array from .ipfs/manifest.jsonl (parsed as array)

    This stores the manifest in GCS and reloads the in-memory mapping.
    """
    if not _check_write_auth():
        return jsonify({"message": "Authentication required"}), 401

    data = request.get_json()
    if not isinstance(data, list):
        return jsonify({"error": "Expected a JSON array of manifest entries"}), 400

    # Store in GCS
    blob = bucket.blob(MANIFEST_PATH)
    blob.upload_from_string(
        json.dumps(data, separators=(",", ":")),
        content_type="application/json",
    )

    # Reload in-memory mapping
    _load_manifest()

    with _manifest_lock:
        count = len(_cid_to_oid)

    return jsonify({"status": "ok", "entries": count})



@app.route("/ipfs/reindex", methods=["POST"])
def reindex():
    """
    Scan all LFS objects in GCS and compute IPFS CIDs server-side.

    POST /ipfs/reindex
    Authorization: Basic (same API key as LFS uploads)

    This finds objects not yet in the manifest, downloads each one,
    computes its CID using the same algorithm as `ipfs add`, and
    updates the manifest. No local IPFS node is needed.
    """
    if not _check_write_auth():
        return jsonify({"message": "Authentication required"}), 401

    # Collect OIDs already in the manifest
    with _manifest_lock:
        known_oids = {info["oid"] for info in _cid_to_oid.values()}

    # Scan GCS for all LFS objects
    prefix = OBJECT_PREFIX
    new_entries = 0
    errors = 0

    for blob in bucket.list_blobs(prefix=prefix):
        # Path format: lfs/objects/xx/yy/<oid>
        parts = blob.name.split("/")
        if len(parts) != 5:
            continue
        oid = parts[4]

        if oid in known_oids:
            continue

        try:
            blob.reload()
            cid = compute_cid_streaming(blob, blob.size)

            with _manifest_lock:
                _cid_to_oid[cid] = {
                    "oid": oid,
                    "size": blob.size,
                    "path": "",
                }
            new_entries += 1
            logging.info("Reindex: %s -> %s", oid[:12], cid[:20])
        except Exception as e:
            errors += 1
            logging.warning("Reindex failed for %s: %s", oid[:12], e)

    # Save updated manifest to GCS
    if new_entries > 0:
        _save_manifest()

    with _manifest_lock:
        total = len(_cid_to_oid)

    return jsonify({
        "status": "ok",
        "new_entries": new_entries,
        "errors": errors,
        "total_entries": total,
    })


def _save_manifest() -> None:
    """Persist the in-memory CID→OID mapping to GCS."""
    with _manifest_lock:
        data = [
            {
                "ipfs_cid": cid,
                "lfs_oid": f"sha256:{info['oid']}",
                "size": info["size"],
                "path": info["path"],
            }
            for cid, info in _cid_to_oid.items()
        ]
    blob = bucket.blob(MANIFEST_PATH)
    blob.upload_from_string(
        json.dumps(data, separators=(",", ":")),
        content_type="application/json",
    )
    logging.info("Manifest saved to GCS: %d entries.", len(data))


@app.route("/", methods=["GET"])
def health():
    """Health check endpoint."""
    with _manifest_lock:
        cid_count = len(_cid_to_oid)
    return jsonify({
        "status": "ok",
        "service": "git-lfs-gcs",
        "ipfs_gateway": {
            "enabled": True,
            "cid_count": cid_count,
            "endpoint": "/ipfs/<cid>",
        },
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
