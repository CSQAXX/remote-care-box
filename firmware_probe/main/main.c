/*
 * 板型探测固件 —— 扫描多条候选 I2C 总线，找出实际挂载的芯片
 *
 * 目的: 区分两种可能的板型
 *   A) 正点原子 DNESP32S3 + BOX3 底板  -> GC0308 摄像头 (0x21), I2C SDA=3/SCL=2
 *   B) 正点原子 DNESP32S3 摄像头扩展口 -> OV2640 摄像头 (0x30), I2C SDA=39/SCL=38
 *
 * 同时扫描:
 *   0x18/0x19 ES8311 音频codec
 *   0x40     ES7210 麦克风ADC
 *   0x59     AW9523B IO扩展器 (BOX3 用来复位摄像头)
 */

#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_err.h"
#include "driver/i2c_master.h"
#include "driver/gpio.h"
#include "esp_psram.h"

static const char *TAG = "probe";

typedef struct {
    const char *name;
    gpio_num_t sda;
    gpio_num_t scl;
} bus_t;

static const bus_t buses[] = {
    { "BOX3 共享I2C  SDA=3  SCL=2",   GPIO_NUM_3,  GPIO_NUM_2  },
    { "DNESP32S3 DCMI SDA=39 SCL=38", GPIO_NUM_39, GPIO_NUM_38 },
    { "常见摄像头     SDA=4  SCL=5",  GPIO_NUM_4,  GPIO_NUM_5  },
    { "BOX I2C        SDA=8  SCL=18", GPIO_NUM_8,  GPIO_NUM_18 },
};

/* 关心的地址 */
typedef struct {
    uint8_t addr;
    const char *name;
} dev_t;

static const dev_t devs[] = {
    { 0x21, "GC0308 摄像头" },
    { 0x30, "OV2640 摄像头" },
    { 0x31, "OV5640 摄像头" },
    { 0x18, "ES8311 音频codec" },
    { 0x19, "ES8311 音频codec" },
    { 0x40, "ES7210 麦克风ADC" },
    { 0x59, "AW9523B IO扩展器" },
    { 0x5A, "TCA95xx IO扩展器" },
};

static esp_err_t probe_bus(const bus_t *bus)
{
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = bus->sda,
        .scl_io_num = bus->scl,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };

    i2c_master_bus_handle_t bus_handle = NULL;
    esp_err_t ret = i2c_new_master_bus(&bus_cfg, &bus_handle);
    if (ret != ESP_OK) {
        return ret;
    }

    printf("\n=== 总线: %s ===\n", bus->name);
    int found = 0;

    for (size_t d = 0; d < sizeof(devs) / sizeof(devs[0]); d++) {
        i2c_device_config_t dev_cfg = {
            .dev_addr_length = I2C_ADDR_BIT_LEN_7,
            .device_address = devs[d].addr,
            .scl_speed_hz = 100000,
        };
        i2c_master_dev_handle_t dev = NULL;
        ret = i2c_master_bus_add_device(bus_handle, &dev_cfg, &dev);
        if (ret != ESP_OK) {
            continue;
        }

        /* 探测：发一个 0x00 命令字节，有的器件会 ACK */
        uint8_t cmd = 0x00;
        ret = i2c_master_transmit(dev, &cmd, 1, 100);
        if (ret == ESP_OK) {
            printf("  [发现] 0x%02X  %s\n", devs[d].addr, devs[d].name);
            found++;
        }
        i2c_master_bus_rm_device(dev);
    }

    if (found == 0) {
        printf("  (无应答)\n");
    }

    i2c_del_master_bus(bus_handle);
    return ESP_OK;
}

void app_main(void)
{
    printf("\n========================================\n");
    printf("  板型探测 - I2C 扫描\n");
    printf("  PSRAM: %u bytes\n", (unsigned int)esp_psram_get_size());
    printf("========================================\n");

    for (size_t i = 0; i < sizeof(buses) / sizeof(buses[0]); i++) {
        probe_bus(&buses[i]);
        vTaskDelay(pdMS_TO_TICKS(200));
    }

    printf("\n========================================\n");
    printf("探测结束\n");
    printf("========================================\n");
}
