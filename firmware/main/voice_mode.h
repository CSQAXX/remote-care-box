/*
 * voice_mode —— 一套固件里的两种语音模式
 *
 * ============================ 这个模块解决什么 ============================
 *
 * 以前要「看护」和「小智聊天」两套固件来回整片刷 (一次两分钟,
 * 刷过去摄像头和看护全没了)。现在把两种用法装进【同一套固件】:
 *
 *   看护对讲 (默认, 出厂行为)   喊「晴老师」   -> 自家服务器 /v1/turn
 *   小智聊天                   喊「小康同学」 -> 切模式后开始聊天
 *
 * 切换只是改一个变量 + 屏幕提示, 不刷机、不重启、不丢任何数据。
 * 看护原有链路一行没动: 「晴老师」命中后走的还是原来那个
 * ai_client_start_turn_at(), 只是中间多了一层模式分发。
 *
 * ============================ 麦克风只有一套 ============================
 *
 * 板子只有一个麦克风一个喇叭, 两种模式【不能同时张嘴】。
 * 所以这里不做双开, 只做「谁在用」的标记:
 *   - 唤醒词任务常驻, 两句口令都认 (MultiNet7 支持多命令)
 *   - 命中哪句就切到哪个模式, 然后发起一轮对话
 *   - 一轮对话期间 ai_client 自己有互斥锁, 不会再起第二轮
 *
 * ============================ 分阶段落地 ============================
 *
 * 第一阶段 (本文件): 口令 + 模式开关 + 屏幕提示 + 统一分发入口。
 *   两种模式暂时共用已验证的 /v1/turn 链路, 所以「喊一声切过去」
 *   立刻就能聊天, 不是空壳。
 * 第二阶段: 把小智云端的对话协议接进 voice_mode_start_turn_at(),
 *   按 mode 分流。设备端交互逻辑不用再改, 只换后端。
 */

#ifndef VOICE_MODE_H
#define VOICE_MODE_H

#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    VOICE_MODE_CARE = 0,      /* 看护 / 自家对讲机 (开机默认) */
    VOICE_MODE_XIAOZHI = 1,   /* 小智聊天 */
} voice_mode_t;

/* 小智模式空闲多久自动回看护 (毫秒)。惰性判定, 不额外建任务。 */
#define VOICE_MODE_AUTO_BACK_MS   (10 * 60 * 1000)

/** @brief 当前模式 (顺手做一次空闲超时回退) */
voice_mode_t voice_mode_get(void);

/** @brief 切换模式; 只在真正变化时打日志 + 弹屏幕提示 */
void voice_mode_set(voice_mode_t mode);

/** @brief 模式的中文名 ("看护对讲" / "小智") */
const char *voice_mode_name(voice_mode_t mode);

/**
 * @brief 发起一轮对话 —— 所有语音入口的【唯一分发点】
 *
 * @param start_pos  绝对样本位置, 同 ai_client_start_turn_at()
 *
 * 现在两种模式都走 ai_client (/v1/turn)。以后接小智云端, 只改这里,
 * 按 s_mode 分流到不同后端, 唤醒词和交互逻辑都不用动。
 */
esp_err_t voice_mode_start_turn_at(int64_t start_pos);

/** @brief 打印模式状态 (串口命令 voice 用) */
void voice_mode_dump_status(void);

#ifdef __cplusplus
}
#endif

#endif /* VOICE_MODE_H */
