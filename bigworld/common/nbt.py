"""Minimaler NBT-Leser und -Schreiber (Java Edition, Big Endian, unkomprimiert).

Der Leser kann
- einen ganzen Tag-Baum in Python-Strukturen umwandeln (für den Chunk-Vergleich),
- schnell nur ein einzelnes Feld der Wurzel lesen, ohne den Rest aufzubauen
  (für die Prüfung "ist der Chunk vollständig generiert?").

Der Schreiber wird nur für Tests und den simulierten Generator gebraucht.
"""

from __future__ import annotations

import struct

TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11
TAG_LONG_ARRAY = 12


class NBTError(ValueError):
    pass


class _Reader:
    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes, pos: int = 0):
        self.buf = buf
        self.pos = pos

    def take(self, n: int) -> bytes:
        end = self.pos + n
        if end > len(self.buf):
            raise NBTError("NBT-Daten unerwartet zu Ende")
        out = self.buf[self.pos : end]
        self.pos = end
        return out

    def u8(self) -> int:
        if self.pos >= len(self.buf):
            raise NBTError("NBT-Daten unerwartet zu Ende")
        v = self.buf[self.pos]
        self.pos += 1
        return v

    def i32(self) -> int:
        return struct.unpack(">i", self.take(4))[0]

    def u16(self) -> int:
        return struct.unpack(">H", self.take(2))[0]

    def string(self) -> str:
        n = self.u16()
        return self.take(n).decode("utf-8", errors="surrogateescape")

    def skip_string(self) -> None:
        n = self.u16()
        self.take(n)


_FIXED = {TAG_BYTE: 1, TAG_SHORT: 2, TAG_INT: 4, TAG_LONG: 8, TAG_FLOAT: 4, TAG_DOUBLE: 8}
_FMT = {TAG_BYTE: ">b", TAG_SHORT: ">h", TAG_INT: ">i", TAG_LONG: ">q", TAG_FLOAT: ">f", TAG_DOUBLE: ">d"}


def _read_payload(r: _Reader, tag: int, depth: int = 0):
    if depth > 512:
        raise NBTError("NBT zu tief verschachtelt")
    if tag in _FMT:
        return struct.unpack(_FMT[tag], r.take(_FIXED[tag]))[0]
    if tag == TAG_BYTE_ARRAY:
        n = r.i32()
        return ("B", r.take(n))
    if tag == TAG_STRING:
        return r.string()
    if tag == TAG_LIST:
        inner = r.u8()
        n = r.i32()
        return [_read_payload(r, inner, depth + 1) for _ in range(max(n, 0))]
    if tag == TAG_COMPOUND:
        out = {}
        while True:
            t = r.u8()
            if t == TAG_END:
                return out
            name = r.string()
            out[name] = _read_payload(r, t, depth + 1)
    if tag == TAG_INT_ARRAY:
        n = r.i32()
        return ("I", r.take(4 * n))
    if tag == TAG_LONG_ARRAY:
        n = r.i32()
        return ("L", r.take(8 * n))
    raise NBTError(f"Unbekannter NBT-Tag {tag}")


def _skip_payload(r: _Reader, tag: int, depth: int = 0) -> None:
    if depth > 512:
        raise NBTError("NBT zu tief verschachtelt")
    if tag in _FIXED:
        r.take(_FIXED[tag])
    elif tag == TAG_BYTE_ARRAY:
        r.take(r.i32())
    elif tag == TAG_STRING:
        r.skip_string()
    elif tag == TAG_LIST:
        inner = r.u8()
        n = r.i32()
        if inner in _FIXED:
            r.take(_FIXED[inner] * max(n, 0))
        else:
            for _ in range(max(n, 0)):
                _skip_payload(r, inner, depth + 1)
    elif tag == TAG_COMPOUND:
        while True:
            t = r.u8()
            if t == TAG_END:
                return
            r.skip_string()
            _skip_payload(r, t, depth + 1)
    elif tag == TAG_INT_ARRAY:
        r.take(4 * r.i32())
    elif tag == TAG_LONG_ARRAY:
        r.take(8 * r.i32())
    else:
        raise NBTError(f"Unbekannter NBT-Tag {tag}")


def _root(r: _Reader) -> None:
    t = r.u8()
    if t != TAG_COMPOUND:
        raise NBTError("Wurzel ist kein Compound")
    r.skip_string()  # Name der Wurzel (meist leer)


def parse(buf: bytes) -> dict:
    """Ganzen Baum lesen. Arrays werden als (Typ, Rohbytes) zurückgegeben."""
    r = _Reader(buf)
    _root(r)
    return _read_payload(r, TAG_COMPOUND)


def read_root_field(buf: bytes, name: str, nested_in: str | None = None):
    """Ein einzelnes Feld der Wurzel lesen (optional innerhalb eines Unter-Compounds).

    Alles andere wird nur übersprungen, nicht aufgebaut. Gibt None zurück,
    wenn das Feld fehlt.
    """
    r = _Reader(buf)
    _root(r)
    return _find_in_compound(r, name, nested_in)


def _find_in_compound(r: _Reader, name: str, nested_in: str | None):
    while True:
        t = r.u8()
        if t == TAG_END:
            return None
        key = r.string()
        if nested_in is not None and key == nested_in and t == TAG_COMPOUND:
            return _find_in_compound(r, name, None)
        if nested_in is None and key == name:
            return _read_payload(r, t)
        _skip_payload(r, t)


# --------------------------------------------------------------------------
# Schreiber (für Tests und simulierten Generator)
# --------------------------------------------------------------------------


class Long(int):
    """Markiert eine Ganzzahl, die als TAG_Long geschrieben werden soll."""


class IntArray(list):
    pass


class LongArray(list):
    pass


def _tag_of(v) -> int:
    if isinstance(v, bool):
        return TAG_BYTE
    if isinstance(v, Long):
        return TAG_LONG
    if isinstance(v, int):
        return TAG_INT if -(2**31) <= v < 2**31 else TAG_LONG
    if isinstance(v, float):
        return TAG_DOUBLE
    if isinstance(v, str):
        return TAG_STRING
    if isinstance(v, IntArray):
        return TAG_INT_ARRAY
    if isinstance(v, LongArray):
        return TAG_LONG_ARRAY
    if isinstance(v, (bytes, bytearray)):
        return TAG_BYTE_ARRAY
    if isinstance(v, dict):
        return TAG_COMPOUND
    if isinstance(v, list):
        return TAG_LIST
    raise TypeError(f"Nicht als NBT schreibbar: {type(v)!r}")


def _write_string(out: bytearray, s: str) -> None:
    b = s.encode("utf-8")
    out += struct.pack(">H", len(b))
    out += b


def _write_payload(out: bytearray, v, tag: int) -> None:
    if tag == TAG_BYTE:
        out += struct.pack(">b", int(v))
    elif tag == TAG_INT:
        out += struct.pack(">i", v)
    elif tag == TAG_LONG:
        out += struct.pack(">q", v)
    elif tag == TAG_DOUBLE:
        out += struct.pack(">d", v)
    elif tag == TAG_STRING:
        _write_string(out, v)
    elif tag == TAG_BYTE_ARRAY:
        out += struct.pack(">i", len(v))
        out += bytes(v)
    elif tag == TAG_INT_ARRAY:
        out += struct.pack(">i", len(v))
        for x in v:
            out += struct.pack(">i", x)
    elif tag == TAG_LONG_ARRAY:
        out += struct.pack(">i", len(v))
        for x in v:
            out += struct.pack(">q", x if x < 2**63 else x - 2**64)
    elif tag == TAG_COMPOUND:
        for k, x in v.items():
            t = _tag_of(x)
            out.append(t)
            _write_string(out, k)
            _write_payload(out, x, t)
        out.append(TAG_END)
    elif tag == TAG_LIST:
        inner = _tag_of(v[0]) if v else TAG_END
        out.append(inner)
        out += struct.pack(">i", len(v))
        for x in v:
            _write_payload(out, x, inner)
    else:  # pragma: no cover
        raise TypeError(tag)


def dumps(root: dict, name: str = "") -> bytes:
    out = bytearray()
    out.append(TAG_COMPOUND)
    _write_string(out, name)
    _write_payload(out, root, TAG_COMPOUND)
    return bytes(out)
