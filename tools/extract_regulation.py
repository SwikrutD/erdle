#!/usr/bin/env python3
"""Unpack Elden Ring's regulation.bin into its PARAM files.

    python tools/extract_regulation.py regulation.bin --out params/
    python tools/extract_regulation.py regulation.bin --list
    python tools/extract_regulation.py regulation.bin --rows NpcParam

The game's own numbers beat any wiki. `NpcParam` holds every enemy's
damage negation, status resistance and poise, which is exactly what
`data/bosses.json` is trying to describe -- second-hand, coarsened to four
buckets, and wrong in places.

Anti-cheat note: this reads a *copy* of the file. Nothing here writes to
the game directory, and the game is not running. Reading is what tools
like Smithbox and DSMapStudio do; it is modifying regulation.bin in place
that gets people banned.

Format, outermost first:

    AES-CBC        16-byte IV, then the body. Key below, public since 2022.
    DCX            FromSoftware's container. Newer patches use ZSTD; older
                   ones used zlib, and both are handled.
    BND4           An archive of ~194 .param files.
    PARAM          Row table. Reading the *values* needs a field layout
                   (a "paramdef"), which is not in the file -- see below.

Field offsets
-------------
A PARAM is rows with no field names, so the offsets below came from the
Paramdex definition (`ER/Defs/NpcParam.xml` -- note the name, not
`NPC_PARAM_ST.xml`) by walking every field and accumulating sizes. Two
things make that walk easy to get wrong:

* `dummy8` is padding but packs into the same bit container as `u8`.
  Treating the literal type name as the container identity closed the
  very first container early and shifted every later field by one byte.
* The result must total 736, which is the row size measured from the
  file itself. That check is the whole safety net: a paramdef applied at
  the wrong offset still yields confident, plausible, wrong numbers.

The values were then sanity-checked against things any player knows --
the Elden Beast shrugging off holy, tree spirits burning, Crystalians
immune to bleed -- before any of it reached `data/bosses.json`.

Names still are not here: they live in the game's message archives. Row
IDs follow the character ID, so Godrick (c4750) is row 47500014.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

#: Published in SoulsFormats. Not a secret, and not a circumvention: the
#: file is encrypted so that patches can ship as opaque blobs, not to keep
#: anyone out.
REGULATION_KEY = bytes([
    0x99, 0xBF, 0xFC, 0x36, 0x6A, 0x6B, 0xC8, 0xC6,
    0xF5, 0x82, 0x7D, 0x09, 0x36, 0x02, 0xD6, 0x76,
    0xC4, 0x28, 0x92, 0xA0, 0x1C, 0x20, 0x7F, 0xB0,
    0x24, 0xD3, 0xAF, 0x4E, 0x49, 0x3F, 0xEF, 0x99,
])


def decrypt(raw: bytes) -> bytes:
    """Strip the AES-CBC layer. The IV is the first sixteen bytes."""
    try:
        from cryptography.hazmat.primitives.ciphers import (
            Cipher, algorithms, modes,
        )
    except ImportError:
        raise SystemExit("needs `pip install cryptography`")

    iv, body = raw[:16], raw[16:]
    body = body[: len(body) - (len(body) % 16)]
    decryptor = Cipher(algorithms.AES(REGULATION_KEY), modes.CBC(iv)).decryptor()
    return decryptor.update(body) + decryptor.finalize()


def decompress_dcx(data: bytes) -> bytes:
    """Unwrap the DCX container.

    The compression method is named in the DCP block rather than implied,
    which is fortunate: patch 1.12 switched regulation.bin from zlib to
    ZSTD, and a hard-coded guess would have failed with a checksum error
    that says nothing about the cause.
    """
    if data[:4] != b"DCX\0":
        raise ValueError(f"not a DCX container: {data[:4]!r}")

    dcs = data.find(b"DCS\0")
    uncompressed_size, compressed_size = struct.unpack_from(">II", data, dcs + 4)
    dcp = data.find(b"DCP\0")
    method = data[dcp + 4: dcp + 8]
    dca = data.find(b"DCA\0")
    header_size, = struct.unpack_from(">I", data, dca + 4)
    payload = data[dca + header_size: dca + header_size + compressed_size]

    if method == b"ZSTD":
        try:
            import zstandard
        except ImportError:
            raise SystemExit("this file is ZSTD; needs `pip install zstandard`")
        out = zstandard.ZstdDecompressor().decompress(
            payload, max_output_size=uncompressed_size + 1024
        )
    elif method == b"DFLT":
        import zlib
        out = zlib.decompress(payload)
    else:
        raise ValueError(f"unsupported DCX compression: {method!r}")

    if len(out) != uncompressed_size:
        print(f"warning: expected {uncompressed_size} bytes, got {len(out)}",
              file=sys.stderr)
    return out


def read_bnd4(data: bytes) -> list[tuple[str, bytes]]:
    """List the files in a BND4 archive."""
    if data[:4] != b"BND4":
        raise ValueError(f"not a BND4 archive: {data[:4]!r}")

    file_count, = struct.unpack_from("<i", data, 12)
    files = []
    offset = 64
    for _ in range(file_count):
        compressed_size, = struct.unpack_from("<q", data, offset + 8)
        data_offset, = struct.unpack_from("<I", data, offset + 24)
        name_offset, = struct.unpack_from("<I", data, offset + 32)

        end = data.index(b"\x00\x00", name_offset)
        if (end - name_offset) % 2:
            end += 1
        name = data[name_offset:end].decode("utf-16-le")

        files.append((name, data[data_offset:data_offset + compressed_size]))
        offset += 36
    return files


def describe_param(blob: bytes) -> dict:
    """Row count and row size, without needing a field layout.

    Row size is the gap between the first two rows' data offsets. It is
    not stored anywhere, which is why a paramdef is needed to go further:
    the file knows how big a row is only by implication.
    """
    row_count, = struct.unpack_from("<H", blob, 10)
    if row_count < 2:
        return {"rows": row_count, "row_size": None}

    _, first = struct.unpack_from("<iq", blob, 0x40)
    first, = struct.unpack_from("<q", blob, 0x48)
    second, = struct.unpack_from("<q", blob, 0x40 + 24 + 8)
    return {"rows": row_count, "row_size": second - first}


#: Byte offsets into an NpcParam row, derived from the Paramdex
#: definition and checked against the 736-byte row size. `DamageCutRate`
#: is a multiplier: above 1.0 the enemy takes *more*. `resist_*` is the
#: build-up needed to proc, so lower means easier, and 999 means immune.
FIELDS = {
    "nameId": 12, "hp": 36,
    "neutralDamageCutRate": 420, "slashDamageCutRate": 424,
    "blowDamageCutRate": 428, "thrustDamageCutRate": 432,
    "magicDamageCutRate": 436, "fireDamageCutRate": 440,
    "thunderDamageCutRate": 444, "darkDamageCutRate": 448,
    "resist_poison": 264, "resist_desease": 266, "resist_blood": 268,
    "resist_sleep": 294, "resist_madness": 296, "resist_freeze": 480,
    "superArmorDurability": 680,
}

ROW_SIZE = 736


def load(path: Path) -> list[tuple[str, bytes]]:
    raw = path.read_bytes()
    if raw[:4] == b"BND4":
        return read_bnd4(raw)                 # already unpacked
    data = raw if raw[:4] == b"DCX\0" else decrypt(raw)
    return read_bnd4(decompress_dcx(data))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("regulation", type=Path)
    parser.add_argument("--out", type=Path, help="write every param here")
    parser.add_argument("--list", action="store_true", help="just list them")
    parser.add_argument("--rows", help="describe one param, e.g. NpcParam")
    args = parser.parse_args()

    if not args.regulation.exists():
        print(f"no such file: {args.regulation}", file=sys.stderr)
        return 1

    files = load(args.regulation)
    print(f"{len(files)} params in {args.regulation.name}")

    if args.list:
        for name, blob in files:
            print(f"  {name.split(chr(92))[-1]:<46} {len(blob):>10,} bytes")
        return 0

    if args.rows:
        wanted = args.rows.lower().replace(".param", "")
        for name, blob in files:
            short = name.split("\\")[-1]
            if short.lower().replace(".param", "") != wanted:
                continue
            info = describe_param(blob)
            print(f"\n{short}")
            print(f"  rows     : {info['rows']:,}")
            print(f"  row size : {info['row_size']} bytes")
            for field, offset in sorted(FIELDS.items(), key=lambda kv: kv[1]):
                print(f"    {field:<24} +{offset}")
            return 0
        print(f"no param called {args.rows!r}", file=sys.stderr)
        return 1

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        for name, blob in files:
            (args.out / name.split("\\")[-1]).write_bytes(blob)
        print(f"extracted to {args.out}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
