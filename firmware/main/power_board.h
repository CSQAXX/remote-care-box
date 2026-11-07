/*
 * 电源 / 电池管理 (正点原子 ATK_DNESP32S3_BOX3)
 *
 * 为什么需要这个模块:
 *   BOX3 底板带 18650 电池座 + 充电电路, 板载电源轨全部由 AW9523B
 *   扩展器经 I2C 开关控制。拔掉 Type-C 后:
 *     - 若底板有电池且电池有电 -> 电池接管供电, 设备继续工作
 *     - 若没有电池            -> 主 3.3V 消失 -> AW9523B 停止工作
 *                              -> 摄像头/音频/背光全部断电 -> 屏幕全灭
 *   所以"拔线就没画面"是供电问题, 不是固件 bug。
 *
 * 本模块负责读出【当前供电来源】和【电池电量】, 让软件能区分:
 *   - 设备被人拔了电 (需要告警)
 *   - 设备正在充电 (正常)
 *
 * 权威依据:
 *   正点原子官方 xiaozhi 固件
 *   main/boards/atk-dnesp32s3-box3/config.h      (XIO 编号)
 *   main/boards/atk-dnesp32s3-box3/power_manager.h (采样时序 + 分档表)
 *
 * 硬件连接:
 *   XIO2 (XIO_BAT_CHRG_EN) 输出 -> 拉低导通分压电阻
 *   XIO3 (XIO_BAT_CHRG)    输入 -> 低 = 正在充电
 *   GPIO1 (ADC1_CH0)             <- 分压后的电池电压
 */

#ifndef POWER_BOARD_H
#define POWER_BOARD_H

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 供电来源 */
typedef enum {
    POWER_SOURCE_UNKNOWN = 0,   /* 读不出来 (扩展器无应答等) */
    POWER_SOURCE_TYPEC   = 1,   /* USB Type-C 供电 */
    POWER_SOURCE_BATTERY = 2,   /* 电池供电 */
} power_source_t;

/* 电量信息快照 */
typedef struct {
    power_source_t source;      /* 当前供电来源 */
    bool           charging;    /* 是否正在充电 */
    bool           valid;       /* adc 是否读成功 */
    int            adc_raw;     /* ADC 原始平均��� */
    int            mv;          /* 换算出的电池电压 (mV), 读失败为 0 */
    int            percent;     /* 电量百分比 0-100 */
} power_status_t;

/**
 * @brief 初始化 ADC 单元与通道
 *
 * 幂等: 重复调用不会报错, 第二次直接返回 ESP_OK。
 * 必须在 AW9523B 初始化之后调用 (采样时序依赖扩展器)。
 *
 * @return ESP_OK 成功
 */
esp_err_t power_board_init(void);

/**
 * @brief 读取电池电压并换算成电量百分比
 *
 * 注意: 这是【阻塞】调用, 内部有 500ms 延时 (等分压稳定, 官方硬性要求)。
 *       不要放进高频任务或 stream 回调里, 只在需要时调用。
 *
 * @param out_adc  可选, 输出 ADC 原始平均值
 * @param out_mv   可选, 输出换算电压 mV
 * @param out_pct  可选, 输出电量百分比 0-100
 * @return ESP_OK 成功; ESP_ERR_TIMEOUT ADC 读取失败
 */
esp_err_t power_board_read_battery(int *out_adc, int *out_mv, int *out_pct);

/**
 * @brief 当前供电来源
 *
 * 判据 (与官方一致): XIO_BAT_CHRG 为【低】表示正在充电 => Type-C 供电;
 *                    为【高】表示没在充 => 电池供电。
 *
 * 注意: 这个判据在【没插电池】时也不准 —— 没有电池的话充电芯片不会
 *       拉低充电检测脚, 会被判成"电池供电"。要真正确认电池在不在,
 *       必须结合 power_board_read_battery() 读到的电压。
 */
power_source_t power_board_get_source(void);

/**
 * @brief 判定供电来源 (显式传入电量读数)
 *
 * 与 power_board_get_source() 的区别: 这个版本用【调用者传入的】电压做
 * 交叉验证, 不会读到过期的缓存。
 */
power_source_t power_board_determine_source(const power_status_t *st);

/**
 * @brief 是否正在充电 (XIO_BAT_CHRG 为低)
 */
bool power_board_is_charging(void);

/**
 * @brief 取【缓存】的电量信息 (不触发阻塞采样)
 *
 * 数据由 power_board_poll() 周期更新。第一次调用前返回 valid=false。
 */
power_status_t power_board_get_status(void);

/**
 * @brief 周期刷新缓存 (每 ~10s 调一次即可)
 *
 * 内部会做 500ms 阻塞采样, 所以不要调得太频繁。
 * 同时检测供电来源 / 充电状态变化, 并通过回调通知上层。
 *
 * @param on_change 可选。签名: void (*)(const power_status_t *now,
 *                                    const power_status_t *old)
 *                  在检测到供电来源或充电状态发生变化时调用一次。
 */
typedef void (*power_change_cb_t)(const power_status_t *now,
                                  const power_status_t *old);
void power_board_poll(power_change_cb_t on_change);

/**
 * @brief 启动后台电源监测任务
 *
 * 每 10 秒刷新一次缓存, 并在供电来源/充电状态变化时通过回调上报。
 * 任务栈 4096 字节, 优先级 3。
 */
esp_err_t power_board_start_monitor(void);

/**
 * @brief 电池电路诊断 (排障用)
 *
 * 打印 AW9523B 的 P0 原始寄存器, 并扫描各种
 * (XIO_BAT_CHRG_EN 方向/电平) x (XIO_ESP_ADC_SEL 电平) 组合,
 * 每种组合读一次 ADC, 用来判断:
 *   - 电池到底在不在
 *   - 采样使能脚的极性对不对
 *   - ADC_SEL 是不是需要拉低才能切到电池通道
 *
 * 诊断结束后会恢复到默认状态 (XIO2 输出高 / XIO4 输出高)。
 */
void power_board_diagnose(void);

/**
 * @brief 注册状态变化回调 (供后台监测任务使用)
 *
 * main.c 用它接上 push_client_submit_event(), 这样"拔电/插电"
 * 能自动变成一条推给子女端的事件。
 *
 * @param cb 回调, 可传 NULL 取消
 */
void power_board_set_change_cb(power_change_cb_t cb);

/**
 * @brief 把状态转成一行人类可读的文本 (串口打印用)
 */
void power_board_format_status(const power_status_t *st, char *buf, int len);

#ifdef __cplusplus
}
#endif

#endif /* POWER_BOARD_H */
