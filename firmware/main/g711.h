/*
 * g711.h —— G.711 A-law 编解码
 *
 * 用于设备上行音频 (老人麦克风 -> 云端 -> 子女端)。
 * 需求文档 3.6: 上行用 G.711 A-law 16kHz 单声道, 20ms 一包 (320 字节)。
 */

#pragma once

#include <stdint.h>
#include "esp_log.h"
#include "g711_ref_table.h"

/* PCM16 (int16) -> A-law (1 字节) */
uint8_t g711_alaw_encode(int16_t sample);

/* A-law (1 字节) -> PCM16 (int16) */
int16_t g711_alaw_decode(uint8_t byte);

/* 批量编码: samples 个 int16 -> samples 个字节 */
void g711_alaw_encode_block(const int16_t *in, uint8_t *out, int samples);

/* 批量解码: samples 个字节 -> samples 个 int16 */
void g711_alaw_decode_block(const uint8_t *in, int16_t *out, int samples);

/* 自检: 用 ITU-T 官方参考向量验证实现, 返回 0 = 通过 */
int g711_selftest(void);
