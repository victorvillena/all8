#!/usr/bin/env python3
"""Change the name on the panel's boot screen (Argon8 3.2, Cobalt8 2.1, Carbon8 1.1 panels).

The boot screen is drawn by one routine, the same in all three panels:

      CARBON8          name ("CARBON") in a large font, then the variant suffix ("8", "8M" or "8X",
       v1.1            from the user signature) in a second font, centred together; below it the
        --             version in a small font, and a dash that runs along the bottom while booting.

Patches (all reversible by flashing the stock panel):
  --name      the two `add rX, sp, #0x24` that pass the stack copy of the name become `ldr rX, =name`
              (reusing the name's own literal, repointed at the new string).
  --suffix    the same for the suffix's `add rX, sp, #0x1c`, through the "8M" literal. It replaces all
              three variants. An empty suffix removes it, and the name is then centred alone.
  --subtitle  the version format "v%d.%0d" becomes "<subtitle> v%d.%0d" (one centred line).
  --name also renames the Settings "Version" entry ("CARBON8 1.1" -> "<name><suffix> 1.1").
New strings go after the end of the panel image, which is then padded to whole 512-byte pages. The tool
works on stock panels and on panels already patched by the other tools here (or by itself).

Usage:
  boot_screen.py show  <set dir> [--png FILE]
  boot_screen.py build <set dir> <out dir> [--name NAME] [--suffix TEXT] [--subtitle TEXT] [--png FILE]
"""
import argparse
import glob
import os
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import modal_fw  # noqa: E402

BASE = modal_fw.LOAD_ADDRESS
PAGE = 512
W, H = 128, 64
MENU_MAX = 13                              # longest value string the parameter display shows ("Clocked Noise")


class Model:
    def __init__(self, name, version, end, name_sites, suffix_sites, fonts, name_lit, suffix_lit, fmt_lit,
                 fmt_buf, menu_lit, stock):
        self.name, self.version, self.end = name, version, end
        self.name_sites = name_sites       # add r1, sp, #0x24 (width) / add r3, sp, #0x24 (draw)
        self.suffix_sites = suffix_sites   # add r1, sp, #0x1c / add r3, sp, #0x1c
        self.fonts = fonts                 # literals holding the name, suffix and version fonts
        self.name_lit, self.suffix_lit, self.fmt_lit, self.menu_lit = name_lit, suffix_lit, fmt_lit, menu_lit
        self.fmt_buf = fmt_buf             # stack buffer the version line is sprintf'd into
        self.stock = stock                 # stock values of the four literals


MODELS = [
    Model("Argon8", (3, 2), 0x456E00, (0x41C15A, 0x41C1AE), (0x41C168, 0x41C1C2), (0x41C394, 0x41C398, 0x41C39C),
          0x41C388, 0x41C3D4, 0x41C3A8, 0x54 - 0x38, 0x41EE64,
          {"name": 0x449524, "suffix": 0x449580, "fmt": 0x449514, "menu": 0x44A534}),
    Model("Cobalt8", (2, 1), 0x44BE80, (0x41B97E, 0x41B9D2), (0x41B98C, 0x41B9E6), (0x41BBC4, 0x41BBC8, 0x41BBCC),
          0x41BBB8, 0x41BBFC, 0x41BBD8, 0x4C - 0x34, 0x41E01C,
          {"name": 0x44585C, "suffix": 0x44584C, "fmt": 0x445854, "menu": 0x446780}),
    Model("Carbon8", (1, 1), 0x44CD80, (0x41AC06, 0x41AC5A), (0x41AC14, 0x41AC6E), (0x41AE40, 0x41AE44, 0x41AE48),
          0x41AE34, 0x41AE80, 0x41AE54, 0x54 - 0x38, 0x41CCF8,
          {"name": 0x4465DC, "suffix": 0x4465C4, "fmt": 0x4465CC, "menu": 0x4475F4}),
]


def check(cond, what):
    if not cond:
        raise RuntimeError(f"unexpected firmware contents: {what}")


def u32(img, addr):
    return struct.unpack_from("<I", img, addr - BASE)[0]


def u16(img, addr):
    return struct.unpack_from("<H", img, addr - BASE)[0]


def cstr(img, addr):
    off = addr - BASE
    return bytes(img[off:img.index(0, off)]).decode("latin-1")


def add_sp(rd, imm):
    return 0xA800 | rd << 8 | imm >> 2


def ldr_lit(rd, site, lit):
    off = lit - ((site + 4) & ~3)
    assert 0 <= off <= 1020 and off % 4 == 0
    return 0x4800 | rd << 8 | off >> 2


def site_state(img, sites, imm, lit):
    """'stock' if both sites still pass the stack copy, 'patched' if both load `lit`."""
    stock = [add_sp(rd, imm) for rd in (1, 3)]
    patched = [ldr_lit(rd, s, lit) for rd, s in zip((1, 3), sites)]
    now = [u16(img, s) for s in sites]
    if now == stock:
        return "stock"
    if now == patched:
        return "patched"
    raise RuntimeError(f"unexpected firmware contents: boot screen code at {sites[0]:#x}")


def identify(img):
    for m in MODELS:
        if all(len(img) >= m.end - BASE and u32(img, lit) in (v, *range(m.end, BASE + len(img)))
               for lit, v in [(m.name_lit, m.stock["name"]), (m.fmt_lit, m.stock["fmt"]),
                              (m.suffix_lit, m.stock["suffix"]), (m.menu_lit, m.stock["menu"])]):
            try:
                site_state(img, m.name_sites, 0x24, m.name_lit)
                site_state(img, m.suffix_sites, 0x1C, m.suffix_lit)
            except RuntimeError:
                continue
            return m
    raise RuntimeError("not an Argon8 3.2, Cobalt8 2.1 or Carbon8 1.1 panel")


# -------------------------------------------------------------------------------------- u8g2 fonts

class Font:
    """A u8g2 font in the image (23-byte header, then RLE glyphs)."""

    def __init__(self, img, addr):
        self.img, self.addr = img, addr
        h = img[addr - BASE:addr - BASE + 23]
        self.bits = h[2:9]                 # 0-run, 1-run, width, height, x, y, delta x
        self.max_height = h[10]
        self.upper, self.lower = h[17] << 8 | h[18], h[19] << 8 | h[20]

    def glyph(self, ch):
        p = self.addr - BASE + 23
        p += self.lower if ch >= ord("a") else self.upper if ch >= ord("A") else 0
        while self.img[p + 1]:
            if self.img[p] == ch:
                return self._decode(p + 2)
            p += self.img[p + 1]
        return None

    def _decode(self, p):
        state = [p, 0]

        def bits(n):
            v = 0
            for k in range(n):
                v |= (self.img[state[0]] >> state[1] & 1) << k
                state[1] += 1
                if state[1] == 8:
                    state[0], state[1] = state[0] + 1, 0
            return v

        def signed(n):
            return bits(n) - (1 << n - 1)

        b0, b1, bw, bh, bx, by, bd = self.bits
        w, h, x, y, dx = bits(bw), bits(bh), signed(bx), signed(by), signed(bd)
        pixels = []
        while w and len(pixels) < w * h:
            a, b = bits(b0), bits(b1)
            while True:
                pixels += [0] * a + [1] * b
                if not bits(1):
                    break
        return dict(w=w, h=h, x=x, y=y, dx=dx, rows=[pixels[r * w:(r + 1) * w] for r in range(h)])

    def missing(self, text):
        return sorted({c for c in text if self.glyph(ord(c)) is None})

    def width(self, text):
        """u8g2_GetStrWidth: the advances, except the last glyph counts its ink width."""
        w = 0
        g = None
        for c in text:
            g = self.glyph(ord(c))
            w += g["dx"]
        if g and g["w"]:
            w += g["w"] + g["x"] - g["dx"]
        return w & 0xFF

    def draw(self, screen, x, y, text):
        for c in text:
            g = self.glyph(ord(c))
            top = y - (g["h"] + g["y"])
            for r, row in enumerate(g["rows"]):
                for col, on in enumerate(row):
                    px, py = x + g["x"] + col, top + r
                    if on and 0 <= px < W and 0 <= py < H:
                        screen[py][px] = 1
            x += g["dx"]


# ------------------------------------------------------------------------------------- the screen

def boot_text(img, m):
    """(name, suffix, version line) as the panel would show them on a plain "8" unit."""
    def text(sites, imm, lit, stack):
        return cstr(img, u32(img, lit)) if site_state(img, sites, imm, lit) == "patched" else stack
    name = text(m.name_sites, 0x24, m.name_lit, cstr(img, u32(img, m.name_lit)))
    suffix = text(m.suffix_sites, 0x1C, m.suffix_lit, "8")
    version = cstr(img, u32(img, m.fmt_lit)).replace("%0d", "%d") % m.version
    return name, suffix, version


def layout(img, m, name, suffix, version):
    """Positions as computed by the boot routine; raises if something doesn't fit."""
    big, mid, small = (Font(img, u32(img, lit)) for lit in m.fonts)
    for font, s, what in [(big, name, "name"), (mid, suffix, "suffix"), (small, version, "subtitle")]:
        if font.missing(s):
            sys.exit(f"{what} uses characters the boot font doesn't have: {''.join(font.missing(s))!r}")
    wn, ws, wv = big.width(name), mid.width(suffix), small.width(version)
    y_name = (mid.max_height >> 1) - small.max_height + 0x22
    x_name = 63 - ((ws >> 1) + (wn >> 1))
    x_suffix = x_name + wn + 2
    if x_name < 0 or x_suffix + ws > W:
        sys.exit(f"name + suffix are {wn + 2 + ws} px wide; the screen is {W} px")
    if wv > W:
        sys.exit(f"version line {version!r} is {wv} px wide; the screen is {W} px")
    return [(big, x_name, y_name, name), (mid, x_suffix, y_name, suffix),
            (small, 64 - (wv >> 1), y_name + small.max_height, version)]


def render(items):
    screen = [[0] * W for _ in range(H)]
    for font, x, y, s in items:
        font.draw(screen, x, y, s)
    for x in range(59, 70):                # the boot progress dash (moves along y = 60)
        screen[60][x] = 1
    return screen


def print_screen(screen):
    print("┌" + "─" * W + "┐")
    for y in range(0, H, 2):
        print("│" + "".join(" ▀▄█"[screen[y][x] | screen[y + 1][x] << 1] for x in range(W)) + "│")
    print("└" + "─" * W + "┘")


def write_png(path, screen, scale=4):
    fg, bg = b"\xf0\xf0\xf0", b"\x08\x08\x08"
    raw = b"".join(b"\0" + b"".join((fg if px else bg) * scale for px in row) for row in screen
                   for _ in range(scale))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W * scale, H * scale, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    open(path, "wb").write(png)


# -------------------------------------------------------------------------------------- patching

def patch(img, m, name=None, suffix=None, subtitle=None):
    img = bytearray(img)

    def emit(s):
        img.extend(bytes(-len(img) % 4))   # the name is read with ldm, so keep strings word aligned
        addr = BASE + len(img)
        data = s.encode("ascii") + b"\0"
        img.extend(data + bytes(-len(data) % 4 + 4))  # >= 8 bytes: the stack copy still reads 7-8
        return addr

    def repoint(sites, imm, lit, s):
        site_state(img, sites, imm, lit)
        struct.pack_into("<I", img, lit - BASE, emit(s))
        for rd, site in zip((1, 3), sites):
            struct.pack_into("<H", img, site - BASE, ldr_lit(rd, site, lit))

    cur_name, cur_suffix, _ = boot_text(img, m)
    if name is not None:
        repoint(m.name_sites, 0x24, m.name_lit, name)
    if suffix is not None:
        repoint(m.suffix_sites, 0x1C, m.suffix_lit, suffix)
    if subtitle is not None:
        fmt = f"{subtitle} v%d.%0d"
        if len(fmt.replace("%0d", "%d") % m.version) + 1 > m.fmt_buf:
            sys.exit(f"subtitle too long: the version line must fit {m.fmt_buf - 1} characters")
        struct.pack_into("<I", img, m.fmt_lit - BASE, emit(fmt))
    layout(img, m, *boot_text(img, m))     # exits if it doesn't fit
    if name is not None or suffix is not None:
        menu = f"{cur_name if name is None else name}{cur_suffix if suffix is None else suffix} %d.%0d"
        if len(menu.replace("%0d", "%d") % m.version) > MENU_MAX:
            print(f"note: Settings > Version left unchanged ({menu!r} is over {MENU_MAX} characters)")
        else:
            struct.pack_into("<I", img, m.menu_lit - BASE, emit(menu))
    img.extend(b"\xff" * (-len(img) % PAGE))
    return bytes(img)


# -------------------------------------------------------------------------------------------- I/O

def find_set(src_dir):
    files = {}
    for role in modal_fw.TARGETS.values():
        found = glob.glob(os.path.join(src_dir, f"*_{role}_*.syx"))
        if len(found) != 1:
            sys.exit(f"{src_dir}: expected one *_{role}_*.syx, found {len(found)}")
        files[role] = found[0]
    return files


def load_panel(path):
    dev, tgt, image, bad = modal_fw.decode(open(path, "rb").read())
    if bad:
        sys.exit(f"{path}: checksum errors in blocks {bad[:10]}")
    return dev, tgt, image


def show(img, m, png):
    items = layout(img, m, *boot_text(img, m))
    screen = render(items)
    print_screen(screen)
    if png:
        write_png(png, screen)
        print(f"{png}: preview")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show", help="draw a set's boot screen")
    s.add_argument("set_dir")
    s.add_argument("--png", help="also write a 4x PNG preview")
    b = sub.add_parser("build", help="write a copy of a set with a new boot screen")
    b.add_argument("set_dir")
    b.add_argument("out_dir")
    b.add_argument("--name", help="replaces the model name (CARBON, COBALT, ARGON)")
    b.add_argument("--suffix", help='replaces the "8"/"8M"/"8X" after the name; "" removes it')
    b.add_argument("--subtitle", help='text before the version: "<subtitle> v1.1"')
    b.add_argument("--png", help="also write a 4x PNG preview")
    a = ap.parse_args()

    files = find_set(a.set_dir)
    dev, tgt, panel = load_panel(files["panel"])
    m = identify(panel)
    if a.cmd == "show":
        print(f"{files['panel']}: {m.name} panel")
        show(panel, m, a.png)
        return

    if a.name == "":
        sys.exit("--name can't be empty")
    for text, what in [(a.name, "name"), (a.suffix, "suffix"), (a.subtitle, "subtitle")]:
        if text is not None and ("%" in text or not all(32 <= ord(c) < 127 for c in text)):
            sys.exit(f"{what}: printable ASCII only, no '%'")
    if a.name is None and a.suffix is None and a.subtitle is None:
        sys.exit("nothing to change: give --name, --suffix and/or --subtitle")
    panel = patch(panel, m, a.name, a.suffix, a.subtitle)
    os.makedirs(a.out_dir, exist_ok=True)
    for role, path in files.items():
        out = os.path.join(a.out_dir, os.path.basename(path))
        data = modal_fw.encode(panel, dev, tgt) if role == "panel" else open(path, "rb").read()
        if os.path.abspath(out) != os.path.abspath(path) or role == "panel":
            open(out, "wb").write(data)
        print(f"{out}: {len(panel)} bytes" if role == "panel" else f"{out}: copied")
    show(panel, m, a.png)


if __name__ == "__main__":
    main()
