/*
 * Carbon8 slave glue for the Cobalt8 oscillator algorithms (see NOTES.md, "Cobalt oscillators on Carbon").
 *
 * Carbon renders each oscillator through a dispatcher (0x41A130) that calls
 *   render(state, out[256], contour[128], osc)
 * with 256 float samples at 96 kHz per 128-sample block. The build hooks that dispatcher so Cores
 * 56..95 land in cobalt_render() below instead.
 *
 * The 40 Cobalt algorithms are copied verbatim from the Cobalt8 2.1 slave. Each one is
 *   alg(out[128], voice_state, alg_index, osc)
 * and renders 128 samples at 48 kHz. Besides its voice state, an algorithm reads a handful of Cobalt
 * globals: the patch (Param A at +6/+7, Param B at +9/+10), the mod-routing table and mod buffers,
 * and four "modulation active" flags. The build points those literals at the cv_* objects here, so the
 * algorithms see a small Cobalt-shaped view that this file fills from Carbon's state on every call.
 *
 * Parameter A is Carbon's Contour, fed per sample through the algorithm's own modulation input,
 * computed exactly as Carbon's cores do (contour + control mod + audio mod A + depth * audio mod B).
 * Parameter B comes from patch +0x66 (osc 1) / +0x67 (osc 2).
 * The output is upsampled 2x with a halfband FIR into Carbon's 96 kHz buffer.
 */
#include <stdint.h>

#define N_VOICES   4
#define N_ALGS     40
#define CORE_BASE  56
#define BLOCK      128
#define COBALT_STATE_SIZE 0x290
#define ENTRY      0x44            /* one phase accumulator: 8 per voice, osc 0/1 interleaved */
#define HB_TAPS    8               /* halfband taps per side */

typedef void (*cobalt_alg)(float *out, uint8_t *state, int alg, int osc);

/* ---- addresses resolved by the build (Carbon 1.1 slave) ---- */
extern volatile int32_t ca_core[2];          /* selected Core per oscillator */
extern volatile uint8_t ca_patch[128];       /* slave patch copy */
extern volatile float   ca_mod_ctrl[];       /* control-rate mod values [slot*4 + voice] */
extern volatile float   ca_mod_a[];          /* audio-rate mod buffers [(slot*4 + voice)*128 + i] */
extern volatile float   ca_mod_b[];
extern volatile int16_t ca_route[];          /* mod destination -> slot, Contour 1/2 at [0]/[1] */
extern volatile uint8_t ca_contour_mod_on[]; /* [0] osc 1, [2] osc 2 */
extern volatile uint8_t ca_unipolar[];       /* [osc] */

/* ---- relocated Cobalt 2.1 slave tables ---- */
extern const cobalt_alg co_algs[N_ALGS];     /* built by the tool: the 40 relocated algorithms */
extern const float   co_ratio[3 * 128];      /* 0x455D4C: sub-oscillator ratio per Param B */
extern const uint8_t co_nsub[N_ALGS];        /* 0x456998: phase accumulators used per algorithm */
extern const uint8_t co_spread[N_ALGS];      /* 0x456970: algorithm randomizes sub-osc phases */
extern const uint8_t co_b_aligned[128];      /* 0x4511BC: Param B values that keep sub-oscs aligned */

/* ---- the Cobalt-shaped view the relocated algorithms read ---- */
uint8_t cv_patch[128];
int16_t cv_route[64];
uint8_t cv_unipolar[8];
uint8_t cv_mod_on_a1, cv_mod_on_b1, cv_mod_on_a2, cv_mod_on_b2;
float   cv_mod_ctrl[3 * N_VOICES];
float   cv_mod_a[3 * N_VOICES * BLOCK];      /* slot 0: zero, slot 1: osc 1 Param A, slot 2: osc 2 */
float   cv_mod_b[3 * N_VOICES * BLOCK];      /* stays zero */

static uint8_t  cv_state[N_VOICES][COBALT_STATE_SIZE] __attribute__((aligned(8)));
static int8_t   cv_alg[N_VOICES][2];
static float    cv_hist[N_VOICES][2][2 * HB_TAPS];
static uint32_t cv_rand;
/* Carbon's startup doesn't clear this RAM, so the view is set up on first use, keyed on a magic word.
   volatile keeps the full 32-bit compare: as a plain static, clang assumes it starts at zero and shrinks
   it to a one-byte flag, which random RAM after power-on almost always passes. */
static volatile uint32_t cv_magic;

#define MAGIC 0xC0BA1708u

/* 2x halfband interpolator, odd phase: y = sum h[k] * (x[c+1+k] + x[c-k]).
   Kaiser-windowed sinc (beta 5): -0.17 dB at 20 kHz, < -34 dB above 28 kHz, < -53 dB above 30 kHz. */
static const float hb[HB_TAPS] = {
    0.63184326f, -0.19629739f, 0.10200048f, -0.05823378f,
    0.03299897f, -0.01752942f, 0.00820151f, -0.00298363f,
};

static void clear(void *p, uint32_t n)
{
    uint32_t *w = p;
    for (n >>= 2; n; n--)
        *w++ = 0;
}

static float clamp01(float x)
{
    return x < 0.0f ? 0.0f : x > 1.0f ? 1.0f : x;
}

static uint32_t rnd(void)
{
    cv_rand = cv_rand * 1664525u + 1013904223u;
    return cv_rand;
}

static void init_view(void)
{
    clear(cv_patch, sizeof cv_patch);
    clear(cv_route, sizeof cv_route);
    clear(cv_unipolar, sizeof cv_unipolar);
    clear(cv_mod_ctrl, sizeof cv_mod_ctrl);
    clear(cv_mod_a, sizeof cv_mod_a);
    clear(cv_mod_b, sizeof cv_mod_b);
    clear(cv_state, sizeof cv_state);
    clear(cv_hist, sizeof cv_hist);
    cv_route[0] = 1;                   /* Param A osc 1 -> slot 1 */
    cv_route[1] = 2;                   /* Param A osc 2 -> slot 2 */
    cv_mod_on_a1 = cv_mod_on_a2 = 1;   /* Param A always arrives through the mod input */
    cv_mod_on_b1 = cv_mod_on_b2 = 0;
    for (int v = 0; v < N_VOICES; v++) {
        cv_state[v][0x234] = (uint8_t)v;
        cv_alg[v][0] = cv_alg[v][1] = -1;
    }
    cv_rand = 0x1234567u;
    cv_magic = MAGIC;
}

/* Cobalt 0x4161E0: clear one oscillator's four phase accumulators */
static void reset_osc(uint8_t *st, int osc)
{
    for (int k = osc; k < 8; k += 2) {
        uint8_t *e = st + k * ENTRY;
        *(float *)(e + 0x08) = 0.0f;
        *(float *)(e + 0x2c) = 0.0f;
        e[0x10] = e[0x11] = e[0x12] = 0;
        for (int o = 0x14; o <= 0x28; o += 4)
            *(float *)(e + o) = 0.0f;
    }
}

/* Cobalt 0x416550 with Free Run off: note-on phase reset for one oscillator */
static void note_reset(uint8_t *st, int osc, int alg, int b)
{
    for (int k = osc; k < 8; k += 2) {
        uint8_t *e = st + k * ENTRY;
        *(float *)(e + 0x08) = 0.0f;
        *(float *)(e + 0x2c) = 0.0f;
        e[0x10] = 0;
        e[0x12] = 0;
        *(float *)(e + 0x28) = 0.0f;
        *(float *)(e + 0x24) = 0.0f;
        e[0x11] = 1;
        for (int o = 0x14; o <= 0x20; o += 4)
            *(float *)(e + o) = 0.0f;
    }
    if (co_spread[alg] && !co_b_aligned[b])
        for (int k = osc + 2; k < 8; k += 2)
            *(float *)(st + k * ENTRY + 0x08) = (float)(rnd() >> 8) * (1.0f / 16777216.0f) * 0.25f;
}

void cobalt_render(uint8_t *cstate, float *out, const float *contour, int osc)
{
    float x[BLOCK + 2 * HB_TAPS];
    int voice = cstate[0xe4] & (N_VOICES - 1);
    int alg = ca_core[osc] - CORE_BASE;
    uint8_t *st;

    if (cv_magic != MAGIC)
        init_view();
    if (alg < 0 || alg >= N_ALGS) {
        clear(out, 2 * BLOCK * sizeof(float));
        return;
    }
    st = cv_state[voice];

    int b = ca_patch[0x66 + osc] & 0x7f;
    cv_patch[9 + osc] = (uint8_t)b;
    st[0x23c + osc] = (uint8_t)b;
    st[0x25c + osc] = (uint8_t)alg;
    st[0x264 + osc] = co_spread[alg];

    if (cv_alg[voice][osc] != alg) {
        reset_osc(st, osc);
        cv_alg[voice][osc] = (int8_t)alg;
    }
    /* Carbon zeroes the oscillator state on note-on unless Free Run is set (0x410F88); the marker
       written at the end of every call tells us when that happened. */
    if (*(float *)cstate != 0.5f && !(ca_patch[0x02] & 1))
        note_reset(st, osc, alg, b);

    /* phase increments: Carbon's final frequency (Hz, +0x10) times Cobalt's sub-oscillator ratios */
    float f = *(float *)(cstate + 0x10);
    for (int j = 0; j < 4; j++) {
        float r = j ? co_ratio[(j - 1) * 128 + b] : 1.0f;
        float fj = f * r;
        uint8_t *e = st + (osc + 2 * j) * ENTRY;
        *(float *)(e + 0x04) = fj;
        *(float *)(e + 0x0c) = fj < 10000.0f ? fj * (1.0f / 48000.0f) : 0.2083333f;
    }

    /* Param A: Carbon's per-sample contour with Carbon's modulation, into Cobalt mod slot osc+1 */
    float *pa = &cv_mod_a[((osc + 1) * N_VOICES + voice) * BLOCK];
    if (ca_contour_mod_on[2 * osc]) {
        int slot = ca_route[osc];
        int base = (slot * N_VOICES + voice) * BLOCK;
        float ctrl = ca_mod_ctrl[slot * N_VOICES + voice];
        float depth = clamp01(contour[0] + ctrl);
        if (!ca_unipolar[osc])
            depth = 1.0f - depth;
        for (int i = 0; i < BLOCK; i++)
            pa[i] = clamp01(contour[i] + ctrl + ca_mod_a[base + i] + depth * ca_mod_b[base + i]);
    } else {
        for (int i = 0; i < BLOCK; i++)
            pa[i] = contour[i];
    }
    cv_patch[6 + osc] = 0;

    /* render 128 samples at 48 kHz after the interpolator's history */
    float *h = cv_hist[voice][osc];
    for (int i = 0; i < 2 * HB_TAPS; i++)
        x[i] = h[i];
    co_algs[alg](x + 2 * HB_TAPS, st, alg, osc);
    /* Some algorithms emit a few huge samples right after a phase reset (e.g. Fold Tri: ~-3900 on
       the first sample, FM Squ Squ: ~1e6 for 4 samples at Param B 127), which stock Cobalt does too.
       Normal output stays within +-1, so clip at +-2 (NaN -> +2) to keep Carbon's filters sane. */
    for (int i = 2 * HB_TAPS; i < BLOCK + 2 * HB_TAPS; i++) {
        float v = x[i] < 2.0f ? x[i] : 2.0f;
        x[i] = v > -2.0f ? v : -2.0f;
    }
    for (int i = 0; i < 2 * HB_TAPS; i++)
        h[i] = x[BLOCK + i];

    /* 2x halfband upsampling, delay HB_TAPS input samples */
    for (int n = 0; n < BLOCK; n++) {
        const float *c = x + n + HB_TAPS;
        float y = 0.0f;
        for (int k = 0; k < HB_TAPS; k++)
            y += hb[k] * (c[1 + k] + c[-k]);
        out[2 * n] = c[0];
        out[2 * n + 1] = y;
    }

    *(float *)cstate = 0.5f;
}
