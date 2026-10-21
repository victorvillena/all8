"""Small Thumb-2 toolkit for patching Modal firmware images (Cortex-M4/M7, loaded at 0x410000).

* `Asm`: a minimal assembler for the instruction forms the patch stubs need (branches with labels,
  MOVW/MOVT constants, byte/word loads and stores, compares, push/pop).
* `read_elf_object` / `link_object`: load a relocatable ELF object made by clang
  (`--target=thumbv7em-none-eabihf -c`) and place its .text and .bss at fixed addresses, resolving
  undefined symbols from a dict. Only the relocation types clang emits for such code are handled.
"""
import struct

REGS = {f"r{i}": i for i in range(13)}
REGS.update(sb=9, sl=10, fp=11, ip=12, sp=13, lr=14, pc=15)
CONDS = dict(eq=0, ne=1, hs=2, cs=2, lo=3, cc=3, mi=4, pl=5, vs=6, vc=7,
             hi=8, ls=9, ge=10, lt=11, gt=12, le=13)


def reg(r):
    return REGS[r] if isinstance(r, str) else r


def _hw(*halves):
    return b"".join(struct.pack("<H", h & 0xFFFF) for h in halves)


def enc_b_w(src, dst):
    """B.W (T4) at src to dst."""
    off = dst - (src + 4)
    if off & 1 or not -(1 << 24) <= off < (1 << 24):
        raise ValueError(f"b.w {src:#x} -> {dst:#x} out of range")
    s = off >> 24 & 1
    i1, i2 = off >> 23 & 1, off >> 22 & 1
    j1, j2 = (1 - i1) ^ s, (1 - i2) ^ s
    return _hw(0xF000 | s << 10 | off >> 12 & 0x3FF, 0x9000 | j1 << 13 | j2 << 11 | off >> 1 & 0x7FF)


def enc_bl(src, dst):
    """BL (T1) at src to the Thumb function at dst."""
    off = (dst & ~1) - (src + 4)
    if not -(1 << 24) <= off < (1 << 24):
        raise ValueError(f"bl {src:#x} -> {dst:#x} out of range")
    s = off >> 24 & 1
    i1, i2 = off >> 23 & 1, off >> 22 & 1
    j1, j2 = (1 - i1) ^ s, (1 - i2) ^ s
    return _hw(0xF000 | s << 10 | off >> 12 & 0x3FF, 0xD000 | j1 << 13 | j2 << 11 | off >> 1 & 0x7FF)


def enc_bcond_w(cond, src, dst):
    """B<cond>.W (T3)."""
    off = dst - (src + 4)
    if off & 1 or not -(1 << 20) <= off < (1 << 20):
        raise ValueError(f"b{cond}.w {src:#x} -> {dst:#x} out of range")
    s = off >> 20 & 1
    j2, j1 = off >> 19 & 1, off >> 18 & 1
    return _hw(0xF000 | s << 10 | CONDS[cond] << 6 | off >> 12 & 0x3F,
               0x8000 | j1 << 13 | j2 << 11 | off >> 1 & 0x7FF)


def decode_branch(hw1, hw2, src):
    """Target of a 32-bit B.W / BL / B<cond>.W at src, or None."""
    if hw1 >> 11 != 0b11110 or not hw2 & 0x8000:
        return None
    s = hw1 >> 10 & 1
    j1, j2 = hw2 >> 13 & 1, hw2 >> 11 & 1
    if hw2 & 0x1000:                                   # B.W T4 / BL T1
        i1, i2 = 1 - (j1 ^ s), 1 - (j2 ^ s)
        off = s << 24 | i1 << 23 | i2 << 22 | (hw1 & 0x3FF) << 12 | (hw2 & 0x7FF) << 1
        off -= s << 25
    else:                                              # B<cond>.W T3
        off = s << 20 | j2 << 19 | j1 << 18 | (hw1 & 0x3F) << 12 | (hw2 & 0x7FF) << 1
        off -= s << 21
    return src + 4 + off


class Asm:
    """Assemble a short Thumb-2 routine at a known address. Branch targets may be label names
    (resolved at the end) or absolute addresses."""

    def __init__(self, addr):
        self.addr = addr
        self.items = []          # (kind, args)
        self.labels = {}

    @property
    def pc(self):
        return self.addr + sum(self._size(k, a) for k, a in self.items)

    @staticmethod
    def _size(kind, args):
        return len(args[0]) if kind == "raw" else 4 if kind in ("b", "bl", "bc") else 2 if kind == "bcn" else 0

    def label(self, name):
        self.labels[name] = self.pc

    def raw(self, data):
        self.items.append(("raw", (bytes(data),)))

    def h(self, *halves):
        self.raw(_hw(*halves))

    # --- control flow ---
    def b(self, target):
        self.items.append(("b", (target,)))

    def bl(self, target):
        self.items.append(("bl", (target,)))

    def bcond(self, cond, target):
        self.items.append(("bc", (cond, target)))

    def bx(self, r):
        self.h(0x4700 | reg(r) << 3)

    def blx(self, r):
        self.h(0x4780 | reg(r) << 3)

    def push(self, regs):
        regs = [reg(r) for r in regs]
        if all(r < 8 or r == 14 for r in regs):
            self.h(0xB400 | (0x100 if 14 in regs else 0) | sum(1 << r for r in regs if r < 8))
        else:
            self.h(0xE92D, sum(1 << r for r in regs))

    def pop(self, regs):
        regs = [reg(r) for r in regs]
        if all(r < 8 or r == 15 for r in regs):
            self.h(0xBC00 | (0x100 if 15 in regs else 0) | sum(1 << r for r in regs if r < 8))
        else:
            self.h(0xE8BD, sum(1 << r for r in regs))

    # --- data ---
    def movw(self, rd, imm):
        rd = reg(rd)
        self.h(0xF240 | (imm >> 11 & 1) << 10 | imm >> 12 & 0xF,
               (imm >> 8 & 7) << 12 | rd << 8 | imm & 0xFF)

    def movt(self, rd, imm):
        rd = reg(rd)
        self.h(0xF2C0 | (imm >> 11 & 1) << 10 | imm >> 12 & 0xF,
               (imm >> 8 & 7) << 12 | rd << 8 | imm & 0xFF)

    def mov32(self, rd, value):
        self.movw(rd, value & 0xFFFF)
        if value >> 16:
            self.movt(rd, value >> 16)

    def mov(self, rd, rm):
        rd, rm = reg(rd), reg(rm)
        self.h(0x4600 | (rd >> 3) << 7 | rm << 3 | rd & 7)

    def movs(self, rd, imm):
        assert reg(rd) < 8 and 0 <= imm < 256
        self.h(0x2000 | reg(rd) << 8 | imm)

    def cmp(self, rn, imm):
        rn = reg(rn)
        if rn < 8 and 0 <= imm < 256:
            self.h(0x2800 | rn << 8 | imm)
        else:
            assert 0 <= imm < 256
            self.h(0xF1B0 | rn, 0x0F00 | imm)

    def _ldst_imm12(self, op, rt, rn, imm):
        assert 0 <= imm < 4096
        self.h(op | reg(rn), reg(rt) << 12 | imm)

    def ldrb(self, rt, rn, imm=0):
        self._ldst_imm12(0xF890, rt, rn, imm)

    def strb(self, rt, rn, imm=0):
        self._ldst_imm12(0xF880, rt, rn, imm)

    def ldr(self, rt, rn, imm=0):
        self._ldst_imm12(0xF8D0, rt, rn, imm)

    def str(self, rt, rn, imm=0):
        self._ldst_imm12(0xF8C0, rt, rn, imm)

    def ldr_lsl2(self, rt, rn, rm):
        self.h(0xF850 | reg(rn), reg(rt) << 12 | 2 << 4 | reg(rm))

    def str_lsl2(self, rt, rn, rm):
        self.h(0xF840 | reg(rn), reg(rt) << 12 | 2 << 4 | reg(rm))

    def nop(self):
        self.h(0xBF00)

    def assemble(self):
        out = bytearray()
        for kind, args in self.items:
            pc = self.addr + len(out)
            if kind == "raw":
                out += args[0]
                continue
            tgt = args[-1]
            tgt = self.labels[tgt] if isinstance(tgt, str) else tgt
            if kind == "b":
                out += enc_b_w(pc, tgt)
            elif kind == "bl":
                out += enc_bl(pc, tgt)
            else:
                out += enc_bcond_w(args[0], pc, tgt)
        return bytes(out)


# ---------------------------------------------------------------- ELF relocatable objects

R_ARM_ABS32, R_ARM_THM_CALL, R_ARM_THM_JUMP24 = 2, 10, 30
R_ARM_THM_MOVW_ABS_NC, R_ARM_THM_MOVT_ABS, R_ARM_PREL31 = 47, 48, 42


def read_elf_object(data):
    """Return {'sections': {name: (type, bytes, size)}, 'symbols': [(name, shndx_name, value)],
    'relocs': {section: [(offset, type, symbol_name, symbol_section, symbol_value)]}}."""
    if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        raise ValueError("not a little-endian ELF32 object")
    e_shoff, = struct.unpack_from("<I", data, 0x20)
    e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", data, 0x2E)
    shdrs = [struct.unpack_from("<10I", data, e_shoff + i * e_shentsize) for i in range(e_shnum)]
    strtab = shdrs[e_shstrndx]

    def cstr(tab, off):
        start = tab[4] + off
        return data[start:data.index(b"\0", start)].decode()

    names = [cstr(strtab, s[0]) for s in shdrs]
    sections = {}
    for n, s in zip(names, shdrs):
        sh_type, size = s[1], s[5]
        body = b"" if sh_type == 8 else data[s[4]:s[4] + size]   # SHT_NOBITS
        sections[n] = (sh_type, body, size)
    symtab_i = next(i for i, s in enumerate(shdrs) if s[1] == 2)
    sym, symstr = shdrs[symtab_i], shdrs[shdrs[symtab_i][6]]
    symbols = []
    for k in range(sym[5] // 16):
        st_name, st_value, st_size, st_info, st_other, st_shndx = struct.unpack_from(
            "<IIIBBH", data, sym[4] + k * 16)
        sec = names[st_shndx] if 0 < st_shndx < len(names) else None
        symbols.append((cstr(symstr, st_name), sec, st_value))
    relocs = {}
    for n, s in zip(names, shdrs):
        if s[1] != 9:                                                # SHT_REL
            continue
        target = names[s[7]]
        lst = relocs.setdefault(target, [])
        for k in range(s[5] // 8):
            r_offset, r_info = struct.unpack_from("<II", data, s[4] + k * 8)
            name, sec, value = symbols[r_info >> 8]
            lst.append((r_offset, r_info & 0xFF, name, sec, value))
    return {"sections": sections, "symbols": symbols, "relocs": relocs}


def _patch_movw_movt(buf, off, value):
    h1, h2 = struct.unpack_from("<HH", buf, off)
    h1 = h1 & ~0x040F | (value >> 11 & 1) << 10 | value >> 12 & 0xF
    h2 = h2 & ~0x70FF | (value >> 8 & 7) << 12 | value & 0xFF
    struct.pack_into("<HH", buf, off, h1, h2)


def link_object(obj, text_addr, bss_addr, externs):
    """Place .text at text_addr and .bss at bss_addr. Returns (text bytes, {global symbol: address})."""
    secs = obj["sections"]
    text = bytearray(secs[".text"][1])
    base = {".text": text_addr, ".bss": bss_addr}
    for n in secs:
        if n not in base and secs[n][0] in (1, 8) and secs[n][2] and n.startswith((".data", ".rodata")):
            raise ValueError(f"unsupported section {n}: keep the glue to .text and .bss")
    addrs = {}
    for name, sec, value in obj["symbols"]:
        if name and sec in base:
            addrs[name] = base[sec] + value
    for off, rtype, name, sec, value in obj["relocs"].get(".text", []):
        if sec in base:
            S = base[sec] + value
        elif sec is None and name in externs:
            S = externs[name]
        else:
            raise KeyError(f"unresolved symbol {name!r}")
        P = text_addr + off
        if rtype == R_ARM_THM_MOVW_ABS_NC:
            h1, h2 = struct.unpack_from("<HH", text, off)
            addend = ((h1 >> 10 & 1) << 11 | (h1 & 0xF) << 12 | (h2 >> 12 & 7) << 8 | h2 & 0xFF)
            _patch_movw_movt(text, off, (S + addend) & 0xFFFF)
        elif rtype == R_ARM_THM_MOVT_ABS:
            h1, h2 = struct.unpack_from("<HH", text, off)
            addend = ((h1 >> 10 & 1) << 11 | (h1 & 0xF) << 12 | (h2 >> 12 & 7) << 8 | h2 & 0xFF)
            addend = addend - 0x10000 if addend & 0x8000 else addend
            _patch_movw_movt(text, off, (S + addend) >> 16 & 0xFFFF)
        elif rtype == R_ARM_ABS32:
            A, = struct.unpack_from("<I", text, off)
            struct.pack_into("<I", text, off, (S + A) & 0xFFFFFFFF)
        elif rtype in (R_ARM_THM_CALL, R_ARM_THM_JUMP24):
            text[off:off + 4] = (enc_bl if rtype == R_ARM_THM_CALL else enc_b_w)(P, S & ~1)
        else:
            raise ValueError(f"unsupported relocation type {rtype} at .text+{off:#x}")
    return bytes(text), addrs
