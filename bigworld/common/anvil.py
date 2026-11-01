"""Lesen (und für Tests: Schreiben) von Minecraft-Regionsdateien (*.mca).

Aufbau einer Regionsdatei:
- 4 KiB Positionstabelle: pro Chunk 3 Byte Sektor-Offset + 1 Byte Sektoranzahl
- 4 KiB Zeitstempel
- Chunkdaten in 4-KiB-Sektoren: 4 Byte Länge, 1 Byte Kompression, Daten

Ist im Kompressions-Byte das Bit 0x80 gesetzt, liegen die Daten in einer
externen Datei c.<cx>.<cz>.mcc neben der Regionsdatei (Chunks > 1 MiB).
Diese Dateien müssen beim Packen unbedingt mitgenommen werden.
"""

from __future__ import annotations

import gzip
import os
import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from . import nbt

SECTOR = 4096
COMP_GZIP = 1
COMP_ZLIB = 2
COMP_NONE = 3
COMP_LZ4 = 4
EXTERNAL_FLAG = 0x80

FULL_STATUSES = {"minecraft:full", "full"}

_NAME_RE = re.compile(r"^(r|c)\.(-?\d+)\.(-?\d+)\.(mca|mcc)$")


class RegionError(ValueError):
    pass


def parse_name(name: str) -> tuple[str, int, int, str] | None:
    """'r.-1.3.mca' -> ('r', -1, 3, 'mca'); None bei fremden Dateinamen."""
    m = _NAME_RE.match(name)
    if not m:
        return None
    kind, x, z, ext = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
    if (kind == "r") != (ext == "mca"):
        return None
    return kind, x, z, ext


def chunk_index(cx: int, cz: int) -> int:
    return (cx & 31) + (cz & 31) * 32


def decompress(comp: int, payload: bytes) -> bytes:
    if comp == COMP_ZLIB:
        return zlib.decompress(payload)
    if comp == COMP_GZIP:
        return gzip.decompress(payload)
    if comp == COMP_NONE:
        return payload
    if comp == COMP_LZ4:
        raise RegionError(
            "Chunk ist LZ4-komprimiert. Bitte region-file-compression=deflate "
            "in server.properties setzen."
        )
    raise RegionError(f"Unbekannte Kompression {comp}")


class RegionFile:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        parsed = parse_name(self.path.name)
        if not parsed or parsed[0] != "r":
            raise RegionError(f"Kein Regionsdateiname: {self.path.name}")
        _, self.rx, self.rz, _ = parsed
        self.data = self.path.read_bytes()
        if len(self.data) < 2 * SECTOR and len(self.data) != 0:
            raise RegionError(f"{self.path.name}: Datei zu kurz ({len(self.data)} Byte)")

    def location(self, index: int) -> tuple[int, int]:
        if len(self.data) == 0:
            return 0, 0
        entry = self.data[index * 4 : index * 4 + 4]
        offset = int.from_bytes(entry[:3], "big")
        count = entry[3]
        return offset, count

    def has_chunk(self, index: int) -> bool:
        offset, count = self.location(index)
        return offset >= 2 and count > 0

    def raw_chunk(self, index: int) -> tuple[int, bytes] | None:
        """(Kompression, komprimierte Daten) oder None, wenn der Chunk fehlt."""
        offset, count = self.location(index)
        if offset < 2 or count == 0:
            return None
        start = offset * SECTOR
        if start + 5 > len(self.data):
            raise RegionError(f"{self.path.name}: Chunk {index} zeigt hinter das Dateiende")
        length = struct.unpack(">i", self.data[start : start + 4])[0]
        comp = self.data[start + 4]
        if comp & EXTERNAL_FLAG:
            cx = self.rx * 32 + (index % 32)
            cz = self.rz * 32 + (index // 32)
            ext = self.path.parent / f"c.{cx}.{cz}.mcc"
            if not ext.exists():
                raise RegionError(f"{self.path.name}: externe Chunkdatei {ext.name} fehlt")
            return comp & ~EXTERNAL_FLAG, ext.read_bytes()
        if length <= 0 or start + 4 + length > len(self.data):
            raise RegionError(f"{self.path.name}: Chunk {index} hat ungültige Länge {length}")
        return comp, self.data[start + 5 : start + 4 + length]

    def chunk_bytes(self, index: int) -> bytes | None:
        raw = self.raw_chunk(index)
        if raw is None:
            return None
        return decompress(*raw)

    def timestamp(self, index: int) -> int:
        if len(self.data) < 2 * SECTOR:
            return 0
        return struct.unpack(">i", self.data[SECTOR + index * 4 : SECTOR + index * 4 + 4])[0]

    def blob(self, index: int) -> bytes | None:
        """Chunk-Eintrag unverändert (4 Byte Länge, Kompressionsbyte, Daten); None, wenn er fehlt.

        Bei externen Chunks (Flag 0x80) ist das nur der kurze Verweis, die Daten liegen in c.X.Z.mcc.
        """
        offset, count = self.location(index)
        if offset < 2 or count == 0:
            return None
        start = offset * SECTOR
        if start + 5 > len(self.data):
            raise RegionError(f"{self.path.name}: Chunk {index} zeigt hinter das Dateiende")
        length = struct.unpack(">i", self.data[start : start + 4])[0]
        if length <= 0 or start + 4 + length > len(self.data):
            raise RegionError(f"{self.path.name}: Chunk {index} hat ungültige Länge {length}")
        return self.data[start : start + 4 + length]

    def is_external(self, index: int) -> bool:
        b = self.blob(index)
        return bool(b and b[4] & EXTERNAL_FLAG)

    def external_files(self) -> list[Path]:
        """Alle c.*.mcc-Dateien, die zu dieser Region gehören."""
        out = []
        for index in range(1024):
            offset, count = self.location(index)
            if offset < 2 or count == 0:
                continue
            start = offset * SECTOR
            if start + 5 <= len(self.data) and self.data[start + 4] & EXTERNAL_FLAG:
                cx = self.rx * 32 + (index % 32)
                cz = self.rz * 32 + (index // 32)
                out.append(self.path.parent / f"c.{cx}.{cz}.mcc")
        return out


def chunk_status(chunk_nbt: bytes) -> str | None:
    status = nbt.read_root_field(chunk_nbt, "Status")
    if status is None:  # altes Format mit "Level"-Wrapper
        status = nbt.read_root_field(chunk_nbt, "Status", nested_in="Level")
    return status if isinstance(status, str) else None


@dataclass
class RegionCheck:
    path: str
    total: int = 0
    full: int = 0
    missing: list[int] = field(default_factory=list)
    not_full: list[tuple[int, str | None]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.not_full and not self.errors


def check_region_complete(path: str | os.PathLike) -> RegionCheck:
    """Prüft, ob alle 1024 Chunks der Region vorhanden und vollständig generiert sind."""
    result = RegionCheck(str(path))
    try:
        region = RegionFile(path)
    except (OSError, RegionError) as e:
        result.errors.append(str(e))
        return result
    for index in range(1024):
        result.total += 1
        try:
            data = region.chunk_bytes(index)
        except (RegionError, zlib.error, OSError, EOFError) as e:
            result.errors.append(f"Chunk {index}: {e}")
            continue
        if data is None:
            result.missing.append(index)
            continue
        try:
            status = chunk_status(data)
        except nbt.NBTError as e:
            result.errors.append(f"Chunk {index}: {e}")
            continue
        if status in FULL_STATUSES:
            result.full += 1
        else:
            result.not_full.append((index, status))
    return result


def write_region_blobs(path: str | os.PathLike, entries: dict[int, tuple[bytes, int]]) -> None:
    """Regionsdatei aus unveränderten Chunk-Einträgen {Index: (blob, Zeitstempel)} schreiben.

    Schreibt erst in eine temporäre Datei und ersetzt dann atomar.
    """
    header = bytearray(2 * SECTOR)
    body = bytearray()
    sector = 2
    for index in sorted(entries):
        blob, ts = entries[index]
        pad = (-len(blob)) % SECTOR
        count = (len(blob) + pad) // SECTOR
        if count > 255:
            raise RegionError(f"Chunk {index} ist zu groß für einen Regionseintrag ({count} Sektoren)")
        header[index * 4 : index * 4 + 3] = sector.to_bytes(3, "big")
        header[index * 4 + 3] = count
        header[SECTOR + index * 4 : SECTOR + index * 4 + 4] = struct.pack(">i", ts)
        body += blob + b"\0" * pad
        sector += count
    path = Path(path)
    tmp = path.with_name(path.name + ".bwtmp")
    with open(tmp, "wb") as f:
        f.write(bytes(header) + bytes(body))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_region(path: str | os.PathLike, chunks: dict[int, bytes], comp: int = COMP_ZLIB,
                 timestamp: int = 0) -> None:
    """Schreibt eine Regionsdatei aus {Index: unkomprimierte NBT-Bytes}. Nur für Tests."""
    header = bytearray(2 * SECTOR)
    body = bytearray()
    sector = 2
    for index in sorted(chunks):
        raw = chunks[index]
        if comp == COMP_ZLIB:
            payload = zlib.compress(raw)
        elif comp == COMP_NONE:
            payload = raw
        else:
            raise ValueError("nur zlib/none unterstützt")
        blob = struct.pack(">iB", len(payload) + 1, comp) + payload
        pad = (-len(blob)) % SECTOR
        blob += b"\0" * pad
        count = len(blob) // SECTOR
        header[index * 4 : index * 4 + 3] = sector.to_bytes(3, "big")
        header[index * 4 + 3] = count
        header[SECTOR + index * 4 : SECTOR + index * 4 + 4] = struct.pack(">i", timestamp)
        body += blob
        sector += count
    Path(path).write_bytes(bytes(header) + bytes(body))
