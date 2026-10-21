#!/usr/bin/env python3
"""Compile cobalt_glue.c and store the object as cobalt_glue.json for carbon_cobalt.py.

The JSON keeps the build tool free of a compiler dependency: it holds .text as hex, the .bss size,
the defined symbols and the relocations. Rerun this after editing cobalt_glue.c:

    python3 tools/glue/make_glue.py            # uses `clang` from PATH
    python3 tools/glue/make_glue.py --cc /path/to/clang

Any clang with the ARM backend works (Apple's command-line tools clang does).
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import thumb  # noqa: E402

CFLAGS = ["--target=thumbv7em-none-eabihf", "-mcpu=cortex-m7", "-mfpu=fpv5-sp-d16", "-mfloat-abi=hard",
          "-O2", "-ffreestanding", "-fno-builtin", "-fno-common", "-fno-pic", "-fno-unwind-tables",
          "-fno-asynchronous-unwind-tables", "-Wall", "-Wextra", "-Werror"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cc", default="clang")
    a = ap.parse_args()
    src = os.path.join(HERE, "cobalt_glue.c")
    with tempfile.TemporaryDirectory() as tmp:
        obj_path = os.path.join(tmp, "glue.o")
        subprocess.run([a.cc, *CFLAGS, "-c", src, "-o", obj_path], check=True)
        obj = thumb.read_elf_object(open(obj_path, "rb").read())
    secs = obj["sections"]
    extra = [n for n, (t, _, size) in secs.items()
             if size and t in (1, 8) and n not in (".text", ".bss") and not n.startswith((".comment", ".note"))]
    if extra:
        sys.exit(f"unexpected sections {extra}: the glue must only use .text and .bss")
    out = {
        "source": "cobalt_glue.c",
        "cflags": CFLAGS,
        "text": secs[".text"][1].hex(),
        "bss_size": secs[".bss"][2] if ".bss" in secs else 0,
        "symbols": [[n, s, v] for n, s, v in obj["symbols"] if n and s in (".text", ".bss")],
        "relocs": [list(r) for r in obj["relocs"].get(".text", [])],
    }
    path = os.path.join(HERE, "cobalt_glue.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=0)
        f.write("\n")
    print(f"{path}: .text {len(secs['.text'][1])} bytes, .bss {out['bss_size']} bytes, "
          f"{len(out['relocs'])} relocations")


if __name__ == "__main__":
    main()
