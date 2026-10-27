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

/* ---- 板载 ST7789 LCD (SPI2) ----
 * 引脚来自正点原子官方 board config.h
 *   SCLK=15  MOSI=16  MISO=17  DC=48  CS=47
 * 320x240, RGB565, SPI mode 0, 60MHz
 */
#define LCD_SCLK_PIN      15
#define LCD_MOSI_PIN      16
#define LCD_MISO_PIN      17
#define LCD_DC_PIN        48
#define LCD_CS_PIN        47
#define LCD_WIDTH         320
#define LCD_HEIGHT        240

/* ---- 摄像头 SCCB 地址 (7 位) ---- */
#define CAM_SCCB_ADDR       0x21    /* GC0308 */

/*
 * AW9523B XIO 编号 —— 与正点原子官方 atk_dnesp32s3_box3 的 config.h 严格对齐。
 *
 * 映射关系 (XIO0..7 -> P0_0..P0_7, XIO8..15 -> P1_0..P1_7):
 *   XIO8  = P1_0   LCD 背光
 *   XIO9  = P1_1   红色 LED
 *   XIO10 = P1_2   蓝色 LED
 *   XIO11 = P1_3   板载 3.3V 总电源使能
 *   XIO12 = P1_4   音频 VBAT 使能
 *   XIO13 = P1_5   音频 3.3V 使能
 *   XIO14 = P1_6   摄像头 2.8V 使能
 *   XIO15 = P1_7   摄像头复位
 *
 * 极性提醒 (照抄官方 InitializeIoExpander, 千万别凭直觉改):
 *   - LCD 背光 XIO8: 低电平点亮 (官方写的是 set_level(XIO_LCD_BL, 0))
 *   - 电源使能类:    高电平有效
 *   - 摄像头复位:    低=复位中, 高=正常工作
 *   - K1/K2:         低电平按下
 */
#define XIO_KEY_K1           0   /* 板载按键 K1 (扩展器上, 低有效) */
#define XIO_KEY_K2           1   /* 板载按键 K2 (扩展器上, 低有效) */
#define XIO_ESP_ADC_SEL      4   /* ADC 选择 */
#define XIO_PA_CTRL          5   /* 音频功放使能 */
#define XIO_LCD_BL           8   /* LCD 背光 —— 低电平点亮 (与官方一致, 勿改成高) */
#define XIO_LED_RED          9   /* 红色 LED */
#define XIO_LED_BLUE        10   /* 蓝色 LED */
#define XIO_VDD_3V3_EN      11   /* 板载 3.3V 总电源使能 (不开摄像头就没电!) */
#define XIO_VBAT_EN         12   /* 音频 VBAT 使能 */
#define XIO_VDDA_3V3_EN     13   /* 音频 3.3V 电源使能 */
#define XIO_CAM_2V8_EN      14   /* 摄像头 2.8V 供电使能 */
#define XIO_CAM_RESET       15   /* 摄像头复位 */

/* ---- 板载按键 ---- */
#define BTN_BOOT_GPIO       0    /* BOOT 键 (GPIO0, 板载直连) */

/* ---- Wi-Fi 配置 ----
 *
 * 两种模式 (改 WIFI_MODE_AP 那一行切换):
 *   STA 模式: 板子连办公室 Wi-Fi, 电脑和板子在同一个局域网,
 *              浏览器访问 http://<板子IP>:8080/
 *   AP  模式: 板子自己开热点, 电脑连板子的热点,
 *              浏览器访问 http://192.168.4.1:8080/  (固定 IP, 不用找)
 *
 * 当前默认 STA 模式 —— 电脑不用切网络, 直接访问板子 IP 就能看画面。
 * (产品最终也是这个架构: 设备主动连服务器, 电脑/小程序只是客户端)
 */
#define WIFI_MODE_AP        0     /* 1=板子开热点, 0=板子连路由器 */

#define WIFI_SSID           "WMU-EDU"
#define WIFI_PASSWORD       "Woaini11"

/* ---- 按键触发时网页弹出的提示语：改这一行就能换文案 ---- */
#define BUTTON_MESSAGE      "老人按了求助按钮"

#endif /* CAMERA_PINS_H */
