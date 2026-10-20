# all8

Tools for building new features into Modal Electronics **Argon8 / Cobalt8 / Carbon8** firmware and
porting features between them. Reverse-engineering notes (formats, addresses, open questions) are
kept in [`NOTES.md`](NOTES.md).

Running one model's firmware on another model's hardware, and flashing in general, is handled by
the separate [crossflash-8](../../crossflash-8) project. This repo produces `.syx` sets that you
flash with its flasher.

> **Status:** custom wavetable builds have been flashed to an Argon8: bank names and bank switching
> work. The panel's wave display fix (below) has been checked offline only.

## Requirements

- Python 3.8+ (standard library only; `numpy` is used for faster resampling if installed)
- Your own official firmware `.syx` files, laid out as:

  ```
  firmwares/argon/Argon8_{panel,master,slave}_3-2.syx
  firmwares/cobalt/Cobalt8_{panel,master,slave}_2-1.syx
  firmwares/carbon/Carbon8_{panel,master,slave}_1-1.syx
  ```

  `firmwares/` is git-ignored. No firmware is distributed with this repo.
- For flashing: a checkout of crossflash-8 (Linux/ALSA is its verified path)

## Tools

### `tools/modal_fw.py`: firmware container codec

Converts between Modal's firmware SysEx files and raw ARM images (loaded at `0x00410000`).

```bash
# Summarize files: model, component, image size, reset vector, checksum and round-trip status
python3 tools/modal_fw.py info firmwares/*/*.syx

# Decode to a raw image; --elf also writes an ELF wrapper for disassembly
python3 tools/modal_fw.py decode firmwares/argon/Argon8_slave_3-2.syx slave.bin --elf slave.elf

# Re-encode an (edited) image. --dev is the model byte of the unit you will flash:
#   0x18 Argon8, 0x19 Cobalt8, 0x1D Carbon8 (it also selects the packet format the bootloader expects)
python3 tools/modal_fw.py encode slave.bin Argon8_slave_3-2.syx --dev 0x18 --target slave
```

Disassembling an image with the macOS system `objdump` (Apple LLVM):

```bash
objdump -d --triple=thumbv7em-none-eabihf --mcpu=cortex-m7 slave.elf  > slave.dis   # master / slave
objdump -d --triple=thumbv7em-none-eabihf --mcpu=cortex-m4 panel.elf  > panel.dis   # panel
```

The raw `.bin` also loads in Ghidra as `ARM:LE:32:v7` at base `0x00410000`. Ghidra misses some
functions in these images, so the objdump listing is useful for greps.

### `tools/argon_wavetables.py`: custom wavetables for Argon8 3.2

Replaces the 12 stored wavetable banks of the Argon8 slave DSP (banks `0x18`–`0x23`, 5 waves each)
with your own waves. The firmware band-limits every wave itself, so you only provide single cycles.
The tool only accepts the stock Argon8 3.2 slave image (SHA-256 check).

```bash
# Dump the stock banks as WAV files (one file per bank, 5 frames x 1024 samples, 16-bit),
# e.g. argon8_bank_0x18_Chew.wav
python3 tools/argon_wavetables.py extract firmwares/argon stock_waves/

# Build a flashable set with replaced banks
python3 tools/argon_wavetables.py build firmwares/argon out/ \
    --bank 0x18=my_table.wav --name "0x18=Glass Morph" \
    --bank 0x19=a.wav,b.wav,c.wav,d.wav,e.wav
```

Replaceable banks and their factory names:

| bank | 0x18 | 0x19 | 0x1A | 0x1B | 0x1C | 0x1D | 0x1E | 0x1F | 0x20 | 0x21 | 0x22 | 0x23 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| name | Chew | Grit | Gnar | Wicked | Transient | Feedback | FM Organ | Metal | Fat | Filter | Vsync | Retro 2 |

**Names.** `--name BANK=NAME` sets the name the panel shows for a replaced bank. Banks without a
name are called `Custom WT 1`, `Custom WT 2`, … in bank order, skipping any name already in use.
Rules:

- 1–13 characters, no leading/trailing spaces, only characters from the panel's name-entry set
  (`A–Z a–z 0–9`, space and `!?#&+-=,.':;<>@_()[]{}~`)
- unique among all 36 banks and Osc2's extra entries (PWM, Clocked Noise, Sine Noise, Square Noise),
  compared case-insensitively. A bank you replace frees its factory name for reuse.

The panel also draws the oscillator wave from its own small copy of every wave (128 points),
separate from what the slave plays. A build rewrites that copy for each replaced bank, so the
screen shows your waves. `--stock-names` keeps the factory names for replaced banks; the panel
still gets the new wave pictures.

`--bank BANK=…` accepts either:

- **one multi-frame WAV**: exactly 5 frames, or a wavetable of 1024- or 2048-sample frames (such as a
  Serum export). The tool takes 5 evenly spaced frames. Use `--frame-size N` for other frame sizes.
- **five single-cycle WAVs**, comma-separated, one per wave position.

Input may be 8/16/24/32-bit PCM or float WAV, mono or stereo (stereo is mixed down). Each cycle is
resampled to 1024 samples, its DC offset is removed, and it is peak-normalized (`--no-normalize`
keeps the input level). Some wave slots require their first sample to be 0. For those, the cycle is
rotated to start on a zero crossing, which only changes its starting phase.

The output directory holds a complete set: the patched panel and slave plus the stock master. The
tool only accepts the stock Argon8 3.2 panel and slave images (SHA-256 check).

## Flashing a built set

Flash with crossflash-8's flasher. It sends whatever set is in the directory, provided the set's model
byte matches what the unit's bootloader reports:

```bash
# 1. Power the synth on while holding SHIFT/INIT (screen: "UPDATE / waiting for MODALapp connection")
# 2. From the crossflash-8 checkout:
python3 -m crossflash8.flash_alsa --dir /path/to/all8/out --pace-ms 20
```

It takes about 3½ minutes. To go back to stock, repeat with your unmodified firmware directory. The
bootloader is never written, so an interrupted or bad flash can be recovered the same way. See
crossflash-8's `docs/USING.md` for setup, troubleshooting, and the macOS/Windows `flash_mido` backend.
Modifying and flashing firmware is at your own risk.
