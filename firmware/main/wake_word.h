/*
 * wake_word —— 离线唤醒词「晴老师」
 *
 * ============================ 这个模块解决什么 ============================
 *
 * 目标: 老人说出「晴老师」三个字, 盒子就开始和大模型对话。
 *       唤醒必须【离线】—— 网络断了也得能叫醒, 否则盒子就变哑巴。
 *
 * ============================ 为什么用 MultiNet7 而不是 WakeNet ============================
 *
 * WakeNet 是乐鑫官方模型, 97 个固定唤醒词, 但里面【没有「晴老师」】。
 * MultiNet7 (mn7_cn) 是「自由指令」模型: 支持运行时注册任意中文命令词,
 * 只要给出拼音就行。这是唯一能做出真正自定义唤醒词的官方路线。
 *
 * 关键技术事实 (已在 esp-sr 2.6.1 的库里核实过):
 *
 *   1. 模型名注册表里只有 "mn5q8" / "mn6" / "mn7", 匹配是【前缀匹配】,
 *      所以 "mn7_cn" 能命中 mn7 的接口。
 *   2. mn7 的 create() 内部会自动:
 *        - 从 model 分区读 vocab (词表)
 *        - 从 model 分区读 commands_cn.txt (命令词表, 格式 "id,pin yin")
 *        - 对每条命令调 esp_mn_commands_add()
 *      所以把「晴老师」写进 commands_cn.txt, 开机就自动生效。
 *   3. mn7 的命令词单位是【无声调拼音】(词表里只有 ▁qing ▁lao ▁shi,
 *      没有 qing2 / lao3 / shi1), 所以写 "qing lao shi", 不是 "qing2 lao3 shi1"。
 *   4. 运行时注册也支持: esp_mn_commands_add() + esp_mn_commands_update()。
 *      本模块两条都做 —— 打包时用 commands_cn.txt 保证开机即用,
 *      运行时再补一次做兜底 (万一分区里的命令表是旧的)。
 *   5. mn7 不支持编译期 FST (esp_mn_commands_update_from_sdkconfig 对
 *      MULTINET7 直接 return NULL), 所以【不能】用 sdkconfig 里的
 *      CN_SPEECH_COMMAND_ID0 —— 那条路只对 mn2/mn4/mn5 有效。
 *
 * ============================ 半双工与音频接续 ============================
 *
 * 唤醒词任务和对话采集都要读麦克风。两个都读 audio_board_read_mono()
 * 会互相抢 (破坏性读), 所以本模块用 audio_board 的【共享读通道】
 * (独立游标), 谁也不影响谁。
 *
 * 「晴老师今天天气怎么样」如果只从唤醒词【之后】开始录, 前半句就丢了。
 * 所以本模块一直保留最近 2 秒的音频, 命中时把这段(含唤醒词本身)
 * 交给 ai_client 当作起句素材。
 */

#ifndef WAKE_WORD_H
#define WAKE_WORD_H

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- 唤醒词规格 ---- */

/* 唤醒词显示名 (屏幕 / 日志 / 串口命令都用这个) */
#define WAKE_WORD_NAME      "晴老师"

/* 拼音 (无声调, 必须是 mn7 词表里的 token) */
#define WAKE_WORD_PINYIN    "qing lao shi"

/*
 * 命令词 ID (mn7 的 command_id)
 *
 * = 「晴老师」在 model 分区 commands_cn.txt 里的行号。
 *
 * 【不要改成 0】—— 之前这里填 0 是配合运行时 esp_mn_commands_add(0, ...)
 * 用的。现在「晴老师」是烘焙进 commands_cn.txt 的第 314 条
 * (官方 313 条通用命令原样保留在 1..313), create() 会自动注册,
 * 所以 ID 必须是 314。
 *
 * 顺便记一笔: 就算想用运行时注册, ID 0 也不行 ——
 * 运行时 esp_mn_commands_add(id, ...) 走的是和打包时不同的路径,
 * 但只要 update() 编译出的命令表太短 (1 条), 一样会 panic。
 */
#define WAKE_WORD_CMD_ID    314

/* ---- 第二句口令：「小康同学」= 切到小智聊天模式 ----
 *
 * 同一套固件里装两种用法, 靠两句口令切换, 不刷机不重启:
 *   「晴老师」  -> 看护对讲 (出厂行为, 原有链路一行不动)
 *   「小康同学」-> 小智聊天
 *
 * 拼音已核对 mn7_cn/vocab: ▁xiao ▁kang ▁tong ▁xue 四个音节都在。
 * ID 315 是它在 commands_cn.txt 里的行号 (官方 313 条 + 314 晴老师)。
 * 见 voice_mode.h。
 */
#define WAKE_WORD_XIAOZHI_NAME      "小康同学"
#define WAKE_WORD_XIAOZHI_PINYIN    "xiao kang tong xue"
#define WAKE_WORD_XIAOZHI_CMD_ID    315


/* PC_VOICE_PATCH v1 (删掉这整段即可还原) —— 第三个叫法「小电脑」 */
#ifdef ENABLE_PC_VOICE
#define WAKE_WORD_PC_NAME        "小电脑"
#define WAKE_WORD_PC_PINYIN      "xiao dian nao"
#define WAKE_WORD_PC_CMD_ID      316   /* commands_cn.txt 第 316 行 */
#endif

/* 检测门限 (0.0~0.9999)。
 * 模型自带门限是 0.90 (见 _MODEL_INFO_), 对三字唤醒词偏严,
 * 老人发音没那么标准, 这里放宽到 0.50。
 * 串口 "wake threshold 0.6" 可以现场调。 */
#define WAKE_WORD_THRESHOLD 0.30f

/* 识别超时 (毫秒) —— 一轮识别最多等这么久 */
#define WAKE_WORD_TIMEOUT_MS 12000

/* 保留多少音频用于接续 (毫秒)。
 * 唤醒词本身大概 600~900ms, 留 2 秒足够把整句都包进去。 */
#define WAKE_WORD_PREROLL_MS 2000

/* ---- 对外接口 ---- */

/**
 * @brief 启动唤醒词识别 (后台任务)
 *
 * 会加载 MultiNet7 中文模型 (2.6MB, 从 model 分区读) 并注册「晴老师」。
 * 失败不影响其它功能 (视频/按键/告警照常)。
 */
esp_err_t wake_word_start(void);

/**
 * @brief 停止唤醒词识别
 */
void wake_word_stop(void);

/**
 * @brief 唤醒词是否已就绪 (模型加载成功 + 命令词注册成功)
 */
bool wake_word_is_ready(void);

/**
 * @brief 累计命中次数
 */
uint32_t wake_word_hit_count(void);

/**
 * @brief 最近一次命中的概率 (0.0~1.0)
 */
float wake_word_last_prob(void);

/**
 * @brief 设置检测门限 (0.0~0.9999), 立即生效
 */
esp_err_t wake_word_set_threshold(float threshold);

/**
 * @brief 手动触发一次唤醒 (等价于说出「晴老师」)
 *
 * 串口命令 "wake" 用。用于排障 —— 把唤醒这一环摘掉单独验证后面的链路。
 */
esp_err_t wake_word_trigger_manual(void);

/**
 * @brief 打印唤醒词状态 (串口命令用)
 */
void wake_word_dump_status(void);

#ifdef __cplusplus
}
#endif

#endif /* WAKE_WORD_H */
