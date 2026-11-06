/*
 * G.711 A-law 编解码正确性测试 (主机端)
 *
 * 用法: gcc -o g711_test g711.c g711_test.c -I. && ./g711_test
 *
 * 检查四件事:
 *   1. 编码 -> 解码 的往返误差在可接受范围内 (A-law 是有损压缩)
 *   2. 静音输入编码后解回来还是静音 (不产生假的底噪)
 *   3. 量化是单调的 (输入变大, 解码不能变小)
 *   4. 批量函数与单样本一致
 */

#include "g711.h"
#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include <string.h>

static int failures = 0;

static void check(int cond, const char *what)
{
    printf("  [%s] %s\n", cond ? "PASS" : "FAIL", what);
    if (!cond) failures++;
}

int main(void)
{
    printf("=== G.711 A-law test ===\n\n");

    /* ---- 1. round-trip error ---- */
    printf("1. round-trip error (1kHz sine)\n");
    {
        int worst_rel = 0;
        for (int amp = 100; amp <= 30000; amp *= 2) {
            int max_err = 0;
            int max_rel = 0;
            for (int i = 0; i < 320; i++) {
                double ph = 2.0 * 3.14159265358979 * 1000.0 * i / 16000.0;
                int16_t orig = (int16_t)(sin(ph) * amp);
                uint8_t enc = g711_alaw_encode(orig);
                int16_t dec = g711_alaw_decode(enc);
                int err = abs((int)orig - (int)dec);
                if (err > max_err) max_err = err;
                int rel = (amp > 0) ? (err * 100 / amp) : 0;
                if (rel > max_rel) max_rel = rel;
            }
            printf("   amp %5d: max abs err %5d, max rel err %d%%\n",
                   amp, max_err, max_rel);
            if (max_rel > worst_rel) worst_rel = max_rel;
        }
        check(worst_rel <= 15, "rel err <= 15%");
    }

    /* ---- 2. silence ---- */
    printf("\n2. silence\n");
    {
        int16_t dec_zero = g711_alaw_decode(g711_alaw_encode(0));
        check(dec_zero == 0, "0 -> encode -> decode == 0");

        int max_out = 0;
        for (int v = -30; v <= 30; v++) {
            int16_t d = g711_alaw_decode(g711_alaw_encode((int16_t)v));
            if (abs((int)d) > max_out) max_out = abs((int)d);
        }
        printf("   input +/-30 -> max decoded amp %d\n", max_out);
        check(max_out <= 64, "small signal not amplified into noise");
    }

    /* ---- 3. monotonicity ---- */
    printf("\n3. monotonic quantization\n");
    {
        int prev = -1;
        int mono = 1;
        for (int v = 0; v <= 32000; v += 97) {
            int16_t d = g711_alaw_decode(g711_alaw_encode((int16_t)v));
            int ad = (d < 0) ? -d : d;
            if (ad < prev) { mono = 0; break; }
            prev = ad;
        }
        check(mono, "decoded amp is monotonic non-decreasing");
    }

    /* ---- 4. block functions ---- */
    printf("\n4. block functions\n");
    {
        int16_t in[64], out[64];
        uint8_t enc[64];
        int i;
        for (i = 0; i < 64; i++) in[i] = (int16_t)(i * 300 - 9000);
        g711_alaw_encode_block(in, enc, 64);
        g711_alaw_decode_block(enc, out, 64);
        int ok = 1;
        for (i = 0; i < 64; i++) {
            if (out[i] != g711_alaw_decode(g711_alaw_encode(in[i]))) { ok = 0; break; }
        }
        check(ok, "block encode/decode matches single-sample");
    }

    printf("\n=== %s (%d failure(s)) ===\n", failures ? "FAILED" : "ALL PASS", failures);
    return failures ? 1 : 0;
}
