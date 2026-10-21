# Modal Argon8 / Cobalt8 / Carbon8 firmware notes

**Focus:** this project builds new features and ports features from one firmware to another. Patching
firmware to run on a different model, and flashing it, is handled by `../../crossflash-8` (see
its README and `docs/USING.md`). The model-gate notes below are background only.

**Dev loop:** `modal_fw.py decode` → edit the image → `modal_fw.py encode --dev <target model byte>`
(the packet format follows `dev`: 0x18 → legacy 165-byte packets, others → 167-byte) → flash the
directory with `python3 -m crossflash8.flash_alsa --dir <dir> --pace-ms 20`. That flasher only checks
that the set's model byte matches what the bootloader reports. Per crossflash-8, the bootloader checks
no signature and no whole-image checksum, and it is never rewritten, so a bad image can be recovered
by re-flashing the stock set.

Inputs: `firmwares/{argon,cobalt,carbon}/*_{panel,master,slave}_*.syx` (Argon 3.2, Cobalt 2.1, Carbon 1.1).
Decoder/encoder: `tools/modal_fw.py` (decodes all 9 files with 0 checksum errors; re-encoding reproduces
every file byte-for-byte).

**Goal and base (decided 2026-10-21):** one firmware with the features of all three, built on **Carbon 1.1**.
Carbon is the newest, but that isn't the reason. The reasons:
- Carbon's unique features are modulation and voice-structure changes that span panel, master and slave:
  LFO1–3 Transform, S+H Ramped/Slewed, per-osc Level and Contour, and Filter Warp. Moving them would be
  harder than moving DSP engines in, since the filter engines are leaf functions (see "Filters").
- Carbon already contains most of Cobalt: the third LFO, the "L" filters, SVF, Drift and the Keyboard menu.
  It shares Cobalt's 48 kHz float voice path and its patch layout (see "Patch storage"). Carbon and Cobalt
  share the most byte-identical code of any pair.
- Its slave has ~181 KB of free RAM; Argon's has ~61 KB (see "Memory budget").

Risks: (1) slave CPU headroom is unmeasured, and Carbon's 2× voice buffers probably make it the
tightest of the three; (2) Argon features (int16 path, 44.1 kHz, stored wavetables, which Carbon doesn't
contain) need adapting for any base; (3) Carbon's master has the least free RAM (~44 KB).

## SysEx container

| Msg | Bytes |
|---|---|
| start | `F0 00 21 07 <dev> 00 <tgt> 7F <count lo7> <count hi7> F7` |
| data  | `F0 00 21 07 <dev> 00 <tgt> 00 <idx lo7> <idx hi7> <152 packed> <cksum> F7` |
| end   | `F0 00 21 07 <dev> 00 <tgt> 7E F7` |

- `dev`: 0x18 Argon8, 0x19 Cobalt8, 0x1D Carbon8. `tgt`: 0 panel, 1 master, 2 slave.
- Payload: 128 image bytes + 5 zero pad = 133 = 19 groups of (7 data bytes, 1 MSB byte; bit 6 → first byte).
- Checksum = sum of the 128 bytes. Argon: low 8 bits, 2 nibbles low-first. Cobalt/Carbon: 16 bits,
  4 nibbles, high byte first, each byte low-nibble-first.
- No image-level CRC/signature/encryption in the container. (Whether the bootloader checks one is unknown:
  the bootloader is not in these files.)

## Images

All three roles are linked at **0x00410000** (the bootloader presumably occupies 0x00400000–0x0040FFFF).

| Role | MCU (inferred) | SRAM | Reset |
|---|---|---|---|
| panel | Atmel SAM4 (Cortex-M4; EFC @ 0x400E0A00, SRAM @ 0x20000000) | ~80 KB used | 0x004151E9 in all 3 |
| master | SAM E70/S70 (Cortex-M7; EFC @ 0x400E0C00, SRAM @ 0x20400000) | | 0x004102F5 in all 3 |
| slave | SAM E70/S70 | | 0x004101CD in all 3 |

For each role, the set of interrupt vectors with non-default handlers is identical across all three
products. That points strongly to one shared hardware platform, though it is not proof.

Disassemble with: `objdump -d --triple=thumbv7em-none-eabi <file>.elf` (use `modal_fw.py decode --elf`).
Ghidra via REA (raw ARM:LE:32:v7 @ 0x410000) works but misses some functions, including panel `main`.

## Product gate: panel reads the SAM4 *user signature* page

Panel `main` (Argon 3.2 @ 0x438EFC) calls ASF `flash_read_user_signature(buf=0x20005A50, 128 words)`
(@ 0x411484: EFC STUS 0x14 / SPUS 0x15). This page is factory data and SysEx updates do not
rewrite it. Fields seen so far:

| Offset | Meaning | Evidence |
|---|---|---|
| 0x01 | "enter updater on next boot" flag; app sets it to 1 then SYSRESETREQ | sig write @ 0x415E3C (erase + rewrite), called from 0x438DD0 ("Rebooting for update...") |
| 0x04 | model variant: 2 → "…8M", 4 → "…8X", else plain | boot screen @ 0x41C10C; strings `8M`/`8X` @ 0x449580 |
| 0x0C | **product ID** | see below |

Product ID check on `sig[0x0C]`:

- Argon8 3.2 panel: OK iff `== 0xFF` (0x438F3A `cmp r3,#0xff`). Argon units apparently ship with this byte erased.
- Cobalt8 2.1 panel: OK iff `== 0x19` (0x4371B8).
- Carbon8 1.1 panel: OK iff `== 0x1D` (0x437EDA).

On mismatch the panel sets 0x20009FFC and 0x20005A4F (Argon addresses), shows
`ERROR: / Incorrect Firmware / (%02Xh) / Please contact support` with the byte it read, and skips the
normal startup handshake. In Argon that handshake is a SysEx sent to the DSP: `F0 00 21 07 18 xx 7B yy F7`
@ 0x439092.

## Device ID is also compiled into the DSP images

- Master builds outgoing `F0 00 21 07 18 …` in many places (e.g. 0x4261F6, 0x4292E4).
- Slave's SysEx parser @ 0x44A09C rejects anything but `00 21 07 18` and dispatches on byte 5
  (0x02, 0x22, 0x25, 0x2E, 0x37, 0x3C, 0x7F).
- Master/slave never read the user signature. The master only erases/writes flash pages (EPA/WP/EWP),
  probably for preset storage. Slaves touch the EFC only in startup (wait-state setup).

So a consistent set (panel+master+slave all from one product) agrees internally. Mixing products would break
inter-MCU messaging.

## Memory budget (Argon8 3.2)

From each reset handler (`.data`/`.bss` bounds), the initial SP, the `_sbrk` limits and code-referenced
addresses. App images start at 0x410000; the bootloader sits below.

| | panel | master | slave |
|---|---|---|---|
| MCU (inferred) | SAM4E (AFEC @ 0x400B0000/0x400B4000, RAM end 0x2001FFFC) | SAM E70/S70, 1 MB flash, 384 KB SRAM | SAM E70/S70, 384 KB SRAM, ≥1 MB flash |
| flash used by app | 0x410000–0x456D8C | 0x410000–0x486F7C | 0x410000–0x499B00 |
| other flash use | user signature only | user storage 0x49C000–0x4FFFFF (factory reset erases it; sequencer data 0x49C000–0x4BA000, settings 0x4FF000). Patches are **not** here: see "Patch storage" | none seen |
| **free flash** | ≥164 KB (to 0x480000 on a 512 KB SAM4E8; ~676 KB if SAM4E16) | **84 KB** (0x486F80–0x49BFFF) | **~409 KB** (to 0x500000 if 1 MB) |
| RAM used (data+bss+stack) | 0x20000000–0x20012B20 (stack 12 KB) | 0x20400000–0x2044ED50 (stack 8 KB) | 0x20400000–0x2044FD08 (stack 8 KB) |
| fixed buffers near RAM top | none | 0x2045B000–0x2045FFFF (20 KB) | 0x2045F000–0x2045FFFF (4 KB) |
| heap (`_sbrk`) | 0x20012B20–0x2001FFFC (live newlib heap) | start 0x20460000 > limit 0x2045AFFC, so it always fails | same pattern (limit 0x2045EFFC) |
| **free RAM** | ≤ ~53 KB, shared with any runtime `malloc` | **~48.7 KB** (0x2044ED50–0x2045AFFF) | **~60.7 KB** (0x2044FD08–0x2045EFFF) |

"Free RAM" on master/slave means no literal-pool reference into that range and no working heap. Addresses
computed from other bases can't be fully excluded. The master's 1 MB size comes from its factory-reset loop
erasing up to exactly 0x500000. The slave's flash size is inferred (same family; its 630 KB image rules out
512 KB parts). Whether the bootloader limits a component's size is unknown.

**All three models.** Same method: the reset handler's `.data`/`.bss` literals; the initial SP is `.bss` end +
8 KB on master and slave. Cobalt and Carbon have the same fixed buffers and `_sbrk` limits as Argon
(master 0x2045AFFC / 0x2045B000+, slave 0x2045EFFC / 0x2045F000+). Masters assume user storage starts at
0x49C000 in all three; slaves assume 1 MB flash.

| | Argon 3.2 | Cobalt 2.1 | Carbon 1.1 |
|---|---|---|---|
| panel image / SP | 0x46E00 / 0x20012B20 | 0x3BE80 / 0x20010688 | 0x3CD80 / 0x20010898 |
| master image / `.bss` end | 0x76F80 / 0x2044CD4C | 0x66580 / 0x2044C48C | 0x74E00 / 0x2044DFF0 |
| master free RAM | ~48.7 KB | ~50.9 KB | **~44.0 KB** |
| master free flash (to 0x49C000) | 84 KB | 150 KB | 92.5 KB |
| slave image / `.bss` end | 0x89B00 / 0x2044DD04 | 0x47B00 / 0x2040DB6C | 0x5E980 / 0x2042FAA8 |
| slave free RAM | ~60.7 KB | ~317 KB | **~181 KB** |
| slave free flash (to 0x500000) | ~409 KB | ~674 KB | ~581 KB |

Cobalt's master factory reset differs from the other two: its sequencer region is 0x49C000–0x4AA000, filled
through a helper (0x446AF5) rather than copied page by page.

## Patch storage (all three masters)

**Where:** a 128 KB I2C EEPROM on the master's TWIHS0 (0x40018000). It's a 24M01-style part: 7-bit address
`0x50 | A16`, two address bytes and 256-byte pages. Primitives (Argon 3.2): read byte 0x412F40, read buffer
0x412F98, write byte 0x412FE0, write page 0x41363C, plus a write helper 0x4133AC. The constants below are
the same in all three masters, so the EEPROM layout is shared.

| EEPROM range | Contents |
|---|---|
| 0x00000–0x0F9FF | **500 patches × 128 B**, address = `idx << 7`, `idx = bank·100 + slot` (5 banks × 100) |
| 0x0FA00–0x1193F | 500 patch names × 16 B, `(idx + 0xFA0) << 4` |
| 0x11940–0x12FFC | other tables and settings (16-B records at 0x11942 + 16·n and 0x1281B + 16·n, a block at 0x11F83, single bytes 0x12E5B–0x12E9A, 0x12F00–0x12FFC). Not mapped further |
| 0x13000–0x1FFFA | **no references found: ~52 KB apparently unused** |
| 0x1FFFB–0x1FFFF | flag and version bytes |

The factory reset rewrites only 0x11942–0x11F81 of the EEPROM (from a table in flash). It doesn't touch the
patches.

**Save path** (Argon 0x438A1A): 0x42AF7C copies the 128-byte edit buffer, **masking every byte with
0x7F**, then writes it to `idx << 7` and the name to `(idx + 0xFA0) << 4`. So each byte carries 7 bits.
Edit buffer: master RAM 0x2043A088 (Argon) / 0x2043A028 (Cobalt) / 0x2043B9E4 (Carbon). The slave keeps a
copy with the **same offsets** (Carbon slave: 0x20413134; its setter switch is at 0x42972E). The panel has
its own copy as well.

**Mapping method:** parameter IDs come from the panel tables (Argon @ 0x453374, Cobalt @ 0x4483CC, Carbon
@ 0x449234; 32-byte records `01 00 id16 min max f6 f7 grp name`). When `max` < `min`, the record is a bit
field: `min` is the bit position. The ID → offset mapping comes from each master's parameter setter
(`tbh` switch: Argon 0x42B8F4, Cobalt 0x43129C, Carbon 0x43183E) and getter (Argon 0x4384A4, Cobalt
0x43DECE, Carbon 0x43DFE2).

**Layout.** Bytes are the same in all three models unless a column says otherwise. Format: name (panel ID, range).

| Offset | Argon 3.2 | Cobalt 2.1 | Carbon 1.1 |
|---|---|---|---|
| +00, +01 | Glide (0x05), Voice Mode (0x09) | = | = |
| +02 | Distortion (0x0C) | Free Run (0xC3, 0–1) | Free Run (0xC3, 0–1) |
| +03, +04 | Osc1 Tune, Fine (0x0E, 0x0F) | = | = |
| +05 | Voice Drift (0x97) | Drift (0x97) | Drift (0x97) |
| **+06, +07** | **Osc1/2 Wave** (0x10/0x11) | **Osc1/2 Param A** (0x10/0x11) | **Osc1/2 Contour** (0x10/0x11) |
| +08 | Mix (0x12) | Mix (0x12) | XMod (0x12) |
| **+09, +0A** | OscMod, Spread (0x13/0x14, shared) | **Osc1/2 Param B** (0x13/0x14) | Osc1/2 Level (0x13/0x14) |
| +0B | Osc Mode (0x15, 0–112) | LFO3 Depth (0x0D) | LFO3 Depth (0x0D) |
| +0C–+16 | AEG ADSR, FEG ADSR, Osc2 Tune/Fine, AEG Amount | = | = |
| +17–+19 | Filter Morph / Cutoff / Reso (0x21–0x23) | = | Warp / Cutoff / Reso |
| +1A–+26 | LFO1 Rate/Depth/Shape, FEG Amt, Octave, MEG ADSR, LFO2 Rate/Depth/Shape, MEG Amt | = (shape max 108) | = (shape max 126) |
| +27 | LFO1 Sync (0x34) | LFO1/2/3 Sync, bits 0/1/2 (0x34) | same as Cobalt |
| +28 | LFO2 Sync (0x36) | LFO3 Mode (0x36, 0–84) | LFO1/2/3 Mode, 2 bits each at bits 0/2/4 (0x36) |
| +29 | FX Clock Sync (0x37) | = | = |
| +2A, +2B | LFO1/2 Mode (0x38/0x39, 0–84) | = | **LFO1/2 Transform** (0x38/0x39) |
| +2C–+30 | Arp Status, Octave (0–3), Direction (0–14), Division (0–15), Swing | = | = |
| **+31, +32** | **Osc1/2 Bank** (0x3E 0–35, 0x3F 0–126) | **Osc1/2 Alg** (0x3E/0x3F, 0–39) | **Osc1/2 Core** (0x3E/0x3F, 0–55) |
| +33 | Filter Type (0x2A, 0–7) | 0–30 | 0–33 |
| +34–+36 | FEG/AEG/MEG Type (0x50–0x52, 0–7) | = | = |
| +37, +38, +39 | Out Level (0x49), Chord Invert (0xBD, 0–36), Patch Gain (0x77) | = | = |
| +3A–+4F | FX Mode (0x29, 0–2), then 3 × FX {Type 0–26, A–F} (0x80–0x94) | = | = |
| +50–+5C | Note/Velo/AftT/Expr Depth, X+/X−/Y+/Y− Depth, Note>Cutoff, Y+>LFO1, AftT>Cutoff, Velo>AEG, Arp Gate | = | = |
| +5D | Pitch Bend (0x99) | = | = |
| +5E, +5F | **Osc1/2 WavMod** (0x03/0x0D, 0–32) | Voice Width (0x98), Linked Sequence (0xB6) | same as Cobalt |
| +60, +61 | Voice Width (0x98), Linked Sequence (0xB6) | LFO3 Rate (0x03), Shape (0x15) | same as Cobalt |
| +62 | Osc2 Tune Mode (0xC2, 0–1) | Vintage (0xC4) | Vintage (0xC4) |
| +63 | Free Run (0xC3) | Cutoff Scaling (0xC5, 0–2) | Cutoff Scaling (0xC5, 0–1) |
| +64 | Vintage (0xC4) | **unused** | LFO3 Transform (0x0C) |
| +65 | Cutoff Scaling (0xC5) | **unused** | XCore (0xC6, 0–5) |
| **+66** | **unused** | **unused** | **unused** |
| **+67** | **free**: ID 0xB7 is written by the master setter only, never read | = | = |
| +68–+7F | mod matrix: 8 slots × 3 bytes (Source/Dest/Depth, IDs 0x58–0x73) | = | = |

- **+0x66 is free in all three.** No master or slave code reads or writes it through the edit-buffer base.
  It is still saved, so stock patches hold 0 there.
- **+0x67 is usable** (checked on Carbon 1.1; Argon and Cobalt have the same setter case):
  - Master: the only writer is the setter case for ID 0xB7 (Carbon 0x432022: `strb r6,[r3,#0x67]`, then
    return). The getter returns 0 for 0xB7, and no code reads +0x67. The other `[#0x67]` hits are whole-record
    byte copies (0x41CD5E, 0x41CDE6…) or other structs.
  - Slave: the setter ignores 0xB7 (default case). Nothing accesses +0x67.
  - Panel: none of the 265 parameter records (flash 0x449234, copied to RAM 0x20000678 as `.data` from
    0x448BBC) has ID 0xB7, so the panel never sends it. The panel's own patch copy (0x2000D528, same offsets)
    gets +0x67 from incoming patch dumps (0x43083C, 0x430F16), but nothing reads it there.
  - **Seq Loop doesn't touch +0x67.** Earlier this note called 0xB7 a panel sequencer function; that was the
    *record index* (#0xB7 = "Loop"), not the parameter ID. Handler 0x42CAF6: with the sequencer stopped it
    shows "Function not available when sequencer isn't running"; otherwise it cycles a 0–2 state
    (0x2000A10E: Off / "Start point set" / On, strings via 0x41CC44) and calls 0x42542C(ch, record#0xB7.id =
    **0x47**, value, 7). That sends `B0 47 vv` to the master over the internal link (port 0xB) and CC 71 to
    MIDI out. The master's setter case 0x47 (0x431C1A) calls 0x432DA8, which only changes sequencer state in
    RAM (0x20420573/76/7D/8A, 0x20400AEC–0x20400B80) and sends ID 0x48 (Record) back. The edit buffer isn't
    touched, so using Seq Loop can't change B2.
  - Master patch load (0x4409C2) applies the record through the setter using the offset → ID table at
    0x452EE8 (+0x66 → 0x00 = skipped, +0x67 → 0xB7), so +0x67 gets the loaded patch's own value. The
    mutate loop (0x43E724, per-offset mode table 0x452F68) has mode 0 for +0x66/+0x67, so it leaves them
    alone. In the new firmware, give +0x66 a real ID in 0x452EE8 so B1 is applied on load like B2, and set
    the mode-table entries if B1/B2 should take part in mutation.
  - Data: **all 300 Carbon factory patches have +0x66 = +0x67 = 0** ("Carbon Factory Set V1, May 24").
  - Residual: the master stores whatever arrives as ID 0xB7 (e.g. an external NRPN, if the master's MIDI-in
    map reaches it; not traced).
    So old user patches might hold junk there. That's harmless: only non-Carbon engines (types 56–127) would
    read it, and no stock patch selects those. The new firmware repoints the setter case anyway.
- **SysEx patch dump** (from the factory set): `F0 00 21 07 1D 02 <bank> <slot> <128 raw bytes> F7`, 137 bytes,
  bank = n / 100, slot = n % 100, no name. The record goes out unchanged, so +0x66/+0x67 travel with dumps
  without a format change.
- Audio In → FX (0x95), XY Lock (0x9A) and Audio In Volume (0x53) are global RAM variables, not patch
  bytes, even though the same setter handles them.
- Modal already bit-packs in Cobalt/Carbon (+0x27, +0x28), and the panel record format supports bit fields.
  Many bytes have spare high bits: +0x02 and +0x63 (0–1), the EG types (0–7), FX Mode (0–2), Arp
  Octave/Direction/Division, and XCore (0–5). Using them means masking in every master, slave and panel
  reader of that byte.

**Oscillator type and parameters on a Carbon base** (what has to fit, 7 bits per byte):
- Type: the three models use the same bytes, +0x31/+0x32. Carbon has 56 Cores, Cobalt 40 Algs, and Argon
  36 banks plus 4 Osc2-only noise/PWM types, so 136 values per oscillator, more than 7 bits. Options:
  (a) a family selector, 2 bits per oscillator, in +0x66; stock patches read 0 = Carbon; or (b) one flat
  list 0–127 (Carbon 0–55, Cobalt 56–95, Argon 96–127), which leaves out 8 of Argon's 40 entries.
- Parameter 1 per osc fits in +0x06/+0x07 (Contour / Param A / Wave), interpreted per engine.
- **Parameter 2 per osc** (Cobalt Param B 0–127, Argon WavMod 0–32): Carbon uses +0x09/+0x0A for Level,
  which has to stay. This needs 2 × 7 bits that the record doesn't have spare. With (a) the total is 18
  bits; with (b) it is 14. Supply: +0x66 (7 bits, verified free), +0x67 (7 bits if ID 0xB7 is dead), and
  spare high bits. So (b) fits exactly in +0x66/+0x67. (a) also needs 4 bits taken from a 0–1 or 0–7 byte.
- Fallback that keeps the 128-byte record: an extension table (e.g. 500 × 4 B) in the unused EEPROM range
  0x13000+. Load, save, copy/swap and SysEx dump/receive all need hooking, and dumps lose the data unless
  the SysEx format is extended too.
- Argon's other oscillator controls (Osc Mode 0–112, OscMod, Spread, Mix, Osc2 Tune Mode) overlap Carbon's
  XMod/XCore and aren't counted above.
- **Decided (2026-10-21):** (b), a flat type list in +0x31/+0x32. The engines share some duplicate types,
  which won't be ported, so 128 entries are enough. Cobalt's Mix (+0x08) is dropped: Carbon keeps XMod there
  and its per-osc Levels at +0x09/+0x0A (see "Oscillator combine stage"). Parameter 2 (Cobalt Param B1/B2, Argon
  WavMod) goes in +0x66 (Osc1: B1) and +0x67 (Osc2: B2), one full 7-bit byte each, so B1 and B2 keep their
  independent 0–127 ranges as in Cobalt (+0x09/+0x0A there). +0x67 was checked: it's usable (see above). Nothing
  but the new Param B record sends ID 0xB7, so B2 keeps it; B1 gets ID 0xB4 (see "Cobalt oscillators on
  Carbon"). Implemented by `tools/carbon_cobalt.py`.

## Oscillator combine stage and XMod (Carbon 1.1 slave)

Source: the Ghidra slave decompiles from the filters work, with literal pools resolved. All addresses are Carbon
1.1 slave unless marked otherwise.

**Block function 0x41EF38.** The outer loop runs over the oscillators and the inner loop over the 4 voices.
1. Control smoothing (one-pole, 0.006 per sample, 128 values per block): Osc1 Contour +0x06, Osc1 Level
   +0x09, Osc2 Level +0x0A, and XMod +0x08 (patch copy 0x20413134). When osc2's pass starts, Contour +0x07
   is smoothed into the same contour array 0x2042E7E4.
2. Osc1 pass: for each voice, `render(state, out, osc, contour[])` writes osc1 into the voice's output buffer.
3. Osc2 pass, for each voice:
   - add the mod-matrix contributions to XMod and both Levels (mod buffers 0x20408964/0x2040C964, routing
     slots in 0x204020B8);
   - render osc2 into 0x2042CDA4;
   - map Level1 and Level2 through a 256-entry gain curve (0x452298) to get g1 and g2;
   - combine according to **XCore (+0x65)**, written back into the voice buffer.
4. Note-on bookkeeping. The voice buffer then goes to the filter.

Oscillator buffers hold **256 samples per 128-sample block (96 kHz, 2× oversampled)**, and the control arrays
hold 128 values (read with index `i >> 1`). Note-on also uses 96000.

**Render dispatch** (0x41A130): `render(state, out[256], contour[128], osc)`. The state is
`0x2042DBA4 + voice·0x148 + osc·0x520`. The selected Core at 0x20406714[osc] indexes two flash tables: a render
family (0x4526AC) and a kind (0x4521B8, where kind 2 selects a second switch). Together they select one of 22
render functions. A ported engine family is one more case here.

**XCore modes** (o1/o2 = osc outputs, x = XMod 0–1, g1/g2 = level gains; the output is `o1·g1 + f(o2)·g2`):

| XCore | f(o2) | effect |
|---|---|---|
| 0 | `o2 + x·(o1·o2 − o2)` | crossfade osc2 → ring mod o1·o2 |
| 1 | `min(o2, o1 + 2(1−x)²)` | osc2 clipped from above by osc1 |
| 2 | one-pole smoother on o2; coefficient = table 0x2040217C[(o1+1)/2 · x · 1022] (RAM, from .data) | osc1 drives a lowpass on osc2 |
| 3 | `o2 + u·x·(o2² − o2)`, u = (o1+1)/2 clamped to 0–1 | osc1-gated squaring of osc2 |
| 4 | `o2 + x·(s·o2 − o2)`, s = sine at **half** the per-voice increment 0x2040003C | sub-octave sine ring mod on osc2 |
| 5 | before osc2 renders: `contour2[i] += s·x`, clamped to 0–1, s = sine at the full increment; then a plain sum | sine modulation of osc2's Contour |

The sine table is 0x45C490 (1024 floats), and the phase is per voice (0x20406704). Values ≥ 6 would give a
plain sum, but the slave clamps XCore to 5. Which pitch 0x2040003C carries (osc1 or the voice note) is not
verified.

**Consequence for ported oscillators.** XCore modes 0–4 work only on the two rendered buffers, so **they work
with any engine** that renders into Carbon's format (float, 256 samples at 96 kHz). A port has to produce that
format anyway to pass through the Levels. Mode 5 is the only engine-specific mode: it feeds the per-sample
parameter-1 array (Contour → Param A / Wave), so the ported engine must read parameter 1 from that array
rather than from the patch byte.

**Cobalt 2.1 for comparison.** The voice function is 0x439774. It calls `alg(out[128], voice_state, osc)` through the 40-case
switch at 0x439490 for each oscillator, then crossfades `o1 + mix·(o2 − o1)` with Mix +0x08 (≥ 0x7E → 1.0)
plus mod. There's no cross-mod stage, so Carbon's Level + XCore stage replaces this whole crossfade. Porting
a Cobalt algorithm into Carbon's render switch needs:
- **Rate.** Cobalt algs loop exactly 128 samples at 48 kHz. The caller computes phase increments as `f/48000`
  into the voice state (8 × 0x44 B entries alternating osc1/osc2, i.e. 4 per osc; more fields follow up to
  +0x280). Options: call twice per
  block with `f/96000` (2× CPU, and the per-sample smoothing constants 0.999/0.001 must be adjusted), or
  render at 48 kHz and upsample 2×.
- **Parameters.** Algs read Param A themselves from patch +0x06/+0x07, smooth it per sample and add the float
  mod buffers (0x2040299C/0x2040699C, routing 0x204000C4). Carbon's buffers have the same shape at different
  addresses (0x20408964/0x2040C964, 0x204020B8). Param B reads (+0x09/+0x0A) must move to +0x66/+0x67.
  Wrapping the alg to take Carbon's contour array also makes XCore 5 work.
- **State.** Cobalt's per-voice state is larger than Carbon's 0x148 slot, so it needs its own RAM. That's
  fine, since the slave has 181 KB free.

## Cobalt oscillators on Carbon (`tools/carbon_cobalt.py`)

Status: works on hardware (Argon8 crossflashed to Carbon, 2026-10-21) after the uninitialised-RAM fix
below; checked in emulation (unicorn) throughout. Cores 56–95 = Cobalt algorithms 0–39; Param B per
oscillator at +0x66 (ID **0xB4**) / +0x67 (ID **0xB7**).

**Choosing the B1 ID.** It has to fall to the default case in the panel records, the master setter
(0x431844 table), the slave setter (0x42972E), the slave CC handler's special IDs (0x42B69C: 0x01, 0x0B,
0x74–0x78, 0x7F, 0xAC–0xAF, 0xBE, 0xBF) and the master's generic CC handler (0x44027C, a `tbh` on ID).
That handler special-cases 0xB5 (and 0x00, 0x01, 0x0B, 0x74–0x7D, 0x7F, 0x96, 0xAB–0xB0, 0xB3, 0xB8, 0xBD,
0xC0), which rules out the first choice. Remaining free IDs: 0x04, 0x08, 0x0A, 0x41, 0x42, 0x4A, 0x61, 0x7A,
0x7E, **0xB4**, 0xC1. The generic path calls setter(id, val, 0) and then forwards the CC to both slave links
(`0x42AF48(0, id, val, 1)` and `0x42AF48(4, id, val, 2)`), so any default-case ID reaches the slaves.

**Master ↔ slave link.** Router 0x42AAC8(cin, msg, flags) builds a 4-byte USB-MIDI-style packet
(byte 0 = CIN, 0xB = CC) and queues it per flag bit: bit 0 → UART1 TX ring (0x20409AF0), bit 1 → UART3 TX
ring (+0x408), bit 2 → UART4 ring (0x20417500). Each slave uses only UART4 on its side (the same image runs on
two slaves, 4 voices each). The slave's CC handler 0x42B800 does NRPN (0x06/0x26/0x62/0x63) itself and passes
everything else to setter(id = cc, val). Whole patches go through the slave's own offset → ID table
0x45278C (identical to the master's 0x452EE8) in 0x42B8A8.

**Slave (Carbon 1.1).**
- Cobalt 2.1 slave: the 40 algorithms are back to back at 0x416B44–0x439490 (switch 0x439490, case stubs
  `ldr r4,=alg ; movs r2,#idx`). Each is `alg(out[128], voice_state, alg, osc)`, a leaf function. They read
  12 RAM globals: patch 0x2040B7C8 (+6/+7 Param A, +9/+0xA Param B), routing 0x204000C4, audio mod buffers
  0x2040299C/0x2040699C, bipolar flags 0x2040A99C, control mod 0x2040A9F8, "mod on" flags 0x2040AAD9 (A1),
  0xAADA (B1), 0xAADB (A2), 0xAADC (B2), and two constant `.data` FM tables 0x20400014/0x20400054
  (LMA 0x456DDC). Flash tables lie in 0x449CE8–0x456E80; the only calls are libgcc dsub/dadd/f2d/d2f
  (0x4477E0/E4, 0x447AA0, 0x4480A8), byte-identical to Carbon's 0x42E098/09C/358/960. All 578 literal words
  holding these values are relocated by value; recursive-descent analysis found exactly the same 578.
- Voice state (0x290 per voice): 8 phase accumulators of 0x44 bytes, osc 0/1 interleaved (entry k =
  osc + 2j). Inputs written by Cobalt's voice function every block: +0x0C increment = f·ratio/48000 (capped at
  10 kHz → 0.2083333), ratio j = 1, 0x455D4C[B], [B+128], [B+256]. +0x234 = voice. Some algorithms rewrite
  +0x0C, so it must be set every block. Alg change (0x4161E0) and note-on with Free Run off (0x416550)
  clear the osc's entries and set +0x11 = 1; algorithms with 0x456970[alg] ≠ 0 also randomize sub-osc phases
  ×0.25 unless 0x4511BC[B] ≠ 0.
- Carbon render dispatcher 0x41A130(out, osc, voice, contour): computes the osc frequency into the state
  (0x412594: state +0x10 = Hz after tune, pitch mod, vintage and drift; +0xE4 = voice), then calls
  `core(state, out[256], contour[128], osc)` by Core (0x20406714[osc]). Contour arrays are 128 control values
  without modulation; cores add it themselves: `clamp01(contour[i] + ctrl + modA[i] + depth·modB[i])` with
  depth = clamp01(contour[0] + ctrl), inverted unless unipolar (flags 0x20410AAD/AAF, routing 0x204020B8[osc],
  buffers 0x204109C4/0x20408964/0x2040C964). Note-on reset 0x410F88(voice) zeroes state word 0 of both oscs.
- The glue (`tools/glue/cobalt_glue.c`) fills a Cobalt-shaped view in free RAM (0x20440000, 15.4 KB) on each
  call: Param A goes in per sample through the mod input (flags A on, routing osc → slot osc+1) using
  Carbon's formula above, B from +0x66/+0x67, increments from state +0x10. It detects Carbon's note-on reset
  with a marker (it writes 0.5 to state word 0 after each call), runs the algorithm, clips to ±2, and
  upsamples with a 16-tap halfband (−0.17 dB at 20 kHz, < −34 dB above 28 kHz; 8 input samples of delay).
- Hooks: dispatcher 0x41A164 (Core ≥ 56 → glue), Core loader 0x411DE0 (Cores ≥ 56 only store the Core
  and set 0x2040004C[osc]), setter entry 0x429720 (0xB4/0xB7 → +0x66/+0x67), Core clamps 0x42A1AC/0x42A1CC
  (55 → 95), offset → ID table +0x66 = 0xB4. Code at 0x470000, tables 0x493000, glue 0x4A1000.
- Checked: all 40 algorithms × 2 oscillators through the patched dispatcher match stock Cobalt bit for bit
  (even samples) and the reference interpolation within 1e-5 (odd samples). Carbon Cores 0–55 render
  identically to stock. Contour mod path, note-on reset and patch load work.
- Cost (emulated instructions per render call, glue included): Cobalt mean 17.9k, max 25.7k (Spread Sqr);
  Carbon Cores mean 13.7k, max 19.5k. Real cycle headroom is unknown.
- Stock Cobalt behaviour kept: Fold Tri emits ~−3900 on the first sample after a phase reset, FM Squ/Tri Squ
  ~1e6 for 4 samples at B 127. The ±2 clip covers these.
- **Hardware bug (Argon8 unit with crossflashed Carbon, 2026-10-21): selecting any Cobalt Core killed the
  sound engine for good.** Cause: the glue sets up its view on first use, keyed on `cv_magic`. Carbon's startup
  doesn't clear 0x20440000+. clang compiled the plain `static uint32_t cv_magic` (only ever assigned MAGIC)
  as a one-byte flag (`ldrb [0x20440000]; cmp #0`), since C lets it assume the static starts at zero. At
  power-on, RAM is random, so the flag almost always read nonzero and the view was never set up.
  The algorithms then indexed with garbage (voice +0x234 = 0x57 instead of 0–3) and took a BusFault.
  The slave has no fault handler (HardFault is `bkpt`), so it stopped.
  Fix: `static volatile uint32_t cv_magic` (full 32-bit compare). In unicorn with 0x20440000–0x20444000
  filled randomly (3 seeds), the old build crashed on all 40 Cores and the fixed one matches the
  zeroed-RAM output. **Any state in RAM that Carbon's startup doesn't clear must be keyed on a volatile
  magic word**, and emulator tests should start from random RAM.
- How it was found: flash probes showed the image was written correctly. A diagnostic build then wrapped
  each algorithm call and replaced the fault vectors (3–6) and the XDMAC vector with handlers that record
  the stacked frame and CFSR/BFAR to RAM that survives a reset, then unwind the call. The record was read
  back by ear, as beeps on Cores 56–63 with Param B choosing the word. Result: BusFault (CFSR 0x8200),
  PC 0x478BC2 in PWM Saw Erase (alg 8, `vldr s9,[r3]`), BFAR 0x203873E8, r12 = 0x57.
  Ruled out on the way: DMA (one XDMAC channel, audio buffer 0x2045F000 only), MPU/cache attributes (RAM
  0x20400000–0x2045FFFF is write-back, D-cache invalidated only at boot), stack depth (a full audio block
  on a Cobalt Core needs ≤ 5.4 KB of the 8 KB stack, against 4.8 KB for Carbon), and the interrupt handlers.
  Carbon and Cobalt slaves have identical MPU/cache/vector setup.

**Master (Carbon 1.1).**
- Setter table entry 0xB4 → stub storing +0x66; Core clamps 0x4305CA/0x430606 → 95; offset → ID +0x66 = 0xB4.
- Other ID switches (0x430270/0x4324A0/0x43DFDA: sequencer parameter lanes; 0x43C068/0x43C760: mod-slot value
  restore; 0x43D494/0x43D7CA/0x43F35C/0x43FB46: stack arrays) are left alone, so B1/B2 can't be recorded in
  sequencer lanes.
- The oscillator waveform shown on the panel is computed by the **master** (0x42DAE4, from Core, Contour and
  per-Core tables 0x4806A4/0x452E08 of 56 entries) and sent as `F0 00 21 07 1D 7A <osc> <128 × 0–63> F7`
  (0x42AA28). The panel requests it with CC 0xC0 (value 1/2 = osc). Cobalt's master has its own 40
  renderers `preview(out, A, B)` (switch 0x42D41C, code 0x423C18–0x427808, leaf functions). They are copied
  with their tables (traced over all A/B values: 0x44D4E4, 0x453CE8, 0x46D2CC–0x46F008, 0x470804–0x4710CC,
  0x4730CC–0x4736D0), constant `.data` 0x204001B0–0x20400AB4 (LMA 0x4741E4) and 0.3 KB of scratch RAM
  (→ 0x20452000). Hook 0x42DB24: Core ≥ 56 → Cobalt renderer → the normal send at 0x42DBD8. All 320
  checked cases match Cobalt's output.
- The scratch RAM isn't only written before read: 24 renderers carry state in it between calls. These are
  58–62, 76–93 and 95 (read-before-write at e.g. 0x4867C2, 0x488144, 0x4894E6). In Cobalt it is zero at boot;
  in Carbon's master it holds power-on garbage, which changed 103 of 120 tested previews. The hook clears
  0x20452000–0x20452250 on first use, keyed on the word after it (`PREVIEW_MAGIC`). With random scratch
  RAM, the previews now match the old build's zeroed-RAM output (414 cases, two calls each).

**Panel (Carbon 1.1).**
- Records are written at runtime (enable flags of #38–43), so the table stays in RAM. The tool moves it to
  the top of the newlib heap (0x2001DC00, 267 records), lowers the `_sbrk` limit (literal 0x415430) and copies
  it there from flash before `main` (reset handler literal 0x4152A4). References: 31 literal words of
  0x20000678, one of 0x20001FFC (record 204 + 4), counts `movw #0x109` at 0x41B134/0x42020E.
- Menus are `.data` halfword lists of record indices, 60-byte slots, 0xFFFF-terminated (Osc1 0x44BA8C,
  Osc2 0x44BAC8, list at 0x44BA14 + 0x3C·n). New records 265/266 "Param B" (grp 1/2) go after Contour.
- Value-to-text 0x41BB4C(record index, value, out): Core names (records 9/10) from 17-byte table 0x4478FC with
  a clamp at 0x41BD6E; extended to 96 names. The title for Contour (records 2/3) comes from 13-byte table
  0x445254 indexed by Core (literals 0x41E12C, 0x422554); extended with Cobalt's Param A names (Cobalt panel
  0x44447C: 40 × {A name, B name}). Hook 0x421604 titles records 265/266 with the B name on Cobalt Cores.
  Core setter clamps 0x427702/0x42771A → 95.
- 20 ID switches (`cmp rN,#0xC3`; list in `CarbonPanel.ID_SWITCHES`) get 0xB4/0xB7 cases cloned from ID 0x12
  (XMod, +0x08) with the offset changed: `tbh` switches through a pre-dispatch stub, the two `ldr pc` tables
  (0x41FDE4, 0x42DB0A) by table entry.
- Front-panel knobs: handler 0x435DE0(_, delta) gets the knob index from 0x435AC0 and switches over 27 cases
  (`tbh` at 0x435DFE). Each case loads r9 = current value, r0 = max, r10 = ID and branches to 0x435EA4, which
  clamps, calls setter 0x426D2C, sends (0x42542C, 0x425EE4) and shows the parameter (0x42012C). Two modifier
  layers: UI state 0x2000CDB4 +0 points at the **shift** button's held flag (button 16, 0x200050E0 + 16·12 + 5;
  shift+Level1 = Glide, shift+Level2 = Drift, shift+Contour = Core, shift+Tune1 = Fine) and +0xD is **Patch**
  held (button 0x12; Patch+XMod = Free Run, Patch+Cutoff = Type). Patch cases are tested first, and they set
  0x200074D2 so that releasing Patch (release handler 0x433674, case 0x12) skips its own action. Buttons
  are 12-byte objects at 0x200050E0 (+0 press time, +4 handled, +5 held); press handler 0x432B78, matrix scan
  0x435108.
- Patch+Level1/2 → Param B: the Level1 (knob 14, 0x436004) and Level2 (knob 16, 0x436190) cases begin
  `ldr r3,=UI ; ldr r3,[r3]`, and the tool replaces those two with a `b.w` to a stub. If UI+0xD is set,
  the stub loads +0x66/+0x67, sets 0x200074D2, max 127, ID 0xB4/0xB7 → 0x435EA4. Otherwise it redoes the two
  loads and returns. The mod-destination table 0x444364 has 127 (none) for 0xB4/0xB7, as for Free Run.
- Checked: boot copy, menus, names, title hook, and encoder edits of records 265/266 through the encoder
  handler 0x42CDD0 (updates +0x66/+0x67 and sends 0xB4/0xB7 through 0x42542C and 0x425EE4 exactly as a Core
  edit does). Knob handler in unicorn: Patch+Level1/2 update +0x66/+0x67 (clamped 0–127), send 0xB4/0xB7 and
  set 0x200074D2; without Patch, Level1/2, Glide and Drift behave exactly as stock.

## Argon8 wavetables (slave 3.2)

Tool: `tools/argon_wavetables.py` (`extract` dumps the stored banks as WAV, `build` writes a flashable
set with replaced banks). On hardware (2026-10-20): custom names display and bank changes work once the
panel is padded to whole flash pages (see the hang report below). The wave display fix is not yet tested.

- **Renderer** `0x419228(float *out, int len, uint bank, int wave)`: `bank` 0x00–0x23 (36 banks) plus 0x7E
  (a special square; 0x24–0x7D render nothing), `wave` 0–4 (5 waves per bank; wave 4 is the switch default).
  - Banks 0x00–0x17 call generator functions (procedural, not yet mapped). Banks 9 and 10 also copy
    3 stored waves.
  - Banks **0x18–0x23 (60 waves)** copy 1024 int16 samples from flash × 1/32767.
  - Bank 0x1B's samples live in `.data` (RAM 0x204005EC, copied at boot from flash 0x4964CC+0x5EC).
  - The compiler folded each wave's first sample into a `vldr` literal. 33 of these are single-use
    (safe to patch). Four (0x419944, 0x41A4A0, 0x41A7B8, 0x41A104; all 0) are shared by 6/7/3/6 waves,
    so those slots need sample 0 = 0.
- **Mip builder** `0x41aa48(len=1024, osc, bank)`: renders the 5 waves into RAM 0x2040C9C0. Each one
  goes through an FFT (complex FFT @ 0x44AE3D, instance @ 0x49366C), and 12 band-limited levels are
  built by halving the harmonic count. A per-oscillator mode byte (RAM 0x2040A0D4[osc]) of 9 or 10
  caps the harmonics at 256 or 128. So the firmware band-limits any single cycle it is given.
- Only the slave renders waves (the master has no bank switch). Within the scanned literal pools,
  nothing besides the renderer references the stored samples. Pointer arithmetic from other
  bases can't be fully ruled out statically.
- The slave also holds 34 cycles of 256 interpolation-pair words, `(x[n+1], x[n])` per 32-bit word, at
  0x44F3EC–0x4573EC (sine @ 0x451FEC, saw @ 0x44F3EC, …). These are probably the basic/LFO shapes, not yet traced.
- Panel parameter table @ 0x453374 (260 × 32-byte records: `01 00 id16 min max f6 f7 grp name`).
  Osc1 Bank = id 62, max 35. Osc2 Bank = id 63, max 126 (the meaning of 126 vs 35 isn't known yet).
  Osc Wave = id 16/17 (0–127), WavMod = id 3/13 (0–32).
- **Bank names are panel-only.** They are not in the wave data, the slave, or the master. The panel's
  parameter display routine `0x41CFE0(kind, value, out)` handles `kind` 10 (Osc1 Bank) and 11 (Osc2 Bank)
  as a chain of `if (value == n)` branches. Each branch copies a literal string with an **inlined,
  fixed-length strcpy** (strlen+1 bytes). Strings sit at 0x449ED8–0x44A01C. Values 0x00–0x23 = the 36
  factory names. Osc2 only: 0x7B "PWM", 0x7C "Clocked Noise", 0x7D "Sine Noise", 0x7E "Square Noise"
  (hence its max of 126). 0x24–0x7A display an empty string.
  - Renaming in place means a new name can't be longer than the old one; shorter names are padded with NULs.
  - The tails "1" (0x449EE0) and "2" (0x449EE8) of "002 1"/"002 2" are also used by unrelated code
    (0x419BB2, 0x419BC4, 0x41EA56, 0x419BF6, 0x419C08), so don't edit them in place.
  - Each bank's pointer literal is loaded once, so pointing it at a new string in free panel flash is safe,
    but the copy length stays fixed. Longer names, or names for added banks, need the branch replaced by a table lookup.
  - **What `argon_wavetables.py build` does:** at 0x41D010 the routine runs `bls.w 0x41D1E8` (taken for kinds
    10/11). At that point r3 = value, r4 = output buffer, `{r4,r5,r6,lr}` has been pushed, and every case returns
    with `pop {r4,r5,r6,pc}`. The tool retargets that branch to a 32-byte stub appended at the old image end
    (0x456E00):
    - `value ≤ 0x23` → strcpy from a 36-entry pointer table (stock entries keep their original strings);
    - otherwise → `b.w 0x41D1E8`, so the Osc2 extras and blanks behave as before.
    - 0x41D1E8 has no other entry. Names are capped at 13 characters ("Clocked Noise" already uses this path)
      and limited to the panel's name-entry charset @ 0x452694.
    - Assumption: the panel's Bank value is the same number the slave renderer switches on. The panel's wave
      display indexes its table by the same value (below).
  - **Hardware report (2026-10-20): the first named build hung as soon as the bank was changed. Fixed by
    padding the panel image to whole 512-byte pages** (confirmed on hardware). Ruled out offline first:
    - name-buffer overflow (all 5 callers of 0x41CFE0 pass 20–30-byte buffers);
    - MPU/CMCC execute restrictions (the panel configures neither);
    - stub encoding (disassembly checked).
    That build's panel image ended half-way through a 512-byte SAM4E page (0x46F00); the stock Argon panel is an
    exact page multiple (0x46E00). Inference: the Argon panel bootloader doesn't program a partial final page, so
    the stub stayed erased and faulted. **Keep every patched Argon panel a whole number of 512-byte pages.**
- **The panel draws the oscillator wave from its own copy**, not from the slave. Table @ 0x44AAA8: 36 banks ×
  5 waves × 128 bytes (to 0x4504A8), entry `bank * 5 + wave`, read only by the draw code at 0x41F136/0x41F290
  (pool word 0x41F348). Each byte is 0–62 with 31 = zero; it is drawn as `63 - b`, so positive is up. The draw
  routine crossfades entry `c` and `c + 1` with `c = Wave >> 5` and weight `(Wave & 31) / 32`. One path first
  converts the 128 points to floats (`(63 - b) / 31.5 - 1`, RAM 0x2000E434 + slot·0x200) and runs them through
  0x418AA1 (probably a WavMod preview).
  - Factory entries for banks 0x18 and 0x1A–0x1F match the slave samples as `round((s / 32767 + 1) * 31)` at
    every 8th sample (±1). Entries for **0x19 and 0x20–0x23 don't match their slave waves or any other slave
    data**, so the stock display already differs from the sound there. The slave renderer copies those banks
    straight from the addresses in the tool, so the sound side is right.
  - The build rewrites the entries of replaced banks in place (no size change). `--stock-names` still does this.
- Free slave flash: image ends at 0x499B00. The MCU must be a 384 KB-SRAM SAM E70/S70 (SP = 0x2044FD08),
  which come with ≥1 MB flash, so there is ≥ ~360 KB free for added banks.

Next steps for *adding* banks (instead of replacing): hook the renderer's `0x24–0x7D` default case to
read from a table appended after 0x499B00. Then raise the panel's Bank max (record @ 0x4534B4/0x4534D4)
and check whether the master clamps or rescales the bank value on its way to the slave.

## Filters (Argon 3.2, Cobalt 2.1, Carbon 1.1 slaves)

Decompiled with headless Ghidra (language `ARM:LE:32:Cortex`, TMode forced to 1, functions seeded from `bl`
targets plus odd code pointers in literal pools). REA's raw-binary import uses the generic ARM language and
finds only ~200 functions in these images. Only the slaves contain filter code: the keytrack constants shared
by every engine (135.504, −0.1015522, 22.727272) are absent from the masters.

**Parameters.** Panel ids are the same on both: Morph 0x21, Cutoff 0x22, Reso 0x23, Type 0x2A, Cutoff Scaling
0xC5. Both slaves keep the patch in RAM with Type at **+0x33**, Morph +0x17, Cutoff +0x18, Reso +0x19,
Note>Cutoff +0x58, AftT>Cutoff +0x5A, Velo>AEG +0x5B (slave setter switch, Argon 0x44796C / Cobalt 0x4430A8).
The tail differs: Cutoff Scaling is +0x65 on Argon and +0x63 on Cobalt, Vintage +0x64 vs +0x62. Argon filters
also read +0x02 (Distortion, id 0x0C), which Cobalt doesn't have. Type is clamped three times:

| | panel max (record) | master clamp | slave clamp |
|---|---|---|---|
| Argon | 7 (0x4535D4) | `cmp r6,#7` @ 0x42C09C | 8 @ 0x4489B4, copied to 0x20405920 |
| Cobalt | 30 (0x4485AC) | `cmp r6,#0x1e` @ 0x4307E0 | 30 @ 0x443AA8, then tail-calls 0x43A5E9(type) |

**Sample rate.** Argon slave runs at **44.1 kHz** and Cobalt at **48 kHz**. Each image has the same
cutoff→coefficient table generated for its own rate: Argon 0x4709BC starts 0.0002912, Cobalt 0x44C2A8 starts
0.0002676. The ratio is 1.0882 = 48000/44100, and both start at the same ~4.09 Hz. Smoothing constants differ
the same way (0.9997088 vs 0.9997324). The "vintage" table is byte-identical (Argon 0x48EE4C = Cobalt 0x456450).

**Argon dispatch** (block function 0x442E88, 4 voices × 128 samples). Oscillators render into int16 buffers
(0x4402F9 per voice), then:

| type | name | engine | domain |
|---|---|---|---|
| 0, 1 | Standard, Standard Notch | 0x4340B8 (20 KB, one loop per mod-routing combination), then 0x43FD5C | Q15 fixed point, coefficients interpolated from int16 tables 0x47231C/0x458FEC/0x46DC30; state 0x48/voice @ 0x20422778 |
| 2, 3 | Classic, Classic Notch | 0x4396D8 (8.8 KB), then 0x43FD5C | Q15; state 0x88/voice @ 0x2043ACFC |
| 4 | Ladder | 0x43BD68 (3.8 KB) | float |
| 5 | Ladder Hybrid | 0x43DD80 (3.8 KB) | float |
| 6 | Ladder Phaser | 0x43CDC0 (3.7 KB) | float |
| 7 | Sallen-Key | 0x43ED98 (2.5 KB) | float |
| 8 | (hidden: no filter) | none | the slave accepts it but the panel max is 7 |

Types ≥ 4 take the `else` branch: int16 × 1/32767 → float, then the switch at **0x43FCE8** (78 bytes) calls
`engine(state, buf)` for each voice. All four float engines share one state object per voice (0x78 bytes @
0x20414E84, stride 0x78; the voice index is stored inside it at word 0x1D). Any new type ≥ 9 already reaches
this switch, so adding engines on Argon means replacing that switch and raising the three clamps.

**Cobalt dispatch** (block function 0x43F410). All float. Voices render via 0x439775, then:

| type | names | engine | state/voice |
|---|---|---|---|
| 0–3 | Reso LP, Balanced LP, Balanced HP, Balanced Phase | 0x43A648 (7.6 KB, one loop per type, nonlinear: `vsqrt`) | 0x24 @ 0x2040B738 |
| 4–5 | SVF LP/BP/HP, SVF LP/Notch/HP | 0x43D578 (3 KB) | 0x6C @ 0x2040BC24 |
| 6–30 | the 25 "L …" modes | 0x43C7FC (3 KB) | 0x68 @ 0x2040B598 |

Engines are called as `engine(buf, state, voice)`. The "L" engine is a 4-pole zero-delay-feedback ladder with
saturation, run 2× per sample, with a 16-tap symmetric FIR decimator. Its output is a mix of the input and the 4
pole outputs, `Σ mix[type-6][k]·tap[k]`, from a 25 × 5 float table @ **0x44A0EC**: Xpander/Matrix-12-style pole
mixing (24LP = 0 0 0 0 1, 24HP = 1 −4 6 −4 1, …).

**Carbon 1.1 dispatch** (slave block function around 0x425F80, 48 kHz, all float). It has the same Type slot
(+0x33), and slot +0x17 is "Warp" (Cobalt's Morph). Clamps: panel max 33 (record 0x449434), master `cmp r6,#0x21`
@ 0x430D9C, slave 0x42A500. It dispatches **per block**: `type ≤ 7 ? 0x424CFD : 0x423409`. Each of those
214-byte wrappers smooths Reso (+0x19) and Warp (+0x17) into two shared 128-float ramps, then calls
`engine(buf, state, voice, reso_ramp, warp_ramp)` for each of the 4 voices:

| type | names | engine | state/voice |
|---|---|---|---|
| 0–1 | SVF LP/BP/HP, SVF LP/Notch/HP | 0x423558 (3 KB) | 0x78 @ 0x2042C594 |
| 2–7 | Dual LP/BP/HP, Dual LP Alt, Dual HP Alt, Dual Peaking | 0x424244 (2.4 KB) | same as SVF |
| 8–32 | the 25 "L …" modes | 0x421C70 (2.7 KB), mix table 0x4309AC (= Cobalt's) | 0x68 @ 0x20412994 |
| 33 | Adj Phaser | 0x422858 (2.7 KB) | same as L |

- **Carbon voice buffers run at 2× rate**: 0x400 bytes per voice, 256 samples per 128 output samples. The L and
  Adj Phaser engines feed both input samples of each pair into their 2× ladder and decimate with the 16-tap FIR.
  Cobalt's L engine instead gets 1× input and interpolates it up. Dual and SVF read only every other input
  sample (`ptr += 2`).
- **Dual**: two parallel 2-pole zero-delay-feedback SVFs, each with saturated resonance feedback. Warp spreads
  the two cutoffs in opposite directions, up to ±2 octaves (tables 0x441A98: 1→0.25 and 0x441298: 1→4,
  ~512 entries). Output = 1.2 × Σ of an 8-float row from the mode table at RAM 0x2040317C (.data, flash 0x46ACE4
  + offset): columns 1–3 are filter A's LP/BP/HP and 5–7 are filter B's. Example rows: Dual LP = LP_A + LP_B,
  Dual LP Alt = −0.9 LP_A + 1.2 LP_B, Dual HP Alt = LP_A − LP_B + 0.1 HP_B. Input gain is 0.6, clipped to ±4.
- **Adj Phaser**: the L ladder core with no table row. Warp computes the pole mix continuously:
  `x − 4·y1 + 12k(w²+2)·y2 − 8(w²+1)·y3 + 4(w²+1)·y4`.
- **SVF / L** are the same algorithms as Cobalt's but compiled separately, with retuned SVF constants and the
  2× input. On the "L" modes, Warp/Morph drives the input on both firmwares (`(1 + w·k)·x`).
- Mod-matrix values are float as on Cobalt, but the destination slots differ: Carbon engines index the
  routing table at +0x26/+0x28/+0x2A (cutoff, reso, warp), Cobalt at +0x20/+0x22/+0x24. The patch tail matches
  Cobalt (Cutoff Scaling +0x63).

**Porting assessment.**
- Every engine on both sides is a **leaf function**: no calls, about 25 RAM globals, 2–4 flash tables. So
  transplanting it as a binary is realistic: copy the code into free slave flash, rewrite its literal pool and
  give it its own state RAM. The 4 Argon float engines use the same 27 globals, and the 3 Cobalt engines the
  same ~25. One mapping table per direction therefore covers a whole family.
- The globals are the same things on both sides (both firmwares compile the same cutoff/keytrack/mod code;
  the polynomial and its constants are identical). What differs is their type: **Argon's per-sample mod-matrix
  buffers are int16** (0x20405B50/0x20407B50/0x20409B50, read with `ldrsh` and Q15 saturation), while
  **Cobalt's are float** (0x2040299C/0x2040699C/0x2040A99C). Relocating addresses isn't enough. Either patch
  those loads, or run a per-block shim that converts the few destinations a filter reads (cutoff/reso/morph ×
  4 voices × 128) into a scratch buffer in the other layout.
- Rate-dependent data: point the cutoff table at the host's own one (same shape, already right for its rate),
  and rescale the handful of 1/fs constants (e.g. 0.0002911 ↔ 0.0002675, the smoothing poles). The mix table,
  the FIR decimator and the vintage table don't depend on the rate.
- Patch offsets: +0x65 → +0x63 (Cutoff Scaling). The Argon engines' +0x02 Distortion drive has no Cobalt
  equivalent (use a constant or another parameter).
- **Easiest: Cobalt "L" modes → Argon.** One 3 KB engine and 25 new types. The Argon float path already feeds
  float blocks, so only the 0x43FCE8 switch, the clamps and the panel names change. **Also realistic: Argon
  Ladder / Hybrid / Phaser / Sallen-Key → Cobalt** (4 × ~3.5 KB plus a ≥31 branch in 0x43F410). **Hard:
  Argon Standard/Classic → Cobalt.** They are Q15 code wrapped around Argon's int16 voice path and int16
  coefficient tables, so a C reimplementation is more practical than a transplant. Cobalt's Reso LP/Balanced
  engine (7.6 KB, 228 literals) is portable in principle but is the largest float engine.
- **Carbon Dual (6 modes) and Adj Phaser → Cobalt: closest pair.** They share the sample rate, float mod values
  and patch tail. Glue: (a) a copy of Carbon's 214-byte wrapper (reso/warp ramps); (b) 2× input. Dual only takes
  even samples, so its stride can be patched to 1, or a shim can duplicate samples. Adj Phaser needs real
  2× input, so the shim should interpolate as Cobalt's L engine does. (c) Mod-routing offsets 0x26/0x28/0x2A →
  0x20/0x22/0x24. (d) The Dual mode table lives in RAM `.data`, so copy it into flash and repoint.
  Cobalt Reso LP/Balanced → Carbon is the reverse case: those engines expect 1× input, so a shim must decimate
  Carbon's 2× buffer first. Carbon → Argon has all the Cobalt → Argon issues plus the 2× input.
- Unknowns: CPU headroom on each slave (Cobalt's engines run 2× per sample), which state fields the type-change
  hooks (Argon flag at 0x20405920, Cobalt 0x43A5E9) reset, and where the second set of 4 voices runs.

## Boot screen (all three panels, `tools/boot_screen.py`)

One routine draws it: Argon 0x41C10C, Cobalt 0x41B928, Carbon 0x41ABB0. It is the same code in all three, apart from
addresses and the frame size. It is called repeatedly while booting. The display is a 128×64 **u8g2** instance at
0x2000CE20 (Carbon). Fonts are standard u8g2 fonts (23-byte header, RLE glyphs), and the helpers are the u8g2 ones
(Carbon: SetFont 0x411F10, GetStrWidth 0x411F38, DrawStr 0x411E84, DrawLine 0x412114, sprintf 0x43AE30).

- If the error flag (Carbon 0x2000A16C) is set, it draws the error screens instead ("Incorrect Firmware",
  "BOOT ERROR!" / "Please reboot...").
- Otherwise:
  - The name ("CARBON") is copied onto the stack at sp+0x24 with an inlined `ldm` + 7-byte store (6 bytes on Argon).
  - The suffix goes to sp+0x1c: "8M" if user-signature byte 4 is 2, "8X" if it is 4, else "8".
  - Name (large font) and suffix (second font) share a baseline at y = h_suffix/2 − h_small + 34 (31 on all three).
    They are centred together: x = 63 − (w_suffix/2 + w_name/2), with the suffix 2 px after the name.
  - The version line is `sprintf(buf, "v%d.%0d", maj, min)`, centred (64 − w/2) one small-font height lower.
    The version numbers are immediates (3.2 / 2.1 / 1.1). The buffer is 28 bytes, 24 on Cobalt.
  - A dash (x = c..c+10, y = 60) advances 1 px per call (counter at Carbon 0x2000759C, wraps at 128).
- **Patch points** (Carbon; Argon and Cobalt are listed in the tool):
  - Name: `add r1/r3, sp, #0x24` at 0x41AC06 / 0x41AC5A become `ldr r1/r3, [pc]` of the name literal 0x41AE34,
    which is repointed. The `ldm` copy still runs, so the new string must be word aligned with 8 bytes readable.
  - Suffix: `add r1/r3, sp, #0x1c` at 0x41AC14 / 0x41AC6E load the "8M" literal 0x41AE80 instead.
    The 8M branch's 3-byte copy from it is then dead.
  - Version format: literal 0x41AE54.
- **Settings > Version** is the parameter display routine's `sprintf(out, "CARBON8 %d.%0d", 1, 1)`, literal 0x41CCF8
  (Argon 0x41EE64, Cobalt 0x41E01C). On Argon its callers pass 20–30-byte buffers. The tool keeps the result to 13
  characters, the longest value the routine already writes.
- "Modal Electronics" / "CARBON8" (0x448C84) have no literal references in the panel.

## Feature differences (first pass: panel UI strings unique to each model)

- **Argon8 only:** wavetable/"Osc Spread"/"Voice Drift" engine names, the Argon filter set (Ladder,
  Sallen-Key, Classic/Standard Notch, Ladder Phaser …), the Argon shaper/derez/fold FX set, and sequencer
  settings (Metronome, Pre-Roll, Quantise, Transport) under different menu labels.
- **Cobalt8 only:** the Cobalt extended-VA oscillator algorithms (Chaos/Fractal/H-Sync/Metal/Noise AM/
  PWM/Reverse/Spread × Saw/Sqr/Tri, VA Crushed/Sweep), Balanced LP/HP/Phase and Reso LP filters, and
  Param A/B.
- **Carbon8 only:** Carbon's oscillator set (Alt Sync/Derez/Pinch/Scrunch/Shaper/Squash/Win Sync WT,
  Contour, Core/XCore), Dual LP/HP/BP/Peaking filters, Filter Warp, **LFO1–3 Transform**, S+H Ramped/
  Slewed, and per-oscillator Level and Contour.
- **Cobalt8 + Carbon8, not Argon8:** **a third LFO** (LFO2/LFO3 edit mode, LFO3 Rate/Shape/Depth), a
  Keyboard menu, Drift, and the "L …" notch/peaking/phaser filter family.

Most differences are DSP algorithms (master/slave) plus their UI tables (panel). Adding or porting one
needs code in the DSP images, the panel parameter/name tables, and the panel↔DSP messages.

## Open questions

- Map the panel↔master link (UART/I2C framing, `F0 00 21 07 <dev> …` command set). The parameter ID space
  and the master's ID → patch-byte dispatch are mapped (see "Patch storage"). The `f6`/`f7` fields of the
  panel records (display format?) are not decoded.
- Patch storage follow-ups: where the panel's patch copy is bulk-filled; the master's MIDI CC/NRPN → ID map;
  the 4 × 0x8E-byte master records at 0x204439BC that the get/set switches iterate over; and the
  sequencer layout in master flash 0x49C000–0x4BA000.
- Slave CPU headroom per block on each model (the main risk of the Carbon base), including the cost of a Cobalt alg at 96 kHz vs 48 kHz plus upsampling.
- Find the oscillator/FX algorithm dispatch tables in master/slave (needed for porting an engine). Filters are
  mapped (see "Filters").
- Free flash for injected code: images end at 0x456E00 (Argon panel), 0x486F80 (Argon master) and
  0x499B00 (Argon slave). Total flash size per MCU is not confirmed.
- Panel-side handling of the incoming `…7F` start message (entry into update mode) is not traced yet.
- Other user-signature fields and the variant differences (8 / 8X / 8M) are not mapped.
