#!/usr/bin/env python3
"""Decode / re-encode Modal Electronics (Argon8, Cobalt8, Carbon8) firmware .syx files.

Container format (see NOTES.md):
  start:  F0 00 21 07 <dev> 00 <tgt> 7F <count lo7> <count hi7> F7
  data:   F0 00 21 07 <dev> 00 <tgt> 00 <idx lo7> <idx hi7> <152 packed bytes> <checksum nibbles> F7
  end:    F0 00 21 07 <dev> 00 <tgt> 7E F7

Each data block carries 128 image bytes + 5 zero pad bytes (133 = 19 x 7), packed as 19 groups of
7 data bytes followed by one byte holding their MSBs (bit 6 = first byte). The checksum is the sum
of the 128 image bytes: Argon8 sends the low 8 bits as 2 nibbles (low first); Cobalt8/Carbon8 send
16 bits as 4 nibbles (high byte first, each byte low nibble first).

Usage:
  modal_fw.py decode <in.syx> <out.bin> [--elf out.elf]
  modal_fw.py encode <in.bin> <out.syx> --dev 0x18 --target master|panel|slave
  modal_fw.py info <in.syx>...
"""
import argparse
import struct
import sys

MANUFACTURER = bytes([0x00, 0x21, 0x07])
DEVICES = {0x18: "Argon8", 0x19: "Cobalt8", 0x1D: "Carbon8"}
TARGETS = {0: "panel", 1: "master", 2: "slave"}
BLOCK = 128
PACKED = 152
LOAD_ADDRESS = 0x00410000


def split_messages(data):
    msgs, i = [], 0
    while i < len(data):
        if data[i] != 0xF0:
            raise ValueError(f"expected F0 at offset {i:#x}")
        j = data.index(0xF7, i)
        msgs.append(data[i:j + 1])
        i = j + 1
    return msgs


def unpack7(packed):
    out = bytearray()
    for g in range(0, len(packed), 8):
        group, msb = packed[g:g + 7], packed[g + 7]
        out += bytes(b | (0x80 if msb >> (6 - k) & 1 else 0) for k, b in enumerate(group))
    return out


def pack7(raw):
    assert len(raw) % 7 == 0
    out = bytearray()
    for g in range(0, len(raw), 7):
        group = raw[g:g + 7]
        out += bytes(b & 0x7F for b in group)
        out.append(sum(1 << (6 - k) for k, b in enumerate(group) if b & 0x80))
    return out


def checksum_nibbles(dev, block):
    s = sum(block)
    if dev == 0x18:
        return bytes([s & 0xF, s >> 4 & 0xF])
    return bytes([s >> 8 & 0xF, s >> 12 & 0xF, s & 0xF, s >> 4 & 0xF])


def decode(data):
    msgs = split_messages(data)
    head = msgs[0]
    if head[1:4] != MANUFACTURER or head[7] != 0x7F:
        raise ValueError("not a Modal firmware start message")
    dev, tgt = head[4], head[6]
    count = head[8] | head[9] << 7
    if len(msgs) != count + 2 or msgs[-1][7] != 0x7E:
        raise ValueError("block count / end message mismatch")
    image, bad = bytearray(), []
    for n, m in enumerate(msgs[1:-1]):
        if m[:7] != head[:7] or m[7] != 0x00 or (m[8] | m[9] << 7) != n:
            raise ValueError(f"bad header in block {n}")
        raw = unpack7(m[10:10 + PACKED])
        block = bytes(raw[:BLOCK])
        if raw[BLOCK:] != bytes(5) or m[10 + PACKED:-1] != checksum_nibbles(dev, block):
            bad.append(n)
        image += block
    return dev, tgt, bytes(image), bad


def encode(image, dev, tgt):
    if len(image) % BLOCK:
        image += bytes(BLOCK - len(image) % BLOCK)
    count = len(image) // BLOCK
    head = bytes([0xF0]) + MANUFACTURER + bytes([dev, 0x00, tgt])
    out = bytearray(head + bytes([0x7F, count & 0x7F, count >> 7, 0xF7]))
    for n in range(count):
        block = image[n * BLOCK:(n + 1) * BLOCK]
        out += head + bytes([0x00, n & 0x7F, n >> 7])
        out += pack7(block + bytes(5)) + checksum_nibbles(dev, block) + b"\xF7"
    out += head + b"\x7E\xF7"
    return bytes(out)


def to_elf(image, base=LOAD_ADDRESS):
    """Minimal ELF32 ARM wrapper so objdump --triple=thumbv7em can disassemble the image."""
    shstr = b"\0.text\0.shstrtab\0"
    off_data = 52 + 32
    off_shstr = off_data + len(image)
    off_sh = (off_shstr + len(shstr) + 3) & ~3
    entry = struct.unpack_from("<I", image, 4)[0]
    eh = b"\x7fELF\x01\x01\x01" + bytes(9) + struct.pack(
        "<HHIIIIIHHHHHH", 2, 40, 1, entry, 52, off_sh, 0x05000000, 52, 32, 1, 40, 3, 2)
    ph = struct.pack("<8I", 1, off_data, base, base, len(image), len(image), 5, 4)
    sh = bytes(40)
    sh += struct.pack("<10I", 1, 1, 6, base, off_data, len(image), 0, 0, 4, 0)
    sh += struct.pack("<10I", 7, 3, 0, 0, off_shstr, len(shstr), 0, 0, 1, 0)
    out = eh + ph + image + shstr
    return out + bytes(off_sh - len(out)) + sh


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decode")
    d.add_argument("syx")
    d.add_argument("bin")
    d.add_argument("--elf")
    e = sub.add_parser("encode")
    e.add_argument("bin")
    e.add_argument("syx")
    e.add_argument("--dev", type=lambda s: int(s, 0), required=True)
    e.add_argument("--target", choices=TARGETS.values(), required=True)
    i = sub.add_parser("info")
    i.add_argument("syx", nargs="+")
    a = ap.parse_args()

    if a.cmd == "decode":
        dev, tgt, image, bad = decode(open(a.syx, "rb").read())
        if bad:
            sys.exit(f"checksum mismatch in blocks {bad[:10]}")
        open(a.bin, "wb").write(image)
        if a.elf:
            open(a.elf, "wb").write(to_elf(image))
    elif a.cmd == "encode":
        tgt = {v: k for k, v in TARGETS.items()}[a.target]
        open(a.syx, "wb").write(encode(open(a.bin, "rb").read(), a.dev, tgt))
    else:
        for path in a.syx:
            raw = open(path, "rb").read()
            dev, tgt, image, bad = decode(raw)
            sp, reset = struct.unpack_from("<II", image)
            roundtrip = encode(image, dev, tgt) == raw
            print(f"{path}: {DEVICES.get(dev, hex(dev))} {TARGETS[tgt]} {len(image)} bytes "
                  f"SP={sp:#010x} reset={reset:#010x} bad_blocks={len(bad)} roundtrip={roundtrip}")


if __name__ == "__main__":
    main()
