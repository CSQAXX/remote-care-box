/*
 * ai_client —— 板子直连云端 AI 网关 (大模型语音对话)
 *
 * ============================ 这个模块解决什么 ============================
 *
 * 目标: 让盒子像小爱同学一样, 老人直接说话就能和大模型聊天,
 *      **不依赖 PC 常开**, 也不依赖手机上装 App。
 *
 * 链路:
 *   板子麦克风 --(G.711 A-law 16k)--> 服务器 AI 网关 /v1/turn
 *              <--(PCM16 16k + 回复文本)---- 板子喇叭
 *
 * 为什么"板子直连服务器"而不是板子自己调大模型 API:
 *   1. ASR/LLM/TTS 都需要 HTTPS + 大 JSON + 流式, ESP32-S3 跑不动;
 *   2. API key 不能烧进固件 (固件是可以被整片 dump 出来的);
 *   3. 服务器侧要留对话历史、做告警事件入库, 这些都在服务器上更自然;
 *   4. 以后换模型 / 换供应商, 只改服务器, 板子不用重新烧。
 *
 * ============================ 为什么用 HTTPS 而不是 HTTP ============================
 *
 * 语音内容是【最敏感的个人数据】—— 老人说什么全在里面。
 * 明文 HTTP 走公网等于把老人的对话广播给沿途每一个节点。
 * 固件已开 CONFIG_MBEDTLS_CERTIFICATE_BUNDLE_DEFAULT_FULL,
 * 系统根证书打包在固件里, 可以直接校验服务器证书。
 *
 * ============================ 半双工: 为什么要静音上行 ============================
 *
 * 板子播放大模型回复时, 喇叭的声音会被麦克风 (ES7210) 收回去。
 * 如果这时还在往服务器传音频, 服务器会认为"老人又说话了",
 * 造成自问自答的无限循环 (误打断)。
 *
 * 两道防线:
 *   1. audio_board_set_downlink_active(true) —— 固件侧静音上行
 *   2. 本模块播放期间直接停掉采集 —— 不把音频推给服务器
 *
 * ============================ 断线重连 ============================
 *
 * 沿用 push_client.c 的指数退避 (1s -> 2s -> 4s -> ... -> 60s 封顶)。
 * 网络恢复后自动重连, 不需要看护端干预。
 */

#ifndef AI_CLIENT_H
#define AI_CLIENT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- 服务器地址 (编译期配置, 密钥不进固件) ---- */
#define AI_SERVER_HOST        "jiuzuozhushou.cn"
#define AI_SERVER_PORT        443

/*
 * 设备标识与密钥。
 *
 * 设备 ID 用板子 MAC (去掉冒号, 小写), 密钥必须和服务器
 * /kanhu-ai/data/devices.txt 里登记的一致。
 *
 * 【安全提醒】密钥在固件里是可被读出的 (整片 dump 就能拿到),
 * 所以这只能挡"误接入", 挡不了专业攻击。上生产环境应该:
 *   - 每台设备一把独立密钥 (现在就是)
 *   - 服务器侧配合限流 + 异常检测
 *   - 真正的量产方案应改成每台设备烧不同的证书
 */
#define AI_DEVICE_ID          "kanhu-dev-001"
#define AI_DEVICE_SECRET      "kanhu-dev-secret-001"

/* ---- 一轮对话的参数 ---- */

/* 采集时长上限 (秒)。老人一句话不会超过这个, 超了就强制截断。 */
#define AI_MAX_UTTERANCE_SEC      10

/* 静音多久算一句话说完 (毫秒) */
#define AI_SILENCE_END_MS        900

/* 起句前保留的音频 (毫秒), 避免吃掉第一个字 */
#define AI_PREROLL_MS            300

/* 连续几帧有声才认起句 (抗突发噪声) */
#define AI_START_FRAMES          3

/* 录音能量门限 (RMS)。实测板上静音 ~21, 人声 ~1000, 取中间值。 */
#define AI_RMS_THRESHOLD         350

/* 采样率与帧长 (固件侧固定 16kHz 单声道) */
#define AI_SAMPLE_RATE           16000
#define AI_FRAME_MS              20
#define AI_FRAME_SAMPLES         (AI_SAMPLE_RATE * AI_FRAME_MS / 1000)   /* 320 */

/* 退避 (与 push_client 一致) */
#define AI_BACKOFF_MIN_MS        1000
#define AI_BACKOFF_MAX_MS        60000

/* ---- 对话状态机 ---- */
typedef enum {
    AI_STATE_IDLE = 0,     /* 空闲, 没在对话 */
    AI_STATE_LISTENING,    /* 正在采集这一句 */
    AI_STATE_THINKING,     /* 已上传, 等服务器回复 */
    AI_STATE_SPEAKING,     /* 正在播放回复 */
    AI_STATE_ERROR,        /* 出错 (会退回 IDLE 并退避重试) */
} ai_state_t;

/**
 * @brief 启动 AI 对话客户端 (后台任务)
 *
 * 会在后台维护到服务器的连接, 掉线自动指数退避重连。
 * 失败也不影响其它功能 (视频/按键/告警照常)。
 */
esp_err_t ai_client_start(void);

/**
 * @brief 停止 AI 对话客户端
 */
void ai_client_stop(void);

/**
 * @brief 当前是否已连上服务器
 */
bool ai_client_is_connected(void);

/**
 * @brief 当前状态 (给 /health 和串口命令用)
 */
ai_state_t ai_client_get_state(void);

/**
 * @brief 当前状态的中文描述
 */
const char *ai_client_state_str(void);

/**
 * @brief 主动开始一轮对话 (相当于"唤醒")
 *
 * 手动触发时用。自动唤醒 (esp-sr) 走的是同一个内部流程。
 */
esp_err_t ai_client_start_turn(void);

/**
 * @brief 从指定的绝对样本位置开始一轮对话 (唤醒词接续用)
 *
 * @param start_pos   绝对样本位置 (16kHz 单声道累计样本数)。
 *                    >= 0: 从这个位置开始采集 —— 唤醒词模块命中时把
 *                    "命中前 2 秒"的位置传进来, 于是唤醒词本身和它后面
 *                    接的半句话都不会丢。
 *                    < 0:   从"此刻"开始采集 (相当于 start_turn)。
 *
 * 音频来源是 audio_board 的共享读通道, ai_client 会自己申请一个
 * 独立游标 —— 和唤醒词任务各读各的, 互不干扰。
 */
esp_err_t ai_client_start_turn_at(int64_t start_pos);

/**
 * @brief 统计信息 (给 /health 用)
 */
typedef struct {
    uint32_t turns;          /* 累计对话轮次 */
    uint32_t replies;        /* 累计成功回复 */
    uint32_t errors;         /* 累计错误 */
    uint32_t connect_fails;  /* 累计连接失败 */
    uint32_t last_reply_len; /* 最近一次回复的字节数 */
    uint32_t backoff_ms;     /* 当前退避时长 */
    bool     connected;      /* 当前是否已连接 */
    ai_state_t state;        /* 当前状态 */
    char     last_user[64];  /* 最近一次识别到的用户文本 */
    char     last_reply[128];/* 最近一次大模型回复 */
} ai_client_stats_t;

void ai_client_get_stats(ai_client_stats_t *out);

/**
 * @brief 最近一轮的对话文本 (给屏幕显示用)
 */
const char *ai_client_last_user_text(void);
const char *ai_client_last_reply_text(void);

#ifdef __cplusplus
}
#endif

#endif /* AI_CLIENT_H */
