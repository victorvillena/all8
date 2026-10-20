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
| other flash use | user signature only | user storage 0x49C000–0x4FFFFF (factory reset erases it; patches 0x49C000–0x4BA000, settings 0x4FF000) | none seen |
| **free flash** | ≥164 KB (to 0x480000 on a 512 KB SAM4E8; ~676 KB if SAM4E16) | **84 KB** (0x486F80–0x49BFFF) | **~409 KB** (to 0x500000 if 1 MB) |
| RAM used (data+bss+stack) | 0x20000000–0x20012B20 (stack 12 KB) | 0x20400000–0x2044ED50 (stack 8 KB) | 0x20400000–0x2044FD08 (stack 8 KB) |
| fixed buffers near RAM top | none | 0x2045B000–0x2045FFFF (20 KB) | 0x2045F000–0x2045FFFF (4 KB) |
| heap (`_sbrk`) | 0x20012B20–0x2001FFFC (live newlib heap) | start 0x20460000 > limit 0x2045AFFC, so it always fails | same pattern (limit 0x2045EFFC) |
| **free RAM** | ≤ ~53 KB, shared with any runtime `malloc` | **~48.7 KB** (0x2044ED50–0x2045AFFF) | **~60.7 KB** (0x2044FD08–0x2045EFFF) |

"Free RAM" on master/slave means no literal-pool reference into that range and no working heap. Addresses
computed from other bases can't be fully excluded. The master's 1 MB size comes from its factory-reset loop
erasing up to exactly 0x500000. The slave's flash size is inferred (same family; its 630 KB image rules out
512 KB parts). Whether the bootloader limits a component's size is unknown.

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

- Map the panel↔master link (UART/I2C framing, `F0 00 21 07 <dev> …` command set) and the parameter ID space.
- Find the parameter tables (name, range, display format) in the panel and the matching dispatch in the master.
- Find the oscillator/filter/FX algorithm dispatch tables in master/slave (needed for porting an engine).
- Free flash for injected code: images end at 0x456E00 (Argon panel), 0x486F80 (Argon master) and
  0x499B00 (Argon slave). Total flash size per MCU is not confirmed.
- Panel-side handling of the incoming `…7F` start message (entry into update mode) is not traced yet.
- Other user-signature fields and the variant differences (8 / 8X / 8M) are not mapped.
