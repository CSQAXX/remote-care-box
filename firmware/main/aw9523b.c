#include "aw9523b.h"

#include <string.h>

#include "esp_log.h"

static const char *TAG = "aw9523b";

static esp_err_t write_reg(i2c_port_t port, uint8_t addr, uint8_t reg, uint8_t val)
{
    uint8_t buf[2] = {reg, val};
    return i2c_master_write_to_device(port, addr, buf, sizeof(buf),
                                      AW9523B_TIMEOUT_MS / portTICK_PERIOD_MS);
}

static esp_err_t read_reg(i2c_port_t port, uint8_t addr, uint8_t reg, uint8_t *val)
{
    return i2c_master_write_read_device(port, addr, &reg, 1, val, 1,
                                        AW9523B_TIMEOUT_MS / portTICK_PERIOD_MS);
}

/* XIO0..7 -> P0, XIO8..15 -> P1 */
static inline uint8_t pin_reg(uint8_t pin, uint8_t p0_reg, uint8_t p1_reg)
{
    return (pin < 8) ? p0_reg : p1_reg;
}

static inline uint8_t pin_mask(uint8_t pin)
{
    return (uint8_t)(1u << (pin & 0x07));
}

esp_err_t aw9523b_init(i2c_port_t port, uint8_t addr)
{
    uint8_t id = 0;
    esp_err_t err = read_reg(port, addr, AW9523B_REG_ID, &id);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "AW9523B 无应答 (addr=0x%02x): %s", addr, esp_err_to_name(err));
        return ESP_ERR_NOT_FOUND;
    }
    if (id != AW9523B_CHIP_ID) {
        ESP_LOGW(TAG, "AW9523B ID 不符: 读到 0x%02x, 期望 0x%02x", id, AW9523B_CHIP_ID);
        return ESP_ERR_NOT_FOUND;
    }
    ESP_LOGI(TAG, "AW9523B 识别成功 (ID=0x%02x)", id);
    return ESP_OK;
}

esp_err_t aw9523b_set_output(i2c_port_t port, uint8_t addr, uint8_t pin, bool is_output)
{
    if (pin > 15) {
        return ESP_ERR_INVALID_ARG;
    }
    uint8_t reg = pin_reg(pin, AW9523B_REG_CONFIG_P0, AW9523B_REG_CONFIG_P1);
    uint8_t mask = pin_mask(pin);

    uint8_t cfg = 0;
    esp_err_t err = read_reg(port, addr, reg, &cfg);
    if (err != ESP_OK) {
        return err;
    }

    /* CONFIG: 1=输入, 0=输出 */
    if (is_output) {
        cfg &= (uint8_t)~mask;
    } else {
        cfg |= mask;
    }
    return write_reg(port, addr, reg, cfg);
}

esp_err_t aw9523b_set_level(i2c_port_t port, uint8_t addr, uint8_t pin, bool high)
{
    if (pin > 15) {
        return ESP_ERR_INVALID_ARG;
    }
    uint8_t reg = pin_reg(pin, AW9523B_REG_OUTPUT_P0, AW9523B_REG_OUTPUT_P1);
    uint8_t mask = pin_mask(pin);

    uint8_t out = 0;
    esp_err_t err = read_reg(port, addr, reg, &out);
    if (err != ESP_OK) {
        return err;
    }

    if (high) {
        out |= mask;
    } else {
        out &= (uint8_t)~mask;
    }
    return write_reg(port, addr, reg, out);
}

esp_err_t aw9523b_get_level(i2c_port_t port, uint8_t addr, uint8_t pin, bool *high)
{
    if (pin > 15 || high == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    uint8_t reg = pin_reg(pin, AW9523B_REG_INPUT_P0, AW9523B_REG_INPUT_P1);

    uint8_t in = 0;
    esp_err_t err = read_reg(port, addr, reg, &in);
    if (err != ESP_OK) {
        return err;
    }
    *high = (in & pin_mask(pin)) != 0;
    return ESP_OK;
}

esp_err_t aw9523b_get_config(i2c_port_t port, uint8_t addr, uint8_t *p0, uint8_t *p1)
{
    esp_err_t err = read_reg(port, addr, AW9523B_REG_CONFIG_P0, p0);
    if (err != ESP_OK) {
        return err;
    }
    return read_reg(port, addr, AW9523B_REG_CONFIG_P1, p1);
}

esp_err_t aw9523b_set_config_raw(i2c_port_t port, uint8_t addr, uint8_t p0, uint8_t p1)
{
    esp_err_t err = write_reg(port, addr, AW9523B_REG_CONFIG_P0, p0);
    if (err != ESP_OK) {
        return err;
    }
    return write_reg(port, addr, AW9523B_REG_CONFIG_P1, p1);
}

esp_err_t aw9523b_get_output(i2c_port_t port, uint8_t addr, uint8_t *p0, uint8_t *p1)
{
    esp_err_t err = ESP_OK;
    if (p0 != NULL) {
        err = read_reg(port, addr, AW9523B_REG_OUTPUT_P0, p0);
        if (err != ESP_OK) {
            return err;
        }
    }
    if (p1 != NULL) {
        err = read_reg(port, addr, AW9523B_REG_OUTPUT_P1, p1);
    }
    return err;
}

esp_err_t aw9523b_read_raw(i2c_port_t port, uint8_t addr, uint8_t reg, uint8_t *val)
{
    return read_reg(port, addr, reg, val);
}

esp_err_t aw9523b_write_output_p1(i2c_port_t port, uint8_t addr, uint8_t val)
{
    return write_reg(port, addr, AW9523B_REG_OUTPUT_P1, val);
}
