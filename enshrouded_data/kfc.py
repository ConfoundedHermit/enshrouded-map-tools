"""Read KFC4 indexed content and decode reflected localization values."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import uuid


def guid_hash(value: str):
    """Compute 32-bit FNV-1a over the GUID's Windows byte layout."""
    digest = 0x811C9DC5
    for byte in uuid.UUID(value).bytes_le:
        digest = ((digest ^ byte) * 0x01000193) & 0xFFFFFFFF
    return digest


class KFCIndex:
    """Map content hashes to byte ranges in the installation's data archives."""

    def __init__(self, game: Path):
        self.game = game
        self.data = (game / "enshrouded.kfc").read_bytes()
        if self.data[:4] != b"KFC4":
            raise ValueError("Expected KFC4")
        self.contents = {}
        key_offset, count = self.location(72)
        value_offset, value_count = self.location(80)
        if count != value_count or max(key_offset, value_offset) + count * 16 > len(self.data):
            raise ValueError("Invalid content index bounds")
        for i in range(count):
            key = struct.unpack_from("<4I", self.data, key_offset + i * 16)
            offset, flags, container = struct.unpack_from("<IHH", self.data, value_offset + i * 16)
            if key in self.contents:
                raise ValueError("Duplicate content hash")
            self.contents[key] = (offset, flags, container)

    def location(self, offset):
        """Resolve a KFC relative offset and count from their header location."""
        relative, count = struct.unpack_from("<II", self.data, offset)
        target = offset + relative
        if target > len(self.data):
            raise ValueError("Invalid KFC location")
        return target, count

    @property
    def version(self):
        offset, count = self.location(16)
        return self.data[offset:offset + count].decode("utf-8")

    def content(self, reference):
        """Read one unflagged content payload and return its archive provenance."""
        key = tuple(reference[name] for name in ("size", "hash0", "hash1", "hash2"))
        offset, flags, container = self.contents[key]
        if flags:
            raise ValueError(f"Unsupported content flags: {flags}")
        path = self.game / f"enshrouded_{container:03}.dat"
        if offset + key[0] > path.stat().st_size:
            raise ValueError("Content exceeds container bounds")
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(key[0])
        if len(data) != key[0]:
            raise ValueError("Truncated content")
        return data, {"container": path.name, "offset": offset, "size_bytes": len(data),
                      "sha256": hashlib.sha256(data).hexdigest(), "content_hash": reference}


class ReflectedReader:
    """Decode localization values using extracted type layouts and bounds checks."""

    def __init__(self, reflection: Path):
        self.registry = json.loads(reflection.read_text(encoding="utf-8"))
        self.types = self.registry['types']
        self.by_name = {t['qualifiedName']: t for t in self.types}

    def read(self, data: bytes, name: str):
        return self._read(data, self.by_name[name], 0, 0)

    @staticmethod
    def _bounds(data, offset, size):
        if offset < 0 or size < 0 or offset + size > len(data):
            raise ValueError("Reflected value exceeds payload bounds")

    def _read(self, data, kind, offset, depth):
        if depth > 40:
            raise ValueError("Excessive reflection recursion")
        self._bounds(data, offset, kind['size'])
        primitive = kind['primitiveType']
        if primitive == 'STRUCT':
            result = {}
            # Decode base-struct fields before the derived fields at their offsets.
            if 'innerType' in kind:
                result.update(self._read(data, self.types[kind['innerType']], offset, depth + 1))
            for name, field in kind.get('structFields', {}).items():
                result[name] = self._read(data, self.types[field['type']], offset + field['dataOffset'], depth + 1)
            return result
        if primitive == 'TYPEDEF':
            return self._read(data, self.types[kind['innerType']], offset, depth + 1)
        if primitive in ('BLOB_ARRAY', 'BLOB_STRING'):
            relative, count = struct.unpack_from('<II', data, offset)
            if not relative:
                if count:
                    raise ValueError("Null blob pointer with nonzero count")
                return '' if primitive == 'BLOB_STRING' else []
            target = offset + relative
            if primitive == 'BLOB_STRING':
                self._bounds(data, target, count)
                return data[target:target + count].decode('utf-8')
            inner = self.types[kind['innerType']]
            alignment = inner['alignment']
            # Array elements occupy their size rounded up to the type's alignment.
            stride = (inner['size'] + alignment - 1) // alignment * alignment
            if not stride:
                raise ValueError("Zero-sized localization array element")
            self._bounds(data, target, stride * count)
            return [self._read(data, inner, target + i * stride, depth + 1) for i in range(count)]
        if primitive == 'ENUM':
            value = self._read(data, self.types[kind['innerType']], offset, depth + 1)
            return next((f['name'] for f in kind.get('enumFields', {}).values() if f['value'] == value), value)
        formats = {'UINT8':'B','SINT8':'b','UINT16':'H','SINT16':'h','UINT32':'I',
                   'SINT32':'i','UINT64':'Q','SINT64':'q','FLOAT32':'f','FLOAT64':'d','BOOL':'?'}
        if primitive in formats:
            return struct.unpack_from('<' + formats[primitive], data, offset)[0]
        raise ValueError(f"Unsupported localization primitive {primitive}: {kind['qualifiedName']}")


def load_localization(game: Path, descriptors: Path, reflection: Path, locale='En_Us'):
    """Decode one language's tags with matching reflection and content provenance."""
    paths = list((descriptors / 'LocaTagCollectionResource').glob('*.json'))
    if len(paths) != 1:
        raise ValueError("Expected one localization collection")
    collection = json.loads(paths[0].read_text(encoding='utf-8'))
    reference = next(row['data'] for row in collection['languages'] if row['language'] == locale)
    index = KFCIndex(game)
    reader = ReflectedReader(reflection)
    if reader.registry['version'] != index.version:
        raise ValueError("Reflection version does not match KFC version")
    data, provenance = index.content(reference['hash'])
    parsed = reader.read(data, 'keen::LocaTagCollectionResourceData')
    tags = {}
    for tag in parsed['tags']:
        key = tag['id']['value']
        if key in tags:
            raise ValueError(f"Duplicate localization ID {key}")
        tags[key] = tag
    return tags, {"locale": locale, "tag_count": len(tags), "collection_guid": collection['$guid'],
                  "kfc_version": index.version, **provenance}
