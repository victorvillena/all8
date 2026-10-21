#!/usr/bin/env python3
"""Build a Carbon8 1.1 firmware set with the 40 Cobalt8 2.1 oscillator algorithms as extra Cores.

Cores 0-55 stay Carbon's own. Cores 56-95 are Cobalt's algorithms in Cobalt's order (VA Sweep ...
Filt Noise). Each oscillator's Contour drives the algorithm's Param A, and a new "Param B" per
oscillator (patch +0x66 / +0x67, parameter IDs 0xB4 / 0xB7) drives Param B. Carbon's Level, XMod,
XCore, Drift, mod matrix and filters apply to Cobalt cores as they do to Carbon's.

How it works (details in NOTES.md, "Cobalt oscillators on Carbon"):
  slave   The Cobalt algorithms (0x416B44-0x439490 of the Cobalt slave) and their tables are copied
          into free flash unchanged except for 578 literal words: Cobalt RAM globals are pointed at a
          small "Cobalt view" in free RAM, flash tables at their copies, and four libgcc helpers at
          Carbon's identical copies. The glue (tools/glue/cobalt_glue.c) fills that view from Carbon's
          oscillator state, runs the algorithm at 48 kHz and upsamples it into Carbon's 96 kHz buffer.
          Hooks: the render dispatcher, the Core loader, the parameter setter (IDs 0xB4/0xB7), the
          Core clamps, and the patch offset -> ID table.
  master  Core clamps raised to 95; ID 0xB4 stores +0x66 (0xB7 already stores +0x67); both are
          readable through the parameter getter; patch loads apply +0x66 like any other byte.
  panel   Core range 0-95 with Cobalt's names; "Param B" entries in the Osc1/Osc2 menus; IDs
          0xB4/0xB7 handled like an ordinary 0-127 parameter everywhere the panel switches on IDs;
          Patch+Level1 / Patch+Level2 turn Param B of Osc1 / Osc2.

Usage:
  carbon_cobalt.py build <Carbon8 1.1 syx dir> <Cobalt8 2.1 syx dir> <out dir>
  carbon_cobalt.py cores
"""
import argparse
import hashlib
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import modal_fw  # noqa: E402
import thumb  # noqa: E402
from thumb import Asm  # noqa: E402

BASE = modal_fw.LOAD_ADDRESS
CARBON_DEV = 0x1D
PAGE = 512

SHA256 = {
    ("carbon", "panel"): "64c179fb654ad5b61bc99e681e27962da139ae7e4dbe2e573ae3ca42362434c6",
    ("carbon", "master"): "c87014f99e61df90d868ac16b33c7beb0c922772e75f6673525ea53a6d18a368",
    ("carbon", "slave"): "52e8420742d18482e25edc8bff1f4c83be6808d2cf57c6c5ec3c9139921a70bb",
    ("cobalt", "panel"): "2bf96abfc7114188416900ede3102a58eae56385d034258c68919a68040bce7a",
    ("cobalt", "master"): "916b66a848e74231465d85e16077d330ef28dc26decfa1f68a8be8ebaa88115f",
    ("cobalt", "slave"): "cebc71da42281624cdaeab2efb7a2eb1e05ca4f6349b48a0da8582ac5676b915",
}
FILES = {"carbon": "Carbon8_{}_1-1.syx", "cobalt": "Cobalt8_{}_2-1.syx"}
TARGET = {"panel": 0, "master": 1, "slave": 2}

N_CARBON_CORES = 56
COBALT_NAMES = [
    "VA Sweep", "VA Crushed", "Spread Saw", "Spread Sqr", "Spread Tri", "PWM", "PWM Dual", "PWM Tri Sqr",
    "PWM Saw Erase", "PWM Tri Pinch", "H-Sync Saw", "H-Sync Sqr", "H-Sync Tri", "Fractal Saw", "Fractal Sqr",
    "Fractal Tri", "Reverse Saw", "Reverse Sqr", "Reverse Tri", "Wind Amp Sync", "Metal Saw", "Metal Sqr",
    "Metal Tri", "RM Saw", "RM Sqr", "RM Tri", "RM Tri Sqr", "RM Saw Sqr", "RM Saw Tri", "FM Saw Squ",
    "FM Squ Squ", "FM Tri Squ", "Noise AM Saw", "Noise AM Squ", "Noise AM Tri", "Chaos Saw", "Chaos Sqr",
    "Chaos Tri", "Fold Tri", "Filt Noise",
]
N_CORES = N_CARBON_CORES + len(COBALT_NAMES)
MAX_CORE = N_CORES - 1                     # 95
ID_PARAM_B = (0xB4, 0xB7)                  # osc 1, osc 2
OFF_PARAM_B = (0x66, 0x67)
PREVIEW_MAGIC = 0xC0BA1709                 # master: preview scratch RAM has been cleared


# ------------------------------------------------------------------------------------------- slave

class CobaltSlave:
    """Addresses in the Cobalt8 2.1 slave."""
    CODE = (0x416B44, 0x439490)            # the 40 algorithms, back to back, with their literal pools
    DATA = (0x449CE8, 0x456E80)            # their tables (+ the .data image of two FM tables)
    DATA_LMA, DATA_VMA = 0x456DDC, 0x20400000
    ALG_SWITCH = 0x4394EC                  # case stubs: ldr r4,=alg; movs r2,#idx; ...
    RATIO, NSUB, SPREAD, B_ALIGNED = 0x455D4C, 0x456998, 0x456970, 0x4511BC
    # literal value -> (kind, target); kind "ram" = Cobalt view symbol, "flash" = copied data,
    # "carbon" = absolute Carbon address. Count = number of literal words with that value.
    LITERALS = {
        0x2040B7C8: ("ram", "cv_patch", 55),
        0x204000C4: ("ram", "cv_route", 82),
        0x2040299C: ("ram", "cv_mod_a", 49),
        0x2040699C: ("ram", "cv_mod_b", 50),
        0x2040A99C: ("ram", "cv_unipolar", 44),
        0x2040A9F8: ("ram", "cv_mod_ctrl", 44),
        0x2040AAD9: ("ram", "cv_mod_on_a1", 40),
        0x2040AADA: ("ram", "cv_mod_on_b1", 34),
        0x2040AADB: ("ram", "cv_mod_on_a2", 40),
        0x2040AADC: ("ram", "cv_mod_on_b2", 34),
        0x20400014: ("flash", 0x456DDC + 0x14, 3),      # FM detune table (.data, never written)
        0x20400054: ("flash", 0x456DDC + 0x54, 6),      # FM ratio table (.data, never written)
        0x4477E1: ("carbon", 0x42E099, 12),             # libgcc __aeabi_dsub (identical code in Carbon)
        0x4477E5: ("carbon", 0x42E09D, 11),             # __aeabi_dadd
        0x447AA1: ("carbon", 0x42E359, 11),             # __aeabi_f2d
        0x4480A9: ("carbon", 0x42E961, 12),             # __aeabi_d2f
        **{t: ("flash", t, n) for t, n in (
            (0x449CE8, 6), (0x44A2E0, 1), (0x44A4E4, 3), (0x44E960, 4), (0x4500F8, 3), (0x450138, 4),
            (0x450178, 6), (0x4501BC, 3), (0x45123C, 3), (0x452B44, 3), (0x456998, 15))},
    }
    LIBGCC = {0x4477E0: 0x42E098, 0x4477E4: 0x42E09C, 0x447AA0: 0x42E358, 0x4480A8: 0x42E960}


class CarbonSlave:
    """Addresses in the Carbon8 1.1 slave."""
    END = 0x46E980
    CODE_AT = 0x470000                     # Cobalt algorithms
    DATA_AT = 0x493000                     # Cobalt tables
    GLUE_AT = 0x4A1000                     # glue + stubs
    RAM_AT = 0x20440000                    # Cobalt view (free RAM: .bss + stack end at 0x20431AA8)
    RAM_END = 0x2045B000
    CORE = 0x20406714                      # int[2]: Core per oscillator
    CORE_DIRTY = 0x2040004C                # int[2]: set by the Core loader
    PATCH = 0x20413134
    MOD_CTRL, MOD_A, MOD_B = 0x204109C4, 0x20408964, 0x2040C964
    ROUTE = 0x204020B8
    CONTOUR_MOD_ON = 0x20410AAD            # [0] osc 1, [2] osc 2
    UNIPOLAR = 0x20410964
    KIND_TABLE = 0x4521B8                  # Core -> loader kind
    RENDER_HOOK = 0x41A164                 # after the pitch setup: ldr r3,=CORE ; ldr r2,=KIND_TABLE
    RENDER_RESUME = 0x41A168
    SET_CORE = 0x411DE0                    # set_core(core, load, osc): cmp r0,#0x37 ; ldr r3,=KIND_TABLE
    SET_CORE_RESUME = 0x411DE4             # it ge ; movge r0,#0x37
    SETTER = 0x429720                      # setter(id, value): subs r0,#3 ; push.w {r4-r8,lr}
    SETTER_RESUME = 0x429726
    CORE_CLAMPS = (0x42A1AC, 0x42A1CC)     # cmp r1,#0x37 ; ldr ; it ge ; movge r4,#0x37 (IDs 0x3F, 0x3E)
    OFFSET_TO_ID = 0x45278C


def check(cond, what):
    if not cond:
        raise RuntimeError(f"unexpected firmware contents: {what}")


def put(image, addr, data):
    off = addr - BASE
    if off + len(data) > len(image):
        image.extend(bytes(off + len(data) - len(image)))
    image[off:off + len(data)] = data


def get(image, addr, n):
    return bytes(image[addr - BASE:addr - BASE + n])


def u32(image, addr):
    return struct.unpack_from("<I", image, addr - BASE)[0]


def expect(image, addr, hexbytes, what):
    check(get(image, addr, len(bytes.fromhex(hexbytes))) == bytes.fromhex(hexbytes), what)


def cobalt_alg_addresses(cobalt):
    """The 40 algorithm entry points, read from the Cobalt alg switch (0x439490)."""
    algs = {}
    a = CobaltSlave.ALG_SWITCH
    while a < 0x4396CC:
        h1, h2 = struct.unpack_from("<HH", cobalt, a - BASE)
        if h1 >> 11 == 0b01001 and h2 >> 8 == 0x22:                    # ldr r4,[pc,#..] ; movs r2,#idx
            algs[h2 & 0xFF] = u32(cobalt, ((a + 4) & ~3) + (h1 & 0xFF) * 4)
        a += 2
    check(sorted(algs) == list(range(40)), "Cobalt alg switch")
    return [algs[i] for i in range(40)]


def load_glue():
    with open(os.path.join(HERE, "glue", "cobalt_glue.json")) as f:
        g = json.load(f)
    sections = {".text": (1, bytes.fromhex(g["text"]), len(g["text"]) // 2), ".bss": (8, b"", g["bss_size"])}
    obj = {"sections": sections, "symbols": [tuple(s) for s in g["symbols"]],
           "relocs": {".text": [tuple(r) for r in g["relocs"]]}}
    return obj


def patch_slave(carbon, cobalt):
    """Return (patched Carbon slave image, {name: address}) with the Cobalt algorithms as Cores 56-95."""
    C, K = CarbonSlave, CobaltSlave
    img = bytearray(carbon)
    check(len(img) == C.END - BASE, "Carbon slave size")
    for a, b in K.LIBGCC.items():
        check(get(cobalt, a, 64) == get(img, b, 64), f"libgcc helper {a:#x}")
    code_delta = C.CODE_AT - K.CODE[0]
    data_delta = C.DATA_AT - K.DATA[0]
    check(code_delta % 4 == 0 and data_delta % 4 == 0, "copy alignment")

    # glue: text after a 40-entry table of algorithm pointers
    glue = load_glue()
    syms = {}
    algs_at = C.GLUE_AT
    text_at = C.GLUE_AT + 0x100
    alg_ptrs = [a + code_delta for a in cobalt_alg_addresses(cobalt)]
    check(all(K.CODE[0] <= (a - code_delta) & ~1 < K.CODE[1] for a in alg_ptrs), "alg range")
    externs = {
        "ca_core": C.CORE, "ca_patch": C.PATCH, "ca_mod_ctrl": C.MOD_CTRL, "ca_mod_a": C.MOD_A,
        "ca_mod_b": C.MOD_B, "ca_route": C.ROUTE, "ca_contour_mod_on": C.CONTOUR_MOD_ON,
        "ca_unipolar": C.UNIPOLAR, "co_algs": algs_at,
        "co_ratio": K.RATIO + data_delta, "co_nsub": K.NSUB + data_delta,
        "co_spread": K.SPREAD + data_delta, "co_b_aligned": K.B_ALIGNED + data_delta,
    }
    text, gsyms = thumb.link_object(glue, text_at, C.RAM_AT, externs)
    check(C.RAM_AT + glue["sections"][".bss"][2] <= C.RAM_END, "glue RAM")
    syms.update(externs)
    syms.update(gsyms)

    # Cobalt code and tables
    code = bytearray(get(cobalt, K.CODE[0], K.CODE[1] - K.CODE[0]))
    counts = {}
    for off in range(0, len(code), 4):
        v, = struct.unpack_from("<I", code, off)
        if v not in K.LITERALS:
            continue
        kind, target, _ = K.LITERALS[v]
        if kind == "flash":
            check(K.DATA[0] <= target < K.DATA[1], f"table {target:#x} outside the copy")
        new = gsyms[target] if kind == "ram" else target + data_delta if kind == "flash" else target
        struct.pack_into("<I", code, off, new)
        counts[v] = counts.get(v, 0) + 1
    check(counts == {v: n for v, (_, _, n) in K.LITERALS.items()}, "Cobalt literal counts")
    put(img, C.CODE_AT, code)
    put(img, C.DATA_AT, get(cobalt, K.DATA[0], K.DATA[1] - K.DATA[0]))
    put(img, algs_at, struct.pack("<40I", *alg_ptrs))
    put(img, text_at, text)

    # stubs after the glue
    stub_at = (text_at + len(text) + 0xFF) & ~0xFF
    render = gsyms["cobalt_render"]

    # 1. render dispatcher: Cores >= 56 go to cobalt_render(state=r4, out=r7, contour=r8, osc=r6)
    expect(img, C.RENDER_HOOK, "744b754a", "render dispatcher")
    s = Asm(stub_at)
    s.mov32("r3", C.CORE)
    s.ldr_lsl2("r3", "r3", "r6")
    s.cmp("r3", N_CARBON_CORES)
    s.bcond("ge", "cobalt")
    s.mov32("r3", C.CORE)
    s.mov32("r2", C.KIND_TABLE)
    s.b(C.RENDER_RESUME)
    s.label("cobalt")
    s.mov("r0", "r4")
    s.mov("r1", "r7")
    s.mov("r2", "r8")
    s.mov("r3", "r6")
    s.bl(render)
    s.pop(["r4", "r5", "r6", "r7", "r8", "pc"])
    stub = s.assemble()
    put(img, stub_at, stub)
    put(img, C.RENDER_HOOK, thumb.enc_b_w(C.RENDER_HOOK, stub_at))
    syms["stub_render"] = stub_at
    stub_at = (stub_at + len(stub) + 15) & ~15

    # 2. Core loader: Cores 56-95 only store the Core (nothing to load), Carbon Cores unchanged
    expect(img, C.SET_CORE, "37281c4ba8bf3720", "Core loader")
    s = Asm(stub_at)
    s.cmp("r0", N_CARBON_CORES)
    s.bcond("ge", "cobalt")
    s.mov32("r3", C.KIND_TABLE)
    s.cmp("r0", 0x37)
    s.b(C.SET_CORE_RESUME)
    s.label("cobalt")
    s.cmp("r0", MAX_CORE)
    s.bcond("le", "store")
    s.movs("r0", MAX_CORE)
    s.label("store")
    s.mov32("r3", C.CORE)
    s.str_lsl2("r0", "r3", "r2")
    s.mov32("r3", C.CORE_DIRTY)
    s.movs("r1", 1)
    s.str_lsl2("r1", "r3", "r2")
    s.bx("lr")
    stub = s.assemble()
    put(img, stub_at, stub)
    put(img, C.SET_CORE, thumb.enc_b_w(C.SET_CORE, stub_at))
    syms["stub_set_core"] = stub_at
    stub_at = (stub_at + len(stub) + 15) & ~15

    # 3. setter: IDs 0xB4/0xB7 store Param B into the patch copy
    expect(img, C.SETTER, "03382de9f041", "slave setter")
    s = Asm(stub_at)
    for pid, off in zip(ID_PARAM_B, OFF_PARAM_B):
        s.cmp("r0", pid)
        s.bcond("eq", f"b{off:x}")
    s.h(0x3803)                                       # subs r0,#3
    s.push(["r4", "r5", "r6", "r7", "r8", "lr"])
    s.b(C.SETTER_RESUME)
    for off in OFF_PARAM_B:
        s.label(f"b{off:x}")
        s.mov32("r3", C.PATCH)
        s.strb("r1", "r3", off)
        s.bx("lr")
    stub = s.assemble()
    put(img, stub_at, stub)
    put(img, C.SETTER, thumb.enc_b_w(C.SETTER, stub_at))
    syms["stub_setter"] = stub_at
    stub_at = (stub_at + len(stub) + 15) & ~15

    # 4. Core clamps 55 -> 95
    for a in C.CORE_CLAMPS:
        expect(img, a, "3729", "Core clamp")
        expect(img, a + 4, "a8bf3724", "Core clamp")
        put(img, a, bytes([MAX_CORE, 0x29]))
        put(img, a + 6, bytes([MAX_CORE, 0x24]))

    # 5. patch load applies +0x66 through ID 0xB4 (+0x67 is already ID 0xB7)
    check(img[C.OFFSET_TO_ID + OFF_PARAM_B[0] - BASE] == 0 and img[C.OFFSET_TO_ID + OFF_PARAM_B[1] - BASE] == 0xB7,
          "slave offset -> ID table")
    img[C.OFFSET_TO_ID + OFF_PARAM_B[0] - BASE] = ID_PARAM_B[0]

    syms["end"] = stub_at
    pad = (-len(img)) % PAGE
    img.extend(b"\xff" * pad)
    return bytes(img), syms


# ------------------------------------------------------------------------------------------ master

class CobaltMaster:
    """Addresses in the Cobalt8 2.1 master: the oscillator preview renderers (the 128-point
    waveform the panel draws). Each is preview(out[128], param_a, param_b), a leaf function."""
    SWITCH = 0x42D41C                      # tbh table, 40 cases: add r0,sp,#0x88 ; ldr r3,=fn ; blx r3
    CODE = (0x423C18, 0x427808)
    TABLES = ((0x44D4E4, 0x44D8EC), (0x453CE8, 0x453EEC), (0x46D2CC, 0x46F008), (0x470804, 0x4710CC),
              (0x4730CC, 0x4736D0))
    DATA = (0x204001B0, 0x20400AB4)        # constant .data tables (never written), image at DATA_LMA
    DATA_LMA, DATA_VMA = 0x4741E4, 0x20400000
    SCRATCH = ((0x20414EA8, 0x20414EC4), (0x2041EB58, 0x2041ED7C))   # .bss written before read
    LITERALS = {                           # value -> number of literal words (checked)
        0x44D4E4: 1, 0x44D6E8: 3, 0x453CE8: 4, 0x46D2CC: 3, 0x46D300: 3, 0x46D334: 2, 0x46E388: 6,
        0x46E3D4: 5, 0x46EE04: 4, 0x470804: 3, 0x470A08: 4, 0x470CC4: 3, 0x470EC8: 12, 0x4730CC: 7,
        0x204001B0: 3, 0x204001F0: 3, 0x204008D4: 6, 0x20400940: 3, 0x20400A94: 6,
        **{a: 1 for a in range(0x20414EA8, 0x20414EC4, 4)},
        **{a: 1 for a in (0x2041EB58, 0x2041EB5C, 0x2041EB60, 0x2041EB64, 0x2041EB68, 0x2041EB6C, 0x2041EB70,
                          0x2041EB74, 0x2041EB78, 0x2041EB84, 0x2041EB88, 0x2041EB8C, 0x2041EB90, 0x2041EB94,
                          0x2041EB98, 0x2041EB9C, 0x2041EBA0, 0x2041EC88, 0x2041EC94, 0x2041ECA0, 0x2041ECAC,
                          0x2041ECB0, 0x2041ECB4, 0x2041ECB8, 0x2041ECBC, 0x2041ED44, 0x2041ED48, 0x2041ED4C,
                          0x2041ED64, 0x2041ED68, 0x2041ED6C, 0x2041ED70, 0x2041ED74, 0x2041ED78)},
        0x2041EC84: 2,
    }


class CarbonMaster:
    END = 0x484E00
    STUB_AT = 0x485000
    PREVIEW_AT = 0x486000                  # Cobalt preview code, then its tables
    FREE_END = 0x49C000                    # user storage starts here
    RAM_AT = 0x20452000                    # preview scratch (free: stack ends 0x2044FFF0, heap limit 0x2045AFFC)
    EDIT = 0x2043B9E4                      # edit buffer
    SETTER_TABLE = 0x431844                # ldr pc,[table, (id-3)*4]; value in r6
    SETTER_DONE = 0x430426                 # common exit
    CORE_CLAMPS = (0x4305CA, 0x430606)     # cmp r6,#0x37 ; it ge ; movge r6,#0x37 (IDs 0x3E, 0x3F)
    OFFSET_TO_ID = 0x452EE8
    PREVIEW_HOOK = 0x42DB24                # ldr r3,=CORE_KIND ; ldr.w r6,[r3,r5,lsl #2] (r5 = Core, r4 = osc)
    PREVIEW_RESUME = 0x42DB2A
    PREVIEW_SEND = 0x42DBD8                # builds and sends the 0x7A message from sp+0x88
    CORE_KIND = 0x4806A4


def cobalt_preview_addresses(cobalt_master):
    fns = []
    for k in range(40):
        case = CobaltMaster.SWITCH + 2 * struct.unpack_from("<H", cobalt_master, CobaltMaster.SWITCH - BASE + 2 * k)[0]
        h0, h1 = struct.unpack_from("<HH", cobalt_master, case - BASE)
        check(h0 == 0xA822 and h1 >> 8 == 0x4B, f"Cobalt preview case {k}")
        fns.append(u32(cobalt_master, ((case + 6) & ~3) + (h1 & 0xFF) * 4))
    check(all(CobaltMaster.CODE[0] <= f & ~1 < CobaltMaster.CODE[1] for f in fns), "Cobalt preview range")
    return fns


def patch_master(master, cobalt_master):
    M, K = CarbonMaster, CobaltMaster
    img = bytearray(master)
    check(len(img) == M.END - BASE, "Carbon master size")
    syms = {}

    # Cobalt preview renderers: code, tables, constant .data, scratch RAM
    at = M.PREVIEW_AT
    code_delta = at - K.CODE[0]
    check(code_delta % 4 == 0, "preview alignment")
    moved = [(K.CODE[0], K.CODE[1], at)]
    at = (at + K.CODE[1] - K.CODE[0] + 0xFF) & ~0xFF
    for lo, hi in K.TABLES:
        at += lo % 4
        moved.append((lo, hi, at))
        put(img, at, get(cobalt_master, lo, hi - lo))
        at = (at + hi - lo + 0xF) & ~0xF
    data_at = at
    put(img, data_at, get(cobalt_master, K.DATA_LMA + K.DATA[0] - K.DATA_VMA, K.DATA[1] - K.DATA[0]))
    moved.append((K.DATA[0], K.DATA[1], data_at))
    at = (data_at + K.DATA[1] - K.DATA[0] + 0xF) & ~0xF
    ram = M.RAM_AT
    for lo, hi in K.SCRATCH:
        moved.append((lo, hi, ram))
        ram = (ram + hi - lo + 0xF) & ~0xF
    scratch_end = ram
    magic_at = ram                         # scratch is zeroed on first use, keyed on this word
    table_at = at
    at += 4 * 40
    check(at <= M.FREE_END, "master free flash")

    def relocate(v):
        for lo, hi, new in moved:
            if lo <= v < hi:
                return new + v - lo
        raise RuntimeError(f"no mapping for {v:#x}")

    code = bytearray(get(cobalt_master, K.CODE[0], K.CODE[1] - K.CODE[0]))
    counts = {}
    for off in range(0, len(code), 4):
        v, = struct.unpack_from("<I", code, off)
        if v in K.LITERALS:
            struct.pack_into("<I", code, off, relocate(v))
            counts[v] = counts.get(v, 0) + 1
    check(counts == K.LITERALS, "Cobalt preview literal counts")
    put(img, M.PREVIEW_AT, code)
    put(img, table_at, struct.pack("<40I", *(f + code_delta for f in cobalt_preview_addresses(cobalt_master))))
    syms.update(preview_table=table_at, preview_ram=M.RAM_AT, preview_magic=magic_at, preview_end=at)

    # stubs
    stub_at = M.STUB_AT

    # 1. setter: ID 0xB4 stores +0x66 (ID 0xB7 already stores +0x67 at 0x432022)
    entry = M.SETTER_TABLE + 4 * (ID_PARAM_B[0] - 3)
    check(u32(img, entry) == M.SETTER_DONE | 1, "master setter: ID 0xB4 unused")
    b2 = u32(img, M.SETTER_TABLE + 4 * (ID_PARAM_B[1] - 3)) & ~1
    expect(img, b2 + 2, "83f86760", "master setter: ID 0xB7 stores +0x67")
    s = Asm(stub_at)
    s.mov32("r3", M.EDIT)
    s.strb("r6", "r3", OFF_PARAM_B[0])
    s.b(M.SETTER_DONE)
    stub = s.assemble()
    put(img, stub_at, stub)
    struct.pack_into("<I", img, entry - BASE, stub_at | 1)
    syms["stub_setter_b1"] = stub_at
    stub_at = (stub_at + len(stub) + 15) & ~15

    # 2. oscillator preview: Cores >= 56 use Cobalt's renderer with Param A (Contour) and Param B
    expect(img, M.PREVIEW_HOOK, "6c4b53f82560", "master preview generator")
    s = Asm(stub_at)
    s.cmp("r5", N_CARBON_CORES)
    s.bcond("ge", "cobalt")
    s.mov32("r3", M.CORE_KIND)
    s.ldr_lsl2("r6", "r3", "r5")
    s.b(M.PREVIEW_RESUME)
    s.label("cobalt")
    # Some renderers keep state in their scratch RAM between calls (Cobalt's .bss, zero at boot). Carbon's
    # startup doesn't clear this RAM, so clear it once here (r1-r3 and r6 are free at the hook).
    s.mov32("r3", magic_at)
    s.ldr("r1", "r3")
    s.mov32("r2", PREVIEW_MAGIC)
    s.h(0x4291)                                       # cmp r1, r2
    s.bcond("eq", "ready")
    s.str("r2", "r3")
    s.mov32("r3", M.RAM_AT)
    s.mov32("r1", 0)
    s.mov32("r2", scratch_end)
    s.label("clear")
    s.h(0xF843, 0x1B04)                               # str.w r1, [r3], #4
    s.h(0x4293)                                       # cmp r3, r2
    s.bcond("ne", "clear")
    s.label("ready")
    s.h(0xA822)                                       # add r0, sp, #0x88
    s.mov32("r3", M.EDIT)
    s.h(0x4423)                                       # add r3, r4
    s.ldrb("r1", "r3", 6)
    s.ldrb("r2", "r3", OFF_PARAM_B[0])
    s.h(0x3D00 | N_CARBON_CORES)                      # subs r5, #56
    s.mov32("r3", table_at)
    s.ldr_lsl2("r3", "r3", "r5")
    s.blx("r3")
    s.b(M.PREVIEW_SEND)
    stub = s.assemble()
    put(img, stub_at, stub)
    put(img, M.PREVIEW_HOOK, thumb.enc_b_w(M.PREVIEW_HOOK, stub_at))
    syms["stub_preview"] = stub_at
    stub_at = (stub_at + len(stub) + 15) & ~15
    check(stub_at <= M.PREVIEW_AT, "master stubs")

    # 3. Core clamps 55 -> 95
    for a in M.CORE_CLAMPS:
        expect(img, a, "372ea8bf3726", "master Core clamp")
        put(img, a, bytes([MAX_CORE, 0x2E]))
        put(img, a + 4, bytes([MAX_CORE, 0x26]))

    # 4. patch load applies +0x66 through ID 0xB4
    check(img[M.OFFSET_TO_ID + OFF_PARAM_B[0] - BASE] == 0 and img[M.OFFSET_TO_ID + OFF_PARAM_B[1] - BASE] == 0xB7,
          "master offset -> ID table")
    img[M.OFFSET_TO_ID + OFF_PARAM_B[0] - BASE] = ID_PARAM_B[0]

    img.extend(b"\xff" * ((-len(img)) % PAGE))
    return bytes(img), syms


# ------------------------------------------------------------------------------------------- panel

class CobaltPanel:
    CORE_NAMES = 0x446E08                  # 40 x 17 bytes: algorithm names
    PARAM_NAMES = 0x44447C                 # 40 x (13 + 13) bytes: Param A name, Param B name


class CarbonPanel:
    END = 0x44CD80
    NEW_AT = 0x44CE00                      # appended tables and stubs
    DATA_LMA, DATA_VMA = 0x448BBC, 0x20000000
    RECORDS = 0x20000678                   # parameter records (.data), 32 bytes each
    N_RECORDS = 265
    RECORD_LITERALS = 31                   # literal words holding RECORDS
    RECORD_204_MIN = 0x20001FFC            # the one literal pointing inside the table (record 204 + 4)
    RECORD_COUNTS = (0x41B134, 0x42020E)   # movw rX, #0x109
    NEW_RECORDS = 0x2001DC00               # top of the newlib heap
    SBRK_LIMIT = 0x415430                  # literal 0x2001FFFC used by _sbrk (0x41540C)
    MAIN_LITERAL = 0x4152A4                # reset handler: ldr r3,=main ; blx r3
    MAIN = 0x437EAD
    MENUS = {1: 0x44BA8C, 2: 0x44BAC8}     # Osc1 / Osc2 menus (.data image): halfword record indices
    MENU_SLOT = 60
    CORE_RECORDS = (9, 10)
    CORE_NAMES = 0x4478FC                  # 56 x 17 bytes
    CORE_NAME_CLAMP = 0x41BD6E             # cmp r1,#0x37 ; it ge ; movge r3,#0x37
    CORE_NAME_LITERAL = 0x41BF4C
    CONTOUR_NAMES = 0x445254               # 56 x 13 bytes: what Contour is called for each Core
    CONTOUR_NAME_LITERALS = (0x41E12C, 0x422554)
    CORE_SETTER_CLAMPS = (0x427702, 0x42771A)   # cmp r6,#0x37 ; ldrb.w ; it ge ; movge r6,#0x37
    TITLE_NAME = 0x421604                  # add.w r1,sb,r1,lsl #5 ; adds r1,#9 ; ldr r0,=title ; blx r5 (strcpy)
    TITLE_NAME_RESUME = 0x421608           # (r1 = record index, sb = record table)
    TITLE_NAME_COPY = 0x42160A
    PATCH = 0x2000D528
    # every switch on parameter ID (`cmp rN,#0xC3` before the dispatch); IDs 0xB4/0xB7 fall to the
    # default case in all of them and get a copy of ID 0x12's case (XMod, +0x08) with the offset changed
    ID_SWITCHES = (0x41D624, 0x41E16C, 0x41F258, 0x41F492, 0x41FDE4, 0x420BC8, 0x421840, 0x421C68,
                   0x422582, 0x42272A, 0x4233B0, 0x426D3E, 0x4277EE, 0x427E82, 0x42AE7A, 0x42B19C,
                   0x42D63A, 0x42D854, 0x42DB0A, 0x42DE2E)
    TEMPLATE_ID, TEMPLATE_OFFSET = 0x12, 0x08
    # knob handler 0x435DE0: one case per knob loads r9 = value, r0 = max, r10 = ID, then goes to KNOB_SET
    UI = 0x2000CDB4                        # UI state: +0 -> shift button's held flag, +0xD Patch held
    UI_PATCH_HELD = 0x0D
    PATCH_USED = 0x200074D2                # set by Patch+knob cases; Patch's release then does nothing else
    LEVEL_KNOBS = (0x436004, 0x436190)     # Level1/Level2 cases: ldr r3,=UI ; ldr r3,[r3] ; ldrb r3,[r3] ;
                                           # cmp r3,#0 ; beq.w Level (else the shift layer: Glide / Drift)
    LEVEL_IDS = (0x13, 0x14)
    KNOB_SET = 0x435EA4


def id_switch(img, cmp_addr):
    """Decode `cmp rN,#0xC3 ; bhi.w default ; (tbh [pc,rN,lsl #1] | adr rT,#4 ; ldr.w pc,[rT,rN,lsl #2])`.
    Returns (rN, default, dispatch address, kind, table address)."""
    h, = struct.unpack_from("<H", img, cmp_addr - BASE)
    check(h & 0xF8FF == 0x28C3, f"ID switch cmp at {cmp_addr:#x}")
    rn = h >> 8 & 7
    h1, h2 = struct.unpack_from("<HH", img, cmp_addr + 2 - BASE)
    default = thumb.decode_branch(h1, h2, cmp_addr + 2)
    check(default is not None and h1 >> 6 & 0xF == thumb.CONDS["hi"], f"ID switch bhi at {cmp_addr:#x}")
    disp = cmp_addr + 6
    d1, d2 = struct.unpack_from("<HH", img, disp - BASE)
    if d1 == 0xE8DF and d2 & 0xFFF0 == 0xF010 and d2 & 0xF == rn:
        return rn, default, disp, "tbh", disp + 4
    rt = d1 >> 8 & 7
    d3, = struct.unpack_from("<H", img, disp + 4 - BASE)
    check(d1 & 0xF8FF == 0xA001 and d2 == 0xF850 | rt and d3 == 0xF020 | rn,   # adr rT,#4 ; ldr.w pc,[rT,rN,lsl #2]
          f"ID switch dispatch at {disp:#x}")
    return rn, default, disp, "ldr", ((disp + 4) & ~3) + 4


def id_case(img, kind, table, pid):
    if kind == "tbh":
        return table + 2 * struct.unpack_from("<H", img, table - BASE + 2 * (pid - 3))[0]
    return u32(img, table + 4 * (pid - 3)) & ~1


def clone_case(img, src, s, offset):
    """Copy the template case at src into Asm s, changing its patch offset 0x08 to `offset`, up to and
    including its first unconditional branch. Only the instruction forms the panel's cases use are
    accepted; anything else stops the build."""
    a, changed = src, 0
    while True:
        h1, = struct.unpack_from("<H", img, a - BASE)
        h2 = struct.unpack_from("<H", img, a + 2 - BASE)[0]
        if h1 >> 11 == 0b01001:                                       # ldr rX,[pc,#imm]
            s.mov32(h1 >> 8 & 7, u32(img, ((a + 4) & ~3) + (h1 & 0xFF) * 4))
            a += 2
        elif h1 & 0xF800 == 0x7800 and h1 >> 6 & 0x1F == CarbonPanel.TEMPLATE_OFFSET:   # ldrb rt,[rn,#8]
            s.ldrb(h1 & 7, h1 >> 3 & 7, offset)
            a += 2
            changed += 1
        elif h1 & 0xF800 == 0x7000 and h1 >> 6 & 0x1F == CarbonPanel.TEMPLATE_OFFSET:   # strb rt,[rn,#8]
            s.strb(h1 & 7, h1 >> 3 & 7, offset)
            a += 2
            changed += 1
        elif h1 & 0xFFF0 in (0xF890, 0xF880) and h2 & 0xFFF == CarbonPanel.TEMPLATE_OFFSET:   # ldrb.w/strb.w
            (s.ldrb if h1 & 0xFFF0 == 0xF890 else s.strb)(h2 >> 12, h1 & 0xF, offset)
            a += 4
            changed += 1
        elif h1 & 0xFF00 == 0xB200 or h1 & 0xFF00 == 0x4600:          # sxth/sxtb/uxth/uxtb, mov
            s.h(h1)
            a += 2
        elif h1 == 0xFA1F and h2 & 0xF0F0 == 0xF080:                  # uxth.w rd, rm
            s.h(h1, h2)
            a += 4
        elif h1 >> 11 == 0b11100:                                     # b (16-bit)
            off = (h1 & 0x7FF) << 1
            s.b(a + 4 + (off - 0x1000 if off & 0x800 else off))
            break
        else:
            t = thumb.decode_branch(h1, h2, a)
            check(t is not None and h2 & 0x1000, f"unsupported instruction {h1:04x} in case at {src:#x}")
            s.b(t)
            break
    check(changed == 1, f"template case at {src:#x}")


def patch_panel(panel, cobalt_panel):
    P = CarbonPanel
    img = bytearray(panel)
    check(len(img) == P.END - BASE, "Carbon panel size")
    syms = {}
    lma = lambda vma: P.DATA_LMA + vma - P.DATA_VMA
    at = P.NEW_AT

    def emit(data, align=4):
        nonlocal at
        at = (at + align - 1) & ~(align - 1)
        addr = at
        put(img, addr, data)
        at += len(data)
        return addr

    # 1. parameter records: copy to the top of the heap at boot, with "Param B" for each oscillator
    recs = bytearray(get(img, lma(P.RECORDS), 32 * P.N_RECORDS))
    for i in P.CORE_RECORDS:
        check(recs[32 * i + 5] == N_CARBON_CORES - 1 and recs[32 * i + 9:32 * i + 14] == b"Core\0", "Core records")
        recs[32 * i + 5] = MAX_CORE
    new_index = {}
    for osc, (pid, off) in enumerate(zip(ID_PARAM_B, OFF_PARAM_B), 1):
        rec = bytes([1, 0, pid, 0, 0, 127, 3, 0xFF, osc]) + b"Param B".ljust(23, b"\0")
        new_index[osc] = len(recs) // 32
        recs += rec
    n_records = len(recs) // 32
    rec_image = emit(bytes(recs))
    check(P.NEW_RECORDS + len(recs) <= 0x2001FF00, "record RAM")
    syms["record_image"] = rec_image

    words = [a for a in range(BASE, BASE + len(panel) - 3, 4) if u32(panel, a) == P.RECORDS]
    check(len(words) == P.RECORD_LITERALS, "record table literals")
    for a in words:
        struct.pack_into("<I", img, a - BASE, P.NEW_RECORDS)
    words = [a for a in range(BASE, BASE + len(panel) - 3, 4) if u32(panel, a) == P.RECORD_204_MIN]
    check(len(words) == 1, "record 204 literal")
    struct.pack_into("<I", img, words[0] - BASE, P.NEW_RECORDS + P.RECORD_204_MIN - P.RECORDS)
    for a in P.RECORD_COUNTS:
        h1, h2 = struct.unpack_from("<HH", img, a - BASE)
        imm = (h1 & 0xF) << 12 | (h1 >> 10 & 1) << 11 | (h2 >> 12 & 7) << 8 | h2 & 0xFF
        check(h1 & 0xFBF0 == 0xF240 and imm == P.N_RECORDS, f"record count at {a:#x}")
        rd = h2 >> 8 & 0xF
        s = Asm(a)
        s.movw(rd, n_records)
        put(img, a, s.assemble())
    check(u32(img, P.SBRK_LIMIT) == 0x2001FFFC, "_sbrk limit")
    struct.pack_into("<I", img, P.SBRK_LIMIT - BASE, P.NEW_RECORDS - 4)

    # boot: copy the record image to RAM, then main()
    check(u32(img, P.MAIN_LITERAL) == P.MAIN, "reset handler main literal")
    s = Asm((at + 15) & ~15)
    s.mov32("r0", P.NEW_RECORDS)
    s.mov32("r1", rec_image)
    s.mov32("r2", rec_image + len(recs))
    s.label("copy")
    s.h(0xF851, 0x3B04)                                   # ldr.w r3,[r1],#4
    s.h(0xF840, 0x3B04)                                   # str.w r3,[r0],#4
    s.h(0x4291)                                           # cmp r1, r2
    s.bcond("ne", "copy")
    s.b(P.MAIN & ~1)
    init = emit(s.assemble(), 16)
    struct.pack_into("<I", img, P.MAIN_LITERAL - BASE, init | 1)
    syms["init"] = init

    # 2. menus: Param B after Contour in the Osc1 / Osc2 menus
    for osc, menu in P.MENUS.items():
        items = []
        a = menu
        while True:
            v, = struct.unpack_from("<H", img, a - BASE)
            if v == 0xFFFF:
                break
            items.append(v)
            a += 2
        contour = P.CORE_RECORDS[osc - 1] - 7                   # records 2 / 3
        check(items[:2] == [P.CORE_RECORDS[osc - 1], contour], f"Osc{osc} menu")
        items.insert(2, new_index[osc])
        check(2 * len(items) + 2 <= P.MENU_SLOT, "menu slot")
        put(img, menu, struct.pack(f"<{len(items)}H", *items) + b"\xff\xff")

    # 3. Core names and per-Core Contour names for Cores 56-95
    core_names = get(img, P.CORE_NAMES, 17 * N_CARBON_CORES)
    contour_names = get(img, P.CONTOUR_NAMES, 13 * N_CARBON_CORES)
    for k in range(len(COBALT_NAMES)):
        name = get(cobalt_panel, CobaltPanel.CORE_NAMES + 17 * k, 17)
        check(name.split(b"\0")[0].decode() == COBALT_NAMES[k], f"Cobalt name {k}")
        core_names += name
        contour_names += get(cobalt_panel, CobaltPanel.PARAM_NAMES + 26 * k, 13)
    syms["core_names"] = emit(core_names)
    syms["contour_names"] = emit(contour_names)
    check(u32(img, P.CORE_NAME_LITERAL) == P.CORE_NAMES, "Core name literal")
    struct.pack_into("<I", img, P.CORE_NAME_LITERAL - BASE, syms["core_names"])
    for a in P.CONTOUR_NAME_LITERALS:
        check(u32(img, a) == P.CONTOUR_NAMES, "Contour name literal")
        struct.pack_into("<I", img, a - BASE, syms["contour_names"])
    expect(img, P.CORE_NAME_CLAMP, "3729a8bf3723", "Core name clamp")
    put(img, P.CORE_NAME_CLAMP, bytes([MAX_CORE, 0x29]))
    put(img, P.CORE_NAME_CLAMP + 4, bytes([MAX_CORE, 0x23]))
    for a in P.CORE_SETTER_CLAMPS:
        expect(img, a, "372e", "panel Core clamp")
        expect(img, a + 6, "a8bf3726", "panel Core clamp")
        put(img, a, bytes([MAX_CORE, 0x2E]))
        put(img, a + 8, bytes([MAX_CORE, 0x26]))

    # the Param B title is the algorithm's own name for it (Spread, Sub, Asym, ...) on Cores 56-95
    b_names = b"".join(get(cobalt_panel, CobaltPanel.PARAM_NAMES + 26 * k + 13, 13) for k in range(len(COBALT_NAMES)))
    syms["param_b_names"] = emit(b_names)
    expect(img, P.TITLE_NAME, "09eb41110931", "parameter title")
    s = Asm((at + 15) & ~15)
    for osc in (1, 2):
        s.movw("r2", new_index[osc])
        s.h(0x4291)                                       # cmp r1, r2
        s.bcond("eq", f"osc{osc}")
    s.h(0xEB09, 0x1141)                                   # add.w r1, sb, r1, lsl #5
    s.b(P.TITLE_NAME_RESUME)
    for osc in (1, 2):
        s.label(f"osc{osc}")
        s.mov32("r3", P.PATCH)
        s.ldrb("r3", "r3", 0x30 + osc)                    # Core
        s.cmp("r3", N_CARBON_CORES)
        s.bcond("ge", f"cobalt{osc}")
        s.h(0xEB09, 0x1141)                               # add.w r1, sb, r1, lsl #5
        s.b(P.TITLE_NAME_RESUME)
        s.label(f"cobalt{osc}")
        s.h(0x3B00 | N_CARBON_CORES)                      # subs r3, #56
        s.h(0xEB03, 0x0243)                               # add.w r2, r3, r3, lsl #1    (3k)
        s.h(0xEB03, 0x0282)                               # add.w r2, r3, r2, lsl #2    (13k)
        s.mov32("r1", syms["param_b_names"])
        s.h(0x4411)                                       # add r1, r2
        s.b(P.TITLE_NAME_COPY)
    syms["title_stub"] = emit(s.assemble(), 16)
    put(img, P.TITLE_NAME, thumb.enc_b_w(P.TITLE_NAME, syms["title_stub"]))

    # 4. ID switches: 0xB4 / 0xB7 behave like an ordinary 0-127 parameter at +0x66 / +0x67
    for sw in P.ID_SWITCHES:
        rn, default, disp, kind, table = id_switch(img, sw)
        template = id_case(img, kind, table, P.TEMPLATE_ID)
        for pid in ID_PARAM_B:
            check(id_case(img, kind, table, pid) == default, f"switch {sw:#x}: ID {pid:#x} in use")
        s = Asm((at + 15) & ~15)
        if kind == "tbh":
            for pid in ID_PARAM_B:
                s.cmp(rn, pid - 3)
                s.bcond("eq", f"id{pid:x}")
            s.cmp(rn, 0xC3)
            s.bcond("hi", default)
            s.b(disp)
        for pid, off in zip(ID_PARAM_B, OFF_PARAM_B):
            s.label(f"id{pid:x}")
            clone_case(img, template, s, off)
        stub = s.assemble()
        stub_at = emit(stub, 16)
        if kind == "tbh":
            put(img, sw, thumb.enc_b_w(sw, stub_at) + b"\x00\xbf")
        else:
            for pid in ID_PARAM_B:
                struct.pack_into("<I", img, table + 4 * (pid - 3) - BASE, s.labels[f"id{pid:x}"] | 1)
        syms[f"switch_{sw:x}"] = stub_at

    # 5. Patch+Level1 / Patch+Level2 turn Param B, like the stock Patch+XMod (Free Run) case
    for a, level_id, pid, off in zip(P.LEVEL_KNOBS, P.LEVEL_IDS, ID_PARAM_B, OFF_PARAM_B):
        h, = struct.unpack_from("<H", img, a - BASE)
        check(h >> 8 == 0x4B and u32(img, ((a + 4) & ~3) + (h & 0xFF) * 4) == P.UI, f"Level knob at {a:#x}")
        expect(img, a + 2, "1b681b78002b", f"Level knob at {a:#x}")
        level = thumb.decode_branch(*struct.unpack_from("<HH", img, a + 8 - BASE), a + 8)
        expect(img, level + 8, bytes([0x4F, 0xF0, level_id, 0x0A]).hex(), f"Level knob at {a:#x}: ID")  # mov.w r10,#id
        s = Asm((at + 15) & ~15)
        s.mov32("r3", P.UI)
        s.ldrb("r2", "r3", P.UI_PATCH_HELD)
        s.cmp("r2", 0)
        s.bcond("ne", "patch")
        s.ldr("r3", "r3")
        s.b(a + 4)
        s.label("patch")
        s.mov32("r3", P.PATCH)
        s.ldrb("r9", "r3", off)
        s.mov32("r3", P.PATCH_USED)
        s.movs("r0", 1)
        s.strb("r0", "r3")
        s.movs("r0", 127)
        s.movw("r10", pid)
        s.b(P.KNOB_SET)
        stub_at = emit(s.assemble(), 16)
        put(img, a, thumb.enc_b_w(a, stub_at))
        syms[f"level_knob_{pid:x}"] = stub_at

    syms["end"] = at
    img.extend(b"\xff" * ((-len(img)) % PAGE))
    return bytes(img), syms


# -------------------------------------------------------------------------------------------- I/O

def load_component(src_dir, model, role):
    path = os.path.join(src_dir, FILES[model].format(role))
    if not os.path.exists(path):
        sys.exit(f"missing {path}")
    dev, tgt, image, bad = modal_fw.decode(open(path, "rb").read())
    if bad:
        sys.exit(f"{path}: checksum errors in blocks {bad[:10]}")
    if hashlib.sha256(image).hexdigest() != SHA256[(model, role)]:
        sys.exit(f"{path}: not the stock {model.title()}8 {role} this tool was mapped against (SHA-256 mismatch)")
    return image


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="write a patched Carbon8 set")
    b.add_argument("carbon_dir")
    b.add_argument("cobalt_dir")
    b.add_argument("out_dir")
    sub.add_parser("cores", help="list the Core numbers of the Cobalt algorithms")
    a = ap.parse_args()
    if a.cmd == "cores":
        for i, n in enumerate(COBALT_NAMES):
            print(f"{N_CARBON_CORES + i:3d}  {n}")
        return
    carbon = {r: load_component(a.carbon_dir, "carbon", r) for r in ("panel", "master", "slave")}
    cobalt = {r: load_component(a.cobalt_dir, "cobalt", r) for r in ("panel", "master", "slave")}
    slave, _ = patch_slave(carbon["slave"], cobalt["slave"])
    master, _ = patch_master(carbon["master"], cobalt["master"])
    panel, _ = patch_panel(carbon["panel"], cobalt["panel"])
    out = {"slave": slave, "master": master, "panel": panel}
    os.makedirs(a.out_dir, exist_ok=True)
    for role, image in out.items():
        path = os.path.join(a.out_dir, FILES["carbon"].format(role))
        with open(path, "wb") as f:
            f.write(modal_fw.encode(image, CARBON_DEV, TARGET[role]))
        print(f"{path}: {len(image)} bytes")


if __name__ == "__main__":
    main()
