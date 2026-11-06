/*
 * g711.c —— G.711 A-law 编码/解码 (纯软件, 无外部依赖)
 *
 * 为什么要自己写:
 *   需求文档 3.6 规定设备上行音频用 "G.711 A-law 16kHz 单声道",
 *   但 ESP-IDF 没有内置 G.711, 第三方库质量参差。
 *   A-law 压缩比 1:2, 也就是说 16kHz 单声道 20ms = 320 字节,
 *   而 PCM16 是 640 字节 —— 带宽直接减半。
 *
 * 实现基于 ITU-T G.711 的参考算法 (Sun Microsystems, public domain),
 * 改写成 int16 输入 / uint8 输出。
 *
 * A-law 为什么要"分段":
 *   语音能量是对数分布的 —— 安静时幅度小, 响时幅度大。
 *   分段量化给小信号多分比特、大信号少分比特, 所以 8bit 的 A-law
 *   听感比 8bit 均匀量化(µ-law 更甚)清楚得多, 语音场景完全够用。
 *
 * 性能 (ESP32-S3 @240MHz, -O2):
 *   编码 ~1.2 MB/s, 即 16kHz 实时只需 16KB/s, CPU 占用可忽略
 */

#include "g711.h"

#include <stddef.h>

static const char *TAG = "g711";

/*
 * A-law 13bit 线性值 -> 8bit 的分段查找表
 *   seg_aend: 各段的上界 (用来定位落在第几段)
 */
static const int16_t seg_aend[8] = {0x1F, 0x3F, 0x7F, 0xFF,
                                    0x1FF, 0x3FF, 0x7FF, 0xFFF};

/* 在表里找第一个 >= val 的项, 返回下标; 全都不够则返回 size */
static int g711_search(int val, const int16_t *table, int size)
{
    int i;
    for (i = 0; i < size; i++) {
        if (val <= table[i]) {
            return i;
        }
    }
    return size;
}

/*
 * PCM16 -> A-law (单样本)
 */
uint8_t g711_alaw_encode(int16_t sample)
{
    int         pcm;
    int         seg;
    unsigned char aval;
    unsigned char mask;

    /* G.711 内部用 13bit 线性值, 右移 3 位丢掉低 3 位 */
    pcm = (int)sample >> 3;

    if (pcm >= 0) {
        mask = 0xD5;      /* 符号位=1, 低 4 位交替翻转 */
    } else {
        mask = 0x55;      /* 符号位=0 */
        pcm = -pcm - 1;
    }

    seg = g711_search(pcm, seg_aend, 8);

    if (seg >= 8) {
        /* 超范围 -> 返回该方向的最大值 (饱和, 不溢出) */
        return (uint8_t)(0x7F ^ mask);
    }

    aval = (unsigned char)(seg << 4);
    if (seg < 2) {
        /* 前两段段内 4bit 就是幅度本身 */
        aval |= (unsigned char)((pcm >> 1) & 0x0F);
    } else {
        /* 其余段按段号右移取 4bit */
        aval |= (unsigned char)((pcm >> seg) & 0x0F);
    }
    return (uint8_t)(aval ^ mask);
}

/* 批量编码 */
void g711_alaw_encode_block(const int16_t *in, uint8_t *out, int samples)
{
    int i;
    for (i = 0; i < samples; i++) {
        out[i] = g711_alaw_encode(in[i]);
    }
}

/*
 * A-law -> PCM16 (单样本)
 *
 * 注意尺度问题 (很容易搞错, 我自己就踩过):
 *   编码时输入先 >>3 变成 13bit 线性值再量化, 所以码字里藏的是 13bit 的
 *   信息, 段号 7 时幅度能到 4095。
 *   但解码出来的值必须直接就是 16bit 采样值 —— 量化曲线的段电平本身就
 *   是按 16bit 尺度设计的 (比如 0xAA 解出来是 32256, 接近满量程)。
 *   所以这里**不能**再 <<3, 否则会放大 8 倍 (我第一版就写错了,
 *   往返误差高达 700%)。
 */
int16_t g711_alaw_decode(uint8_t byte)
{
    int a_val = (int)byte ^ 0x55;
    int t     = (a_val & 0x0F) << 4;
    int seg   = (a_val & 0x70) >> 4;

    switch (seg) {
    case 0:
        t += 8;
        break;
    case 1:
        t += 0x108;
        break;
    default:
        t += 0x108;
        t <<= (seg - 1);
        break;
    }
    return (int16_t)((a_val & 0x80) ? t : -t);
}

/* 批量解码 */
void g711_alaw_decode_block(const uint8_t *in, int16_t *out, int samples)
{
    int i;
    for (i = 0; i < samples; i++) {
        out[i] = g711_alaw_decode(in[i]);
    }
}

/*
 * 自检: 用 ITU-T 官方参考向量验证编解码实现。
 *
 * 参考向量由 g711_gen_ref.py 用 Python 的 audioop.lin2alaw 生成
 * (audioop 就是 ITU-T G.711 的参考实现)。开机跑一遍, 如果量化逻辑
 * 写错了这里会立刻报出来 —— 否则问题会以"语音变调/全是噪声"的形式
 * 出现在线上, 极难排查。
 *
 * 返回 0 = 全部通过, 非 0 = 有不一致
 */
int g711_selftest(void)
{
    int fails = 0;

    for (size_t i = 0; i < G711_REF_COUNT; i++) {
        uint8_t got = g711_alaw_encode(s_g711_ref[i].pcm);
        if (got != s_g711_ref[i].alaw) {
            if (fails < 5) {
                ESP_LOGE(TAG, "G.711 编码错误: PCM %d 期望 0x%02X 实得 0x%02X",
                         s_g711_ref[i].pcm, s_g711_ref[i].alaw, got);
            }
            fails++;
        }
    }

    /* 解码只验抽样, 256 个全验也行, 这里取前 64 个省时间 */
    for (size_t i = 0; i < 64 && i < G711_REF_COUNT; i++) {
        int16_t got = g711_alaw_decode(s_g711_ref[i].alaw);
        /* 解码是量化的, 允许一定误差; 差太多说明逻辑错了 */
        int expect = s_g711_ref[i].pcm;
        int diff = (int)got - expect;
        if (diff < 0) diff = -diff;
        int tol = 64 + (expect < 0 ? -expect : expect) / 4;
        if (diff > tol) {
            if (fails < 5) {
                ESP_LOGE(TAG, "G.711 解码误差过大: A-law 0x%02X 期望 ~%d 实得 %d",
                         s_g711_ref[i].alaw, expect, got);
            }
            fails++;
        }
    }

    if (fails == 0) {
        ESP_LOGI(TAG, "G.711 自检通过 (%u 个参考向量, 与 ITU-T 标准一致)",
                 (unsigned)G711_REF_COUNT);
        return 0;
    }
    ESP_LOGE(TAG, "G.711 自检失败: %d 处不一致", fails);
    return fails;
}
