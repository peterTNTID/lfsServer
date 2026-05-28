"""
Pure Python IPFS CID computation.

Produces CIDv1 (base32lower) identical to:
    ipfs add --cid-version=1 --raw-leaves --chunker=size-262144 <file>

Handles:
  - Single-chunk files (≤ 256 KiB): CIDv1 raw codec (bafkrei...)
  - Multi-chunk files (> 256 KiB):  CIDv1 dag-pb codec (bafybei...)
    with balanced Merkle DAG and UnixFS file nodes.

No external dependencies — uses only hashlib, base64, struct.
"""

import base64
import hashlib

# IPFS defaults
CHUNK_SIZE = 262144   # 256 KiB
MAX_CHILDREN = 174    # max links per DAG node

# Codec identifiers
RAW_CODEC = 0x55
DAG_PB_CODEC = 0x70

# Hash identifiers
SHA2_256 = 0x12
SHA2_256_LEN = 0x20  # 32 bytes


# =============================================================================
# Public API
# =============================================================================

def compute_cid(data: bytes) -> str:
    """
    Compute the IPFS CIDv1 (base32lower) for file content.

    Returns a string like 'bafkrei...' (raw, single-chunk) or
    'bafybei...' (dag-pb, multi-chunk).
    """
    if len(data) <= CHUNK_SIZE:
        cid_bytes = _raw_cid_bytes(data)
        return _to_base32(cid_bytes)

    # Multi-chunk: split and build balanced DAG
    chunks = [data[i:i + CHUNK_SIZE] for i in range(0, len(data), CHUNK_SIZE)]
    root = _build_dag(chunks)
    return _to_base32(root["cid"])


def compute_cid_streaming(blob, file_size: int) -> str:
    """
    Compute CID by streaming a GCS blob in chunks.
    Avoids loading the entire file into memory at once.
    """
    if file_size <= CHUNK_SIZE:
        data = blob.download_as_bytes()
        return _to_base32(_raw_cid_bytes(data))

    # Download in CHUNK_SIZE pieces, compute leaf CIDs on the fly
    leaves = []
    offset = 0
    while offset < file_size:
        end = min(offset + CHUNK_SIZE, file_size)
        chunk = blob.download_as_bytes(start=offset, end=end - 1)
        cid_bytes = _raw_cid_bytes(chunk)
        leaves.append({
            "cid": cid_bytes,
            "data_size": len(chunk),
            "tsize": len(chunk),  # raw leaf tsize = raw byte count
        })
        offset = end

    root = _build_tree(leaves)
    return _to_base32(root["cid"])


# =============================================================================
# CID encoding
# =============================================================================

def _raw_cid_bytes(data: bytes) -> bytes:
    """CIDv1 bytes for raw content (single chunk)."""
    digest = hashlib.sha256(data).digest()
    multihash = bytes([SHA2_256, SHA2_256_LEN]) + digest
    return bytes([0x01, RAW_CODEC]) + multihash


def _dagpb_cid_bytes(node_bytes: bytes) -> bytes:
    """CIDv1 bytes for a dag-pb node."""
    digest = hashlib.sha256(node_bytes).digest()
    multihash = bytes([SHA2_256, SHA2_256_LEN]) + digest
    return bytes([0x01, DAG_PB_CODEC]) + multihash


def _to_base32(cid_bytes: bytes) -> str:
    """Encode CID bytes as base32lower multibase string."""
    encoded = base64.b32encode(cid_bytes).decode("ascii").lower().rstrip("=")
    return "b" + encoded


# =============================================================================
# Balanced Merkle DAG builder
# =============================================================================

def _build_dag(chunks: list[bytes]) -> dict:
    """Build a balanced Merkle DAG from file chunks."""
    # Create leaf nodes
    leaves = []
    for chunk in chunks:
        cid_bytes = _raw_cid_bytes(chunk)
        leaves.append({
            "cid": cid_bytes,
            "data_size": len(chunk),
            "tsize": len(chunk),
        })
    return _build_tree(leaves)


def _build_tree(leaves: list[dict]) -> dict:
    """Build tree bottom-up from leaf nodes, max MAX_CHILDREN per node."""
    level = leaves
    while len(level) > 1:
        next_level = []
        for i in range(0, len(level), MAX_CHILDREN):
            batch = level[i:i + MAX_CHILDREN]
            if len(batch) == 1 and len(level) > 1:
                # Single child at end — still wrap in a node for correctness,
                # but only if there are other nodes at this level
                next_level.append(_make_intermediate(batch))
            elif len(batch) == 1:
                next_level.append(batch[0])
            else:
                next_level.append(_make_intermediate(batch))
        level = next_level
    return level[0]


def _make_intermediate(children: list[dict]) -> dict:
    """Create an intermediate dag-pb node linking to children."""
    total_data = sum(c["data_size"] for c in children)
    block_sizes = [c["data_size"] for c in children]

    # Encode the UnixFS file data protobuf
    unixfs_data = _encode_unixfs_file(total_data, block_sizes)

    # Encode PBLinks
    pb_links = []
    for child in children:
        pb_links.append(_encode_pb_link(child["cid"], child["tsize"]))

    # Encode PBNode (canonical order: Links first, then Data)
    node_bytes = _encode_pb_node(pb_links, unixfs_data)

    cid_bytes = _dagpb_cid_bytes(node_bytes)
    tsize = len(node_bytes) + sum(c["tsize"] for c in children)

    return {
        "cid": cid_bytes,
        "data_size": total_data,
        "tsize": tsize,
    }


# =============================================================================
# Protobuf encoding (minimal, hand-rolled — no dependency)
# =============================================================================

def _varint(n: int) -> bytes:
    """Encode an unsigned integer as a protobuf varint."""
    buf = []
    while n > 0x7F:
        buf.append((n & 0x7F) | 0x80)
        n >>= 7
    buf.append(n & 0x7F)
    return bytes(buf)


def _pb_field_varint(field: int, value: int) -> bytes:
    """Protobuf: encode a varint field (wire type 0)."""
    tag = (field << 3) | 0
    return _varint(tag) + _varint(value)


def _pb_field_bytes(field: int, data: bytes) -> bytes:
    """Protobuf: encode a length-delimited field (wire type 2)."""
    tag = (field << 3) | 2
    return _varint(tag) + _varint(len(data)) + data


def _encode_unixfs_file(filesize: int, blocksizes: list[int]) -> bytes:
    """
    Encode a UnixFS Data protobuf for a file node.

    message Data {
        required DataType Type = 1;   // 2 = File
        optional uint64 filesize = 3;
        repeated uint64 blocksizes = 4;
    }
    """
    buf = _pb_field_varint(1, 2)          # Type = File
    buf += _pb_field_varint(3, filesize)   # filesize
    for bs in blocksizes:
        buf += _pb_field_varint(4, bs)     # blocksizes (repeated)
    return buf


def _encode_pb_link(cid_bytes: bytes, tsize: int) -> bytes:
    """
    Encode a PBLink protobuf.

    message PBLink {
        optional bytes Hash = 1;    // CID bytes
        optional string Name = 2;   // empty string for file chunks
        optional uint64 Tsize = 3;
    }
    """
    buf = _pb_field_bytes(1, cid_bytes)   # Hash
    buf += _pb_field_bytes(2, b"")        # Name = "" (required by go-ipfs)
    buf += _pb_field_varint(3, tsize)      # Tsize
    return buf


def _encode_pb_node(links: list[bytes], data: bytes) -> bytes:
    """
    Encode a PBNode protobuf in canonical dag-pb order.

    Canonical encoding: Links (field 2) first, then Data (field 1).
    This is required for content addressing — the hash depends on
    exact byte order.

    message PBNode {
        optional bytes Data = 1;
        repeated PBLink Links = 2;
    }
    """
    buf = b""
    for link in links:
        buf += _pb_field_bytes(2, link)   # Links (field 2, repeated)
    buf += _pb_field_bytes(1, data)       # Data (field 1)
    return buf
