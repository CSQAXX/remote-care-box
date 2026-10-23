/*
 * 远程看护盒子 - 摄像头引脚定义 (正点原子 ATK_DNESP32S3_BOX3)
 *
 * 权威来源:
 *   - 板级 config.h (xiaozhi-esp32 / boards/alientek/atk-dnesp32s3-box3)
 *   - 板级引脚文档 ATK_DNESP32S3_BOX3_外设与GPIO.md
 *   - 本机 I2C 实测: SDA=3/SCL=2 总线上 0x21(GC0308) 应答
 *
 * 重要: GPIO 22/23/24/25 被 Octal PSRAM 占用，芯片上根本不存在，
 *       千万不要拿来当摄像头引脚（会导致 ll_cam_set_pin 空指针崩溃）。
 *
 * 摄像头传感器: GC0308 (I2C 地址 0x21)
 * 摄像头复位: 通过 AW9523B IO 扩展器 XIO15 (需要先初始化扩展器)
 * 摄像头供电: AW9523B XIO14 (CAM_2V8_EN) 拉高使能 2.8V
 * XCLK: 板上由其他电路提供 (CAM_PIN_XCLK = NC)，因此 SCCB 必须复用已有 I2C
 */

#ifndef CAMERA_PINS_H
#define CAMERA_PINS_H

/* ---- 摄像头 DVP 数据线 (GC0308) ---- */
#define CAM_PIN_D0     7    /* GPIO_NUM_7  */
#define CAM_PIN_D1     8    /* GPIO_NUM_8  */
#define CAM_PIN_D2     9    /* GPIO_NUM_9  */
#define CAM_PIN_D3     10   /* GPIO_NUM_10 */
#define CAM_PIN_D4     11   /* GPIO_NUM_11 */
#define CAM_PIN_D5     12   /* GPIO_NUM_12 */
#define CAM_PIN_D6     4    /* GPIO_NUM_4  */
#define CAM_PIN_D7     5    /* GPIO_NUM_5  */

#define CAM_PIN_VSYNC  6    /* GPIO_NUM_6  */
#define CAM_PIN_HREF   46   /* GPIO_NUM_46 (板级宏名为 LREF/DE) */
#define CAM_PIN_PCLK   45   /* GPIO_NUM_45 */

/* ---- 摄像头控制线 ---- */
#define CAM_PIN_PWDN   -1   /* GPIO_NUM_NC */
#define CAM_PIN_RESET  -1   /* GPIO_NUM_NC，实际复位走 AW9523B XIO15 */
#define CAM_PIN_XCLK   -1   /* GPIO_NUM_NC，板载时钟源 */
#define CAM_PIN_SIOD   -1   /* GPIO_NUM_NC，摄像头复用共享 I2C */
#define CAM_PIN_SIOC   -1   /* GPIO_NUM_NC，摄像头复用共享 I2C */

/* ---- 共享 I2C 总线 (音频 / 扩展器 / 摄像头 SCCB 共用) ---- */
#define BOARD_I2C_PORT      I2C_NUM_0
#define BOARD_I2C_SDA_PIN   3
#define BOARD_I2C_SCL_PIN   2
#define BOARD_I2C_FREQ_HZ   100000

/* ---- AW9523B IO 扩展器 (XIO 编号) ---- */
#define XIO_CAM_2V8_EN      14   /* 摄像头 2.8V 供电使能 */
#define XIO_CAM_RESET       15   /* 摄像头复位 */
#define XIO_KEY_K1           0   /* 板载按键 K1 (扩展器上, 低有效) */
#define XIO_KEY_K2           1   /* 板载按键 K2 (扩展器上, 低有效) */

/* ---- 板载按键 ---- */
#define BTN_BOOT_GPIO       0    /* BOOT 键 (GPIO0, 板载直连) */

/* ---- Wi-Fi 配置：改成办公室 Wi-Fi ---- */
#define WIFI_SSID           "YOUR_WIFI_SSID"
#define WIFI_PASSWORD       "YOUR_WIFI_PASSWORD"

/* ---- 按键触发时网页弹出的提示语：改这一行就能换文案 ---- */
#define BUTTON_MESSAGE      "老人按了求助按钮"

#endif /* CAMERA_PINS_H */
