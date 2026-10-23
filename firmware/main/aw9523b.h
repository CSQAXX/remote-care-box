/*
 * AW9523B 16 位 IO 扩展器驱动 (正点原子 ATK_DNESP32S3_BOX3)
 *
 * 为什么自己写而不用 esp_io_expander_tca95xx_16bit:
 *   那个组件驱动的是 TCA9555/TCA9539, 寄存器是 16 位合并的 (0x00 输入/0x02 输出/0x06 方向)。
 *   而板子上实际焊的是 Awinic AW9523B, 寄存器按端口分成 8 位两页:
 *     0x00 INPUT_P0  0x01 INPUT_P1
 *     0x02 OUTPUT_P0 0x03 OUTPUT_P1
 *     0x04 CONFIG_P0 0x05 CONFIG_P1   (1=输入, 0=输出)
 *     0x10 ID = 0x23
 *     0x7F 软复位
 *   两者不兼容, 所以自己实现。
 *
 * 引脚编号: XIO0..XIO7 -> P0_0..P0_7, XIO8..XIO15 -> P1_0..P1_7
 *
 * I2C 说明:
 *   本驱动刻意使用 ESP-IDF 旧版 I2C API (driver/i2c.h)。
 *   原因: esp32-camera 的 SCCB 在 IDF < 5.4 时也用旧版 API, 新旧两套驱动
 *   同时链接会在启动时 abort() ("CONFLICT! driver_ng is not allowed to be used
 *   with this old driver")。统一成旧版最省事。
 */

#ifndef AW9523B_H
#define AW9523B_H

#include <stdbool.h>
#include <stdint.h>

#include "driver/i2c.h"
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define AW9523B_I2C_ADDR        0x59

#define AW9523B_REG_INPUT_P0    0x00
#define AW9523B_REG_INPUT_P1    0x01
#define AW9523B_REG_OUTPUT_P0   0x02
#define AW9523B_REG_OUTPUT_P1   0x03
#define AW9523B_REG_CONFIG_P0   0x04
#define AW9523B_REG_CONFIG_P1   0x05
#define AW9523B_REG_ID          0x10
#define AW9523B_REG_SW_RSTN     0x7F

#define AW9523B_CHIP_ID         0x23

#define AW9523B_TIMEOUT_MS      100

/**
 * @brief 初始化 AW9523B
 * @param port I2C 端口号 (旧版 API 的 i2c_port_t)
 * @param addr 7 位从机地址
 * @return ESP_OK 成功; ESP_ERR_NOT_FOUND 表示芯片无应答或 ID 不匹配
 */
esp_err_t aw9523b_init(i2c_port_t port, uint8_t addr);

/**
 * @brief 设置引脚方向
 * @param is_output true=输出, false=输入
 */
esp_err_t aw9523b_set_output(i2c_port_t port, uint8_t addr, uint8_t pin, bool is_output);

/**
 * @brief 设置输出电平
 */
esp_err_t aw9523b_set_level(i2c_port_t port, uint8_t addr, uint8_t pin, bool high);

/**
 * @brief 读取引脚输入电平
 */
esp_err_t aw9523b_get_level(i2c_port_t port, uint8_t addr, uint8_t pin, bool *high);

#ifdef __cplusplus
}
#endif

#endif /* AW9523B_H */
