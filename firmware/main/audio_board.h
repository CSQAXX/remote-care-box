/*
 * 远程看护盒子 - 板载音频 (ES8311 codec + ES7210 麦克风 ADC)
 *
 * 硬件实测 (I2C0 总线扫描, SDA=3/SCL=2):
 *   0x18 -> ES8311  (ES8311_CODEC_DEFAULT_ADDR 0x30 >> 1 = 0x18)  ✓
 *   0x40 -> ES7210  (ES7210_CODEC_DEFAULT_ADDR 0x80 >> 1 = 0x40)  ✓
 *   0x21 -> GC0308 摄像头
 *   0x59 -> AW9523B IO 扩展器
 *   0x19 -> 未确认 (reg0=0x00, 待查)
 *
 * 引脚 (正点原子 BOX3 官方 board config):
 *   MCLK=21  WS=39  BCLK=38  DIN=41(codec->MCU 录音)  DOUT=40(MCU->codec 播放)
 *
 * 重要: 之前需求文档写的 "ES8388" 是裸板 DNESP32S3 的方案, 不是 BOX3。
 * ES8388 的引脚 (GPIO46/9/10/14/3) 和摄像头数据线 (GPIO9/10/46) 直接冲突,
 * 照抄会花屏。这里用的是 BOX3 真实的 ES8311+ES7210。
 *
 * 音频电源由 AW9523B 控制: XIO_VBAT_EN(12) / XIO_VDDA_3V3_EN(13) /
 * XIO_PA_CTRL(5), 在 camera_power_reset() 里已经拉高。
 */

#ifndef AUDIO_BOARD_H
#define AUDIO_BOARD_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 音频任务统一钉在哪个核上跑。
 *
 * 为什么必须钉: 环形缓冲在 PSRAM 里 (内部 RAM 不够),
 * 而 ESP32-S3 的 PSRAM 缓存【每核独立】—— 写指针在采集任务,
 * 读指针在 httpd 任务, 两个任务跑在不同核上会各自看到不同的缓存内容,
 * 表现为读到陈旧数据 / 音频撕裂。
 *
 * 视频那条链路 (cam_producer / lcd_preview / httpd) 也全在 core 0,
 * 两边保持一致, 避免 core 1 上的 Wi-Fi 任务干扰。
 */
#ifndef APP_CORE_ID
#define APP_CORE_ID 0
#endif

/* 音频初始化 (codec + 麦克风 ADC + I2S 通道)
 * 返回 ESP_OK 表示成功; 失败时音频功能不可用, 但不影响视频。
 */
esp_err_t audio_board_init(void);

/* 释放音频资源 */
void audio_board_deinit(void);

/* 录音: 读 raw PCM (交错立体声 16bit), 返回实际读到的帧数, <0 表示错误
 *
 * 注意: 这条 I2S RX 通道由内部采集任务独占, 外部调用会被互斥保护。
 * 产品功能请用 audio_board_read_mono() (取上行用的单声道数据)。
 */
int audio_board_read(int16_t *dest, int sample_count);

/* 播放: 写 raw PCM (交错立体声 16bit) */
int audio_board_write(const int16_t *src, int sample_count);

/* ---- 上行链路 (老人麦克风 -> 云端) ----
 *
 * 需求文档 3.6: 上行用 G.711 A-law 16kHz 单声道, 20ms 一包 (320 字节)。
 * 这里提供取单声道 PCM 的接口, 由上层 (HTTP handler) 再做 G.711 编码。
 */

/*
 * 取单声道 PCM (16kHz)。这是上行链路的数据源。
 * 返回实际取到的样本数 (最多 sample_count), 0 表示暂时没数据。
 *
 * 音频由内部采集任务统一从 I2S 取, 放进环形缓冲, 这里只是消费者 ——
 * 这样避免多个消费者 (监测任务 / HTTP handler) 抢同一个 I2S 通道。
 */
int audio_board_read_mono(int16_t *dest, int sample_count);

/* ---- 共享读通道 (2026-11-08 新增) ----
 *
 * 为什么需要:
 *   唤醒词任务 (wake_word) 和对话采集 (ai_client) 都要读麦克风。
 *   audio_board_read_mono() 是【破坏性读】, 两个消费者会互相抢数据 ——
 *   各拿一半, 唤醒词识别和语音上传都失真。
 *
 * 用法:
 *   int h = audio_board_reader_open();          // 申请一个独立游标
 *   audio_board_read_mono_shared(h, buf, 320);  // 读, 不影响别人
 *   audio_board_reader_close(h);
 *
 * audio_board_reader_rewind() 可以把游标往回退 (接续唤醒词之前的音频),
 * 这是"晴老师今天天气怎么样"不丢前半句的关键。
 */
int  audio_board_reader_open(void);
void audio_board_reader_close(int handle);
int  audio_board_read_mono_shared(int handle, int16_t *dest, int sample_count);
int  audio_board_reader_rewind(int handle, int back_samples);
int  audio_board_reader_seek(int handle, int64_t abs_pos, int back);
int64_t audio_board_mono_total(void);
int64_t audio_board_reader_pos(int handle);

/* ---- 下行链路 (云端 -> 设备喇叭) ---- */

/*
 * 播放单声道 PCM (16kHz) —— 会自动复制成双声道送进 I2S。
 * 这是下行链路的数据源 (子女端语音)。
 */
int audio_board_play_mono(const int16_t *src, int sample_count);

/*
 * 下行对讲是否正在播放。
 * v1 是半双工「按住说话」, 上行在这个期间必须停 ——
 * 否则喇叭的回声会被麦克风拾回去, 形成啸叫。
 */
bool audio_board_is_downlink_active(void);
void audio_board_set_downlink_active(bool active);

/* 录音音量 (dB), 范围约 -40 ~ +40 */
int audio_board_set_input_gain(float db);

/* 播放音量 (0-100) */
int audio_board_set_output_volume(int volume);

/* 当前输出音量 (0-100), 未初始化返回 -1 */
int audio_board_get_output_volume(void);

/* 当前麦克风输入增益 (dB) */
float audio_board_get_input_gain(void);

/* 静音开关 */
int audio_board_set_mute(bool mute);

/* 诊断: 读 codec 寄存器 */
int audio_board_read_reg(int reg, int *val);

/* 诊断: 读 ES7210 (麦克风 ADC) 寄存器 */
int audio_board_read_adc_reg(int reg, int *val);

/* 诊断: 一次打印 ES7210 关键寄存器 */
void audio_board_dump_adc_regs(void);

/* 音频已就绪? */
bool audio_board_is_ready(void);

/* 自检: 播放 440Hz 测试音 + 录音统计电平
 * 返回录音峰值 (0-32767), -1 表示失败。
 * 对着麦克风说话时峰值应明显 > 100。
 */
int audio_board_selftest(int duration_ms, int16_t *out_buf, int out_cap_samples);

/* ---- 持续运行的音频任务 (产品功能, 不只是自检) ---- */

/*
 * 启动录音监测任务: 每 AUDIO_MONITOR_PERIOD_MS 打一条日志,
 * 格式 "AUDIO: peak=xxx rms=xxx", 方便对着麦克风说话看电平是否跳动。
 * 不影响播放 —— 同一个 I2S 全双工通道, 录和放可以同时跑。
 */
esp_err_t audio_board_start_monitor(void);

/*
 * 循环播放测试音 (每 AUDIO_TONE_PERIOD_MS 播 AUDIO_TONE_DURATION_MS 的
 * 440Hz 正弦波)。用来确认喇叭真的能出声 —— 硬件问题(功放没使能/
 * 喇叭虚焊)光看 I2S 写入成功是发现不了的, 必须耳朵听。
 */
esp_err_t audio_board_start_tone(void);

/* 停止上面两个后台任务 */
void audio_board_stop_background(void);

/* 只停止测试音 (不动录音监测) */
void audio_board_stop_tone(void);

/*
 * 播放一次性提示音 (需求文档 §3.8「伴随提示音」)
 *
 * 音色: 880Hz -> 1174Hz, 各 120ms, 中间 60ms 静音 —— 手机通知音的
 * 「叮咚」结构, 老人最熟悉。
 *
 * 阻塞约 300ms。播放期间会自动静音上行 (避免回声被麦克风拾回去),
 * 播完恢复。
 */
void audio_board_play_prompt_beep(void);

/* 后台任务统计 (给串口命令用) */
typedef struct {
    uint32_t peak;        /* 最近一帧的峰值 */
    uint32_t rms;         /* 最近一帧的 RMS */
    uint32_t samples;     /* 累计采样样本数 */
    uint32_t frames;      /* 累计帧数 (左右声道算一帧) */
    bool     speaking;    /* RMS 超过阈值 = 有人在说话 */
    bool     tone_playing;/* 测试音任务是否在跑 */
    /*
     * 【2026-11-03 新增】左右声道分开统计。
     *
     * 为什么要分开: ES7210 上 MIC1/MIC2 是差分对, 两个麦克风的极性
     * 可能是反的。如果 L 和 R 拾到的是同一个信号但极性相反,
     * 降混 (L+R)/2 会把它们互相抵消 —— 结果就是:
     *   左右单看都有信号, 合起来却只剩底噪。
     * 分开统计才能一眼看出是不是这个问题。
     */
    uint32_t peak_l;      /* 左声道峰值 */
    uint32_t peak_r;      /* 右声道峰值 */
    uint32_t rms_l;       /* 左声道 RMS */
    uint32_t rms_r;       /* 右声道 RMS */
} audio_board_stats_t;

int audio_board_get_stats(audio_board_stats_t *out);

#ifdef __cplusplus
}
#endif

#endif /* AUDIO_BOARD_H */
