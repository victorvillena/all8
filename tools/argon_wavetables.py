#!/usr/bin/env python3
"""Replace the stored wavetable banks of Argon8 3.2 (slave DSP) with your own single-cycle waves.

How the stock firmware works (see NOTES.md, "Wavetables"):
  * The slave renders a bank's 5 waves into a 1024-float buffer (renderer @ 0x419228), then builds
    12 band-limited mip levels per wave with an FFT (@ 0x41aa48). Any 1024-sample cycle is valid input.
  * Banks 0x00-0x17 are procedural. Banks 0x18-0x23 (12 banks x 5 waves) copy 1024 int16 samples
    from flash, scaled by 1/32767. Those 60 waves are what this tool replaces.
  * The compiler constant-folded each wave's first sample into a literal-pool word. Unshared
    literals are patched to the new value. Four literals (all 0) are shared between several waves,
    so a wave in one of those slots is rotated to start on an upward zero crossing (a phase shift).
  * Bank names exist only in the panel. Its display routine (0x41CFE0) copies each name with a
    fixed-length inlined strcpy, so names can't simply be overwritten. The build retargets the routine's
    bank branch (0x41D010) to a small stub appended after the panel image. The stub looks names up in
    a 36-entry table; stock banks keep their original strings.
  * The panel also keeps its own 128-point copy of every wave for the oscillator display (@ 0x44AAA8).
    The build rewrites the entries of replaced banks in place.

Usage:
  argon_wavetables.py extract <Argon8 syx dir> <out dir>
  argon_wavetables.py build <Argon8 syx dir> <out dir> --bank 0x18=table.wav [--name 0x18="Glass"]
                            [--bank 0x19=a.wav,b.wav,c.wav,d.wav,e.wav]

`--bank` takes one multi-frame WAV (5 frames, or a 1024/2048-sample-frame table from which 5 evenly
spaced frames are taken; override with --frame-size) or 5 comma-separated single-cycle WAVs.
`--name` names a replaced bank. Banks without a name are called "Custom WT 1", "Custom WT 2", … in
bank order, skipping names already in use. Names must be unique among all banks (case-insensitive).
The build writes a complete flashable set (patched panel + slave, stock master) to <out dir>.
"""
import argparse
import cmath
import hashlib
import math
import os
import shutil
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import modal_fw  # noqa: E402

SLAVE_SHA256 = "31bf0dd43de5c4b6d5087ca7164923fb10e00bfa9d8a419531978a7c412c7f16"
BASE = modal_fw.LOAD_ADDRESS
FRAME = 1024
WAVES_PER_BANK = 5
SHARED_LITERALS = {0x419944, 0x41a4a0, 0x41a7b8, 0x41a104}

# (bank, wave) -> (flash address of the 1024 int16 samples, address of the folded first-sample literal)
WAVES = {
    (0x18, 0): (0x4594f0, 0x419c98),
    (0x18, 1): (0x459cf0, 0x419944),
    (0x18, 2): (0x45a4f0, 0x419944),
    (0x18, 3): (0x45acf0, 0x41a4a0),
    (0x18, 4): (0x45b4f0, 0x41a7b8),
    (0x19, 0): (0x46a430, 0x419d30),
    (0x19, 1): (0x46ac30, 0x419d28),
    (0x19, 2): (0x46b430, 0x419d20),
    (0x19, 3): (0x46bc30, 0x41a124),
    (0x19, 4): (0x46c430, 0x41a824),
    (0x1a, 0): (0x467c30, 0x41a104),
    (0x1a, 1): (0x468430, 0x41a104),
    (0x1a, 2): (0x468c30, 0x41a138),
    (0x1a, 3): (0x469430, 0x41a130),
    (0x1a, 4): (0x469c30, 0x41a530),
    # Bank 0x1b is copied from .data (RAM 0x204005ec..); these are its flash init-image addresses.
    (0x1b, 0): (0x496ab8, None),
    (0x1b, 1): (0x4972b8, None),
    (0x1b, 2): (0x497ab8, None),
    (0x1b, 3): (0x4982b8, None),
    (0x1b, 4): (0x498ab8, None),
    (0x1c, 0): (0x48a748, 0x41a4a0),
    (0x1c, 1): (0x48af48, 0x41a4a0),
    (0x1c, 2): (0x48b748, 0x41a4a0),
    (0x1c, 3): (0x48bf48, 0x41a4a0),
    (0x1c, 4): (0x48c748, 0x41a7b8),
    (0x1d, 0): (0x46132c, 0x419944),
    (0x1d, 1): (0x461b2c, 0x419980),
    (0x1d, 2): (0x46232c, 0x419978),
    (0x1d, 3): (0x462b2c, 0x41a4b0),
    (0x1d, 4): (0x46332c, 0x41a7ac),
    (0x1e, 0): (0x463d2c, 0x419944),
    (0x1e, 1): (0x46452c, 0x419944),
    (0x1e, 2): (0x464d2c, 0x419944),
    (0x1e, 3): (0x46552c, 0x41a4a0),
    (0x1e, 4): (0x465d2c, 0x41a7b8),
    (0x1f, 0): (0x472880, 0x41a104),
    (0x1f, 1): (0x473080, 0x41a104),
    (0x1f, 2): (0x473880, 0x41a104),
    (0x1f, 3): (0x474080, 0x41a104),
    (0x1f, 4): (0x474880, 0x41a4a0),
    (0x20, 0): (0x45eb2c, 0x419930),
    (0x20, 1): (0x45f32c, 0x419928),
    (0x20, 2): (0x45fb2c, 0x419920),
    (0x20, 3): (0x46032c, 0x41a160),
    (0x20, 4): (0x460b2c, 0x41a7c4),
    (0x21, 0): (0x481114, 0x41a158),
    (0x21, 1): (0x481914, 0x41a150),
    (0x21, 2): (0x482114, 0x41a148),
    (0x21, 3): (0x482914, 0x419918),
    (0x21, 4): (0x483114, 0x41a538),
    (0x22, 0): (0x48f36c, 0x4199b4),
    (0x22, 1): (0x48fb6c, 0x4199ac),
    (0x22, 2): (0x49036c, 0x4199a4),
    (0x22, 3): (0x490b6c, 0x41a0ec),
    (0x22, 4): (0x49136c, 0x41a7e0),
    (0x23, 0): (0x47dd14, 0x41a51c),
    (0x23, 1): (0x47e514, 0x41a514),
    (0x23, 2): (0x47ed14, 0x41a50c),
    (0x23, 3): (0x47f514, 0x41990c),
    (0x23, 4): (0x47fd14, 0x41a7f4),
}
BANKS = sorted({bank for bank, _ in WAVES})

PANEL_SHA256 = "d0919b01df9e203b068e2d7576a7f1016552ea8c33d5e0585475ed03d56f7d4e"
# Factory bank names, index = bank number (0x00-0x23), as shown by the panel.
STOCK_NAMES = [
    "VA", "002 1", "002 2", "002 3", "002 4", "Retro", "Math", "Formant", "Terrain", "Peaks",
    "Complex 1", "Complex 2", "Harmonic 1", "Harmonic 2", "Jumble", "Perc", "Reso 1", "Reso 2",
    "Sweep", "Fold 1", "Fold 2", "Fold 3", "Min", "Chaos", "Chew", "Grit", "Gnar", "Wicked",
    "Transient", "Feedback", "FM Organ", "Metal", "Fat", "Filter", "Vsync", "Retro 2",
]
# Extra entries in the Osc2 bank list (values 0x7B-0x7E); names must not collide with these either.
OSC2_EXTRA_NAMES = ["PWM", "Clocked Noise", "Sine Noise", "Square Noise"]
NAME_BLOCK = (0x449ED8, 0x44A020)  # where the stock name strings live in the panel image
# The panel's name-entry character set (@ 0x452694), i.e. glyphs the display can draw.
NAME_CHARSET = set(" ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!?#&+-=,.':;<>@_()[]{}~")
NAME_MAX = 13  # longest name this display path already produces ("Clocked Noise")
DEFAULT_NAME = "Custom WT {}"

# Display routine 0x41CFE0: `bls.w 0x41D1E8` taken for kinds 10/11 (Osc1/Osc2 Bank) with r3 = value,
# r4 = output buffer, and {r4, r5, r6, lr} pushed. Every original case returns with pop {r4, r5, r6, pc}.
NAME_HOOK = 0x41D010
NAME_HOOK_ORIGINAL = bytes.fromhex("40f2ea80")  # bls.w 0x41d1e8 (halfwords f240 80ea, little-endian)
NAME_BRANCH = 0x41D1E8
PANEL_FLASH_PAGE = 512  # SAM4E flash page size

# The panel draws the oscillator wave from its own 128-point copy of every wave (draw routine around
# 0x41F290), not from the slave: one byte (0-62, 31 = zero) per point, entry index bank * 5 + wave.
# The factory entries match b = round((sample / 32767 + 1) * 31) taken at every 8th sample.
PREVIEW_TABLE = 0x44AAA8
PREVIEW_POINTS = 128


# --- WAV I/O ---------------------------------------------------------------------------------------

def read_wav(path):
    """Return mono float samples in [-1, 1] from PCM 8/16/24/32-bit or IEEE float32/64 WAV."""
    data = open(path, "rb").read()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError(f"{path}: not a RIFF/WAVE file")
    fmt = body = None
    i = 12
    while i + 8 <= len(data):
        cid, size = data[i:i + 4], struct.unpack_from("<I", data, i + 4)[0]
        chunk = data[i + 8:i + 8 + size]
        if cid == b"fmt ":
            fmt = chunk
        elif cid == b"data":
            body = chunk
        i += 8 + size + (size & 1)
    if fmt is None or body is None:
        raise ValueError(f"{path}: missing fmt or data chunk")
    tag, channels, _, _, align, bits = struct.unpack_from("<HHIIHH", fmt)
    if tag == 0xFFFE:  # WAVE_FORMAT_EXTENSIBLE: real format is the first 2 bytes of the sub-GUID
        tag = struct.unpack_from("<H", fmt, 24)[0]
    width = bits // 8
    n = len(body) // align
    out = []
    for k in range(n):
        acc = 0.0
        for c in range(channels):
            o = k * align + c * width
            if tag == 3:
                v = struct.unpack_from("<f" if width == 4 else "<d", body, o)[0]
            elif tag == 1 and width == 1:
                v = (body[o] - 128) / 128.0
            elif tag == 1:
                v = int.from_bytes(body[o:o + width], "little", signed=True) / float(1 << (bits - 1))
            else:
                raise ValueError(f"{path}: unsupported WAV format tag {tag}")
            acc += v
        out.append(acc / channels)
    return out


def write_wav(path, samples, rate=44100):
    pcm = struct.pack(f"<{len(samples)}h", *samples)
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE")
        f.write(b"fmt " + struct.pack("<I", len(fmt)) + fmt)
        f.write(b"data" + struct.pack("<I", len(pcm)) + pcm)


# --- frame conditioning ----------------------------------------------------------------------------

def resample_cycle(cycle, n=FRAME):
    """Band-limited resample of one period to n samples (keeps harmonics below n/2)."""
    m = len(cycle)
    if m == n:
        return list(cycle)
    harmonics = min(m, n) // 2 - 1
    try:
        import numpy as np
        spec = np.fft.rfft(np.asarray(cycle, dtype=float))
        out = np.zeros(n // 2 + 1, dtype=complex)
        out[:harmonics + 1] = spec[:harmonics + 1] * (n / m)
        return list(np.fft.irfft(out, n))
    except ImportError:
        coeffs = []
        for h in range(harmonics + 1):
            w = -2j * math.pi * h / m
            coeffs.append(sum(x * cmath.exp(w * t) for t, x in enumerate(cycle)) / m)
        res = []
        for t in range(n):
            v = coeffs[0].real
            for h in range(1, harmonics + 1):
                v += 2 * (coeffs[h] * cmath.exp(2j * math.pi * h * t / n)).real
            res.append(v)
        return res


def condition(cycle, normalize=True):
    """Remove DC, peak-normalize, quantize to int16."""
    dc = sum(cycle) / len(cycle)
    cycle = [x - dc for x in cycle]
    peak = max(abs(x) for x in cycle) or 1.0
    gain = 32767.0 / peak if normalize else 32767.0
    return [max(-32767, min(32767, round(x * gain))) for x in cycle]


def rotate_to_zero(frame):
    """Rotate so sample 0 sits on the upward zero crossing nearest zero, and force it to exactly 0."""
    best = min((k for k in range(len(frame)) if frame[k - 1] < 0 <= frame[k]),
               key=lambda k: abs(frame[k]), default=min(range(len(frame)), key=lambda k: abs(frame[k])))
    frame = frame[best:] + frame[:best]
    frame[0] = 0
    return frame


def load_bank_frames(spec, frame_size):
    files = spec.split(",")
    if len(files) == WAVES_PER_BANK:
        return [read_wav(f) for f in files]
    if len(files) != 1:
        raise ValueError(f"give 1 multi-frame WAV or {WAVES_PER_BANK} single-cycle WAVs, got {len(files)}")
    s = read_wav(files[0])
    if frame_size is None:
        if len(s) % WAVES_PER_BANK == 0 and len(s) // WAVES_PER_BANK in (256, 512, 1024, 2048, 4096):
            frame_size = len(s) // WAVES_PER_BANK
        else:
            frame_size = next((f for f in (2048, 1024) if len(s) % f == 0), None)
        if frame_size is None:
            raise ValueError(f"{files[0]}: can't infer frame size from {len(s)} samples; pass --frame-size")
    count = len(s) // frame_size
    if count < 1:
        raise ValueError(f"{files[0]}: shorter than one {frame_size}-sample frame")
    picks = [round(k * (count - 1) / (WAVES_PER_BANK - 1)) for k in range(WAVES_PER_BANK)]
    return [s[p * frame_size:(p + 1) * frame_size] for p in picks]


# --- firmware access ---------------------------------------------------------------------------------

def find_component(src_dir, role):
    for name in sorted(os.listdir(src_dir)):
        if name.lower().endswith(".syx") and f"_{role}_" in name.lower():
            return os.path.join(src_dir, name)
    sys.exit(f"no *_{role}_*.syx in {src_dir}")


def load_component(src_dir, role, target, sha256):
    path = find_component(src_dir, role)
    dev, tgt, image, bad = modal_fw.decode(open(path, "rb").read())
    if bad or dev != 0x18 or tgt != target:
        sys.exit(f"{path}: not a clean Argon8 {role} image")
    if hashlib.sha256(image).hexdigest() != sha256:
        sys.exit(f"{path}: not the Argon8 3.2 {role} this tool was mapped against (SHA-256 mismatch)")
    return path, bytearray(image)


def load_slave(src_dir):
    return load_component(src_dir, "slave", 2, SLAVE_SHA256)


def load_panel(src_dir):
    return load_component(src_dir, "panel", 0, PANEL_SHA256)


def read_wave(image, bank, wave):
    addr, _ = WAVES[(bank, wave)]
    return list(struct.unpack_from(f"<{FRAME}h", image, addr - BASE))


def write_wave(image, bank, wave, frame):
    """Store one int16 frame in the slave image; return it as stored (possibly rotated)."""
    addr, lit = WAVES[(bank, wave)]
    if lit in SHARED_LITERALS:
        frame = rotate_to_zero(frame)
    struct.pack_into(f"<{FRAME}h", image, addr - BASE, *frame)
    if lit is not None and lit not in SHARED_LITERALS:
        struct.pack_into("<i", image, lit - BASE, frame[0])
    return frame


def check_literals(image):
    for (bank, wave), (addr, lit) in WAVES.items():
        if lit is not None:
            s0 = struct.unpack_from("<h", image, addr - BASE)[0]
            if struct.unpack_from("<i", image, lit - BASE)[0] != s0:
                raise AssertionError(f"bank {bank:#x} wave {wave}: literal does not match sample 0")


# --- bank names ----------------------------------------------------------------------------------------

def validate_name(bank, name):
    if not 1 <= len(name) <= NAME_MAX:
        sys.exit(f"bank {bank:#04x}: name {name!r} must be 1-{NAME_MAX} characters")
    if name != name.strip():
        sys.exit(f"bank {bank:#04x}: name {name!r} has leading or trailing spaces")
    bad = sorted(set(name) - NAME_CHARSET)
    if bad:
        sys.exit(f"bank {bank:#04x}: name {name!r} uses characters the panel can't display: {''.join(bad)!r}")


def assign_names(banks, requested):
    """Return the full bank-number -> name list after applying requested and default names."""
    names = list(STOCK_NAMES)
    for bank, name in requested.items():
        validate_name(bank, name)
        names[bank] = name
    unnamed = sorted(b for b in banks if b not in requested)
    taken = {n.lower() for b, n in enumerate(names) if b not in unnamed}
    taken |= {n.lower() for n in OSC2_EXTRA_NAMES}
    number = 1
    for bank in unnamed:
        while DEFAULT_NAME.format(number).lower() in taken:
            number += 1
        names[bank] = DEFAULT_NAME.format(number)
        taken.add(names[bank].lower())
    owners = {}
    labelled = [(f"bank {b:#04x}", n) for b, n in enumerate(names)]
    labelled += [(f"Osc2 entry {n!r}", n) for n in OSC2_EXTRA_NAMES]
    for label, name in labelled:
        if name.lower() in owners:
            sys.exit(f"duplicate wavetable name {name!r}: {owners[name.lower()]} and {label} "
                     "(names are compared case-insensitively)")
        owners[name.lower()] = label
    return names


def write_preview(panel, bank, wave, frame):
    """Replace the panel's 128-point display copy of one wave."""
    step = FRAME // PREVIEW_POINTS
    points = bytes(max(0, min(62, round((frame[i * step] / 32767 + 1) * 31))) for i in range(PREVIEW_POINTS))
    off = PREVIEW_TABLE - BASE + (bank * WAVES_PER_BANK + wave) * PREVIEW_POINTS
    panel[off:off + PREVIEW_POINTS] = points


def thumb_b_cond(cond, src, dst):
    """Encode a Thumb-2 T3 conditional branch (b<cond>.w) at src to dst."""
    imm = dst - (src + 4)
    assert -(1 << 20) <= imm < (1 << 20) and imm % 2 == 0
    s, j2, j1 = imm >> 20 & 1, imm >> 19 & 1, imm >> 18 & 1
    hw1 = 0xF000 | s << 10 | cond << 6 | (imm >> 12 & 0x3F)
    hw2 = 0x8000 | j1 << 13 | j2 << 11 | (imm >> 1 & 0x7FF)
    return struct.pack("<HH", hw1, hw2)


def thumb_b(src, dst):
    """Encode a Thumb-2 T4 unconditional branch (b.w) at src to dst."""
    imm = dst - (src + 4)
    assert -(1 << 24) <= imm < (1 << 24) and imm % 2 == 0
    s, i1, i2 = imm >> 24 & 1, imm >> 23 & 1, imm >> 22 & 1
    j1, j2 = (~i1 ^ s) & 1, (~i2 ^ s) & 1
    hw1 = 0xF000 | s << 10 | (imm >> 12 & 0x3FF)
    hw2 = 0x9000 | j1 << 13 | j2 << 11 | (imm >> 1 & 0x7FF)
    return struct.pack("<HH", hw1, hw2)


def stock_name_addresses(panel):
    lo, hi = NAME_BLOCK
    addrs = []
    for name in STOCK_NAMES:
        needle = name.encode() + b"\0"
        off = panel.find(needle, lo - BASE, hi - BASE)
        while off >= 0 and off % 4:
            off = panel.find(needle, off + 1, hi - BASE)
        if off < 0:
            raise AssertionError(f"stock name {name!r} not found in the panel's name block")
        addrs.append(off + BASE)
    return addrs


def patch_panel_names(panel, names):
    """Append a name stub + table to the panel image and hook the bank-name branch into it."""
    if panel[NAME_HOOK - BASE:NAME_HOOK - BASE + 4] != NAME_HOOK_ORIGINAL:
        raise AssertionError("panel name hook site does not hold the expected instruction")
    stock = stock_name_addresses(panel)
    stub = BASE + len(panel)                # image length is a multiple of 128, so this is aligned
    table = stub + 32
    strings = bytearray()
    entries = []
    for bank, name in enumerate(names):
        if name == STOCK_NAMES[bank]:
            entries.append(stock[bank])
        else:
            entries.append(table + 4 * len(names) + len(strings))
            strings += name.encode() + b"\0"
    code = bytearray()
    code += bytes.fromhex("232b")             # cmp   r3, #0x23
    code += bytes.fromhex("09d8")             # bhi   fallback (stub+24)
    code += bytes.fromhex("054a")             # ldr   r2, [pc, #20]  -> table address (stub+28)
    code += bytes.fromhex("52f82320")         # ldr.w r2, [r2, r3, lsl #2]
    code += bytes.fromhex("12f8011b")         # 1: ldrb r1, [r2], #1
    code += bytes.fromhex("04f8011b")         #    strb r1, [r4], #1
    code += bytes.fromhex("0029")             #    cmp  r1, #0
    code += bytes.fromhex("f9d1")             #    bne  1b
    code += bytes.fromhex("70bd")             # pop   {r4, r5, r6, pc}
    code += thumb_b(stub + len(code), NAME_BRANCH)  # fallback: original branch (Osc2 extras, blanks)
    code += struct.pack("<I", table)
    assert len(code) == 32
    panel += code + struct.pack(f"<{len(entries)}I", *entries) + strings
    # The stock Argon panel is a whole number of 512-byte flash pages. Keep it that way so the new code
    # never sits in a partial final page (the panel bootloader's handling of one is untested).
    panel += bytes(-len(panel) % PANEL_FLASH_PAGE)
    panel[NAME_HOOK - BASE:NAME_HOOK - BASE + 4] = thumb_b_cond(0x9, NAME_HOOK, stub)  # bls.w stub


# --- commands ----------------------------------------------------------------------------------------

def cmd_extract(a):
    _, image = load_slave(a.src)
    os.makedirs(a.out, exist_ok=True)
    for bank in BANKS:
        samples = [s for w in range(WAVES_PER_BANK) for s in read_wave(image, bank, w)]
        label = STOCK_NAMES[bank].replace(" ", "_")
        write_wav(os.path.join(a.out, f"argon8_bank_{bank:#04x}_{label}.wav"), samples)
    print(f"wrote {len(BANKS)} banks ({WAVES_PER_BANK} x {FRAME}-sample frames each) to {a.out}")


def parse_bank(key):
    bank = int(key, 0)
    if bank not in BANKS:
        sys.exit(f"bank {bank:#x} is not a stored bank; choose from {', '.join(f'{b:#x}' for b in BANKS)}")
    return bank


def cmd_build(a):
    slave_path, image = load_slave(a.src)
    panel_path, panel = load_panel(a.src)
    specs = {}
    for spec in a.bank:
        key, _, files = spec.partition("=")
        bank = parse_bank(key)
        if bank in specs:
            sys.exit(f"bank {bank:#04x} given twice")
        specs[bank] = files
    if a.stock_names and a.name:
        sys.exit("--stock-names keeps the factory names; it can't be combined with --name")
    requested = {}
    for spec in a.name or []:
        key, sep, name = spec.partition("=")
        bank = parse_bank(key)
        if not sep or bank not in specs:
            sys.exit(f"--name {spec!r}: give BANK=NAME for a bank that is also replaced with --bank")
        requested[bank] = name
    names = list(STOCK_NAMES) if a.stock_names else assign_names(specs, requested)

    for bank, files in sorted(specs.items()):
        frames = load_bank_frames(files, a.frame_size)
        for wave, cycle in enumerate(frames):
            frame = write_wave(image, bank, wave, condition(resample_cycle(cycle), not a.no_normalize))
            write_preview(panel, bank, wave, frame)
        print(f"bank {bank:#04x} {names[bank]!r} <- {files}")
    check_literals(image)
    if not a.stock_names:
        patch_panel_names(panel, names)

    os.makedirs(a.out, exist_ok=True)
    shutil.copy(find_component(a.src, "master"), a.out)
    for path, img, tgt in [(slave_path, image, 2), (panel_path, panel, 0)]:
        open(os.path.join(a.out, os.path.basename(path)), "wb").write(modal_fw.encode(bytes(img), 0x18, tgt))
    print(f"wrote flashable set to {a.out} (patched panel and slave, stock master)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract", help="dump the 12 stored banks as WAV files")
    e.add_argument("src", help="directory with the stock Argon8 3.2 .syx files")
    e.add_argument("out")
    b = sub.add_parser("build", help="write a flashable set with replaced banks")
    b.add_argument("src", help="directory with the stock Argon8 3.2 .syx files")
    b.add_argument("out")
    b.add_argument("--bank", action="append", required=True, metavar="BANK=WAV[,WAV...]")
    b.add_argument("--name", action="append", metavar="BANK=NAME",
                   help=f"display name for a replaced bank (default: {DEFAULT_NAME.format('<n>')!r})")
    b.add_argument("--stock-names", action="store_true",
                   help="keep the factory bank names (only the panel's wave display is updated)")
    b.add_argument("--frame-size", type=int, help="samples per frame in a multi-frame WAV")
    b.add_argument("--no-normalize", action="store_true", help="keep input level instead of peak-normalizing")
    a = ap.parse_args()
    {"extract": cmd_extract, "build": cmd_build}[a.cmd](a)


if __name__ == "__main__":
    main()
