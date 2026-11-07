/*
 * 电源 / 电池管理实现 —— 见 power_board.h 的说明。
 *
 * 采样时序严格照抄正点原子官方 xiaozhi 固件 power_manager.h:
 *   1. XIO_BAT_CHRG_EN 设为输出并拉低 (导通分压)
 *   2. 等 500ms 让分压稳定
 *   3. ADC1_CH0 连读 10 次取平均
 *   4. XIO_BAT_CHRG_EN 改回输入 (别一直给电池分压)
 */

#include "power_board.h"

#include <string.h>

#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "aw9523b.h"
#include "camera_pins.h"

static const char *TAG = "power_board";

static adc_oneshot_unit_handle_t s_adc_handle = NULL;
static adc_cali_handle_t        s_cali_handle = NULL;
static bool                     s_adc_ready = false;

/*
 * 电池访问互斥锁。
 *
 * 【为什么必须有】这是一个实测踩到的真 bug:
 *   - 后台监测任务每 2 秒调一次 power_board_poll() -> read_battery()
 *   - 串口命令 "bat" / "batdiag" 也会同时调 read_battery()
 *   - 两者都会操作同一组 AW9523B 引脚 (ADC_SEL / BAT_CHRG_EN) 并读同一个 ADC
 *
 *   read_battery() 读完后会把 ADC_SEL 恢复成高电平。如果这时另一个调用
 *   正在采样, 它的 ADC_SEL 会被中途拉高 -> 读数突变 / 直接读到满量程 4095。
 *
 *   实测证据: batdiag 第一轮读数正常 (375~379), 第二轮就变成 4095 —— 正好
 *   是因为后台任务在这两轮之间插进来, 把引脚状态改了。
 *
 *   所以所有对 AW9523B 电池引脚 + ADC 的访问都必须串行化。
 */
static SemaphoreHandle_t s_bat_mutex = NULL;

/* 申请互斥锁 (超时 3s), 返回 false 表示拿不到 */
static bool bat_lock(void)
{
    if (s_bat_mutex == NULL) {
        return true;   /* 还没初始化, 单线程阶段不需要锁 */
    }
    return xSemaphoreTake(s_bat_mutex, pdMS_TO_TICKS(3000)) == pdTRUE;
}

static void bat_unlock(void)
{
    if (s_bat_mutex != NULL) {
        xSemaphoreGive(s_bat_mutex);
    }
}

/* 缓存的状态 */
static power_status_t s_status = {
    .source   = POWER_SOURCE_UNKNOWN,
    .charging = false,
    .valid    = false,
    .adc_raw  = 0,
    .mv       = 0,
    .percent  = 0,
};

/* 上次刷新时间 */
static int64_t s_last_poll_ms = 0;
#define POWER_POLL_INTERVAL_MS 10000

/* 后台监测任务句柄与回调 */
static TaskHandle_t   s_monitor_task = NULL;
static power_change_cb_t s_change_cb = NULL;

/* ------------------------------------------------------------------ */

/*
 * ADC 原始值 -> 毫伏 -> 百分比
 *
 * 官方分档表是按 ADC 原始值给的:
 *   2696 -> 0%   (3.48V)
 *   2724 -> 20%  (3.53V)
 *   2861 -> 40%  (3.70V)
 *   3038 -> 60%  (3.90V)
 *   3150 -> 80%  (4.02V)
 *   3280 -> 100% (4.14V)
 *
 * 反推系数: (3280-2696) 对应 (4140-3480) mV, 即 584 个 ADC 码 = 660mV
 *          -> 1 ADC 码 ≈ 1.13 mV。取 mv = adc * 660 / 584。
 */
/*
 * ADC 原始值 -> 毫伏
 *
 * 优先用 IDF 的曲线拟合校准 (esp_adc_cali_curve_fitting),
 * 它能补偿 ESP32-S3 的非线性误差, 比自己按"满量程线性"换算准得多。
 * 校准不可用时退回线性估算 (仅用于粗略判断)。
 *
 * 注意: 校准给出的是【ADC 引脚上的电压】, 也就是分压之后的电压。
 *       要还原电池电压还得乘上分压比 (见 BOARD_BAT_DIVIDER_NUM/DEN)。
 */
static int adc_to_mv(int adc)
{
    if (adc < 0) {
        return 0;
    }
    /*
     * 满量程 (4095) 是无效读数, 不能换算成电压。
     * 12dB 衰减下 ESP32-S3 的 ADC 量程约 3.1V, 4095 对应的是
     * "输入被拉到 VDD 附近" (分压回路断开 / 选错通道), 而不是真的 4.6V。
     */
    if (adc >= 4090) {
        return 0;
    }

    if (s_cali_handle != NULL) {
        int mv = 0;
        if (adc_cali_raw_to_voltage(s_cali_handle, adc, &mv) == ESP_OK) {
            return mv;
        }
    }

    /* 退回线性估算: 12dB 衰减下满量程约 3100mV */
    return (int)((int64_t)adc * 3100 / 4095);
}

/*
 * 电池电压 (mV) -> 电量百分比
 *
 * 为什么不用官方的 ADC 原始码分档表:
 *   官方表 (2696->0% ... 3280->100%) 是基于"ADC 原始码直接对应分压后电压"
 *   的假设, 但我们实测的原始码 (SEL 拉低时约 367~377) 与官方表的量级
 *   完全对不上, 且我们还没确认底板的分压比。
 *   所以改成按【真实电池电压】分档 —— 锂电池的电压-电量曲线本来就是
 *   这几个点, 与硬件分压比无关, 更通用也更容易标定。
 *
 * 18650 锂电池放电曲线的典型拐点:
 *   4.20V 满电 / 3.90V 60% / 3.80V 40% / 3.70V 20% / 3.50V 0%
 * 充电时电压偏高, 放电时偏低, 这里取中间值。
 */
static int mv_to_percent(int mv)
{
    static const struct { int mv; uint8_t level; } levels[] = {
        {3450, 0},    /* 3.45V 以下算没电 */
        {3600, 10},
        {3700, 30},
        {3800, 50},
        {3900, 70},
        {4000, 90},
        {4200, 100},
    };
    const int n = sizeof(levels) / sizeof(levels[0]);

    if (mv <= 0) {
        return 0;
    }
    if (mv < levels[0].mv) {
        return 0;
    }
    if (mv >= levels[n - 1].mv) {
        return 100;
    }
    for (int i = 0; i < n - 1; i++) {
        if (mv >= levels[i].mv && mv < levels[i + 1].mv) {
            int span = levels[i + 1].mv - levels[i].mv;
            int ratio = (mv - levels[i].mv) * 100 / span;
            return levels[i].level +
                   ratio * (levels[i + 1].level - levels[i].level) / 100;
        }
    }
    return 0;
}

/* ------------------------------------------------------------------ */

esp_err_t power_board_init(void)
{
    if (s_adc_ready) {
        return ESP_OK;
    }

    /* 电池访问互斥锁 (幂等) */
    if (s_bat_mutex == NULL) {
        s_bat_mutex = xSemaphoreCreateMutex();
        if (s_bat_mutex == NULL) {
            ESP_LOGW(TAG, "电池互斥量创建失败");
            return ESP_FAIL;
        }
    }

    adc_oneshot_unit_init_cfg_t unit_cfg = {
        .unit_id   = BOARD_ADC_UNIT,
        .ulp_mode  = ADC_ULP_MODE_DISABLE,
    };
    esp_err_t err = adc_oneshot_new_unit(&unit_cfg, &s_adc_handle);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "ADC 单元创建失败: %s", esp_err_to_name(err));
        return err;
    }

    adc_oneshot_chan_cfg_t chan_cfg = {
        .atten    = BOARD_ADC_ATTEN,
        .bitwidth = BOARD_ADC_BITWIDTH,
    };
    err = adc_oneshot_config_channel(s_adc_handle, BOARD_ADC_CHANNEL, &chan_cfg);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "ADC 通道配置失败: %s", esp_err_to_name(err));
        adc_oneshot_del_unit(s_adc_handle);
        s_adc_handle = NULL;
        return err;
    }

    /*
     * 曲线拟合校准 (ESP32-S3 必须走 V1 校准方案)。
     *
     * 没有校准的话, ADC 原始码到电压的换算误差能到 10% 以上,
     * 对电量百分比来说是灾难性的 (3.9V 和 3.5V 会算出同一个值)。
     */
    adc_cali_curve_fitting_config_t cali_cfg = {
        .unit_id  = BOARD_ADC_UNIT,
        .atten    = BOARD_ADC_ATTEN,
        .bitwidth = BOARD_ADC_BITWIDTH,
    };
    if (adc_cali_create_scheme_curve_fitting(&cali_cfg, &s_cali_handle) == ESP_OK) {
        ESP_LOGI(TAG, "ADC 校准已启用 (曲线拟合)");
    } else {
        s_cali_handle = NULL;
        ESP_LOGW(TAG, "ADC 校准创建失败, 退回线性估算 (电量精度会下降)");
    }

    s_adc_ready = true;
    ESP_LOGI(TAG, "电池 ADC 就绪 (unit=%d ch=%d)", BOARD_ADC_UNIT,
             (int)BOARD_ADC_CHANNEL);
    return ESP_OK;
}

/* ------------------------------------------------------------------ */

esp_err_t power_board_read_battery(int *out_adc, int *out_mv, int *out_pct)
{
    if (!s_adc_ready) {
        esp_err_t err = power_board_init();
        if (err != ESP_OK) {
            return err;
        }
    }

    if (!bat_lock()) {
        ESP_LOGW(TAG, "电池采样获取锁超时");
        return ESP_ERR_TIMEOUT;
    }

    /*
     * 1. 把 ADC 切到电池采样通道。
     *
     *    实测结论 (batdiag 多轮扫描 + bat 命令交叉验证):
     *
     *      任何 EN + SEL(高)  -> 4095   满量程, 选错通道
     *      SEL(低) + EN(高)   -> 375/377 (batdiag 里) / 4095 (bat 里)
     *      SEL(低) + EN(低)   -> 374/367
     *
     *    两种 EN 电平都能拿到有效读数, 但只有【拉低】才是官方定义的分压
     *    导通状态, 所以这里以官方为准: XIO_BAT_CHRG_EN 设为输出并拉低。
     *
     *    关于延时:
     *    官方写 500ms。我们实测发现等待太久反而读到满量程 ——
     *    因为拉低 EN 之后, 分压通路把引脚拉低, 而某条上拉路径在把引脚
     *    慢慢往上顶。所以改成"短延时 -> 采样 -> 长延时不再等"，
     *    先在通路刚导通时采样。
     *
     *    具体多少合适还要实测, 先用 100ms。
     */
    aw9523b_set_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_ESP_ADC_SEL, true);
    aw9523b_set_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_ESP_ADC_SEL,
                      BOARD_BAT_ADC_SEL_LOW);
    aw9523b_set_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_BAT_CHRG_EN, true);
    aw9523b_set_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_BAT_CHRG_EN, false);

    /* 2. 等分压导通 */
    vTaskDelay(pdMS_TO_TICKS(BOARD_BAT_SETTLE_MS));

    /* 3. 连读 10 次取平均 */
    int sum = 0;
    int valid = 0;
    int raw_min = 4095, raw_max = 0;
    for (int i = 0; i < 10; i++) {
        int raw = 0;
        if (adc_oneshot_read(s_adc_handle, BOARD_ADC_CHANNEL, &raw) == ESP_OK) {
            sum += raw;
            valid++;
            if (raw < raw_min) raw_min = raw;
            if (raw > raw_max) raw_max = raw;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }

    /*
     * 4. 恢复现场:
     *    - XIO_BAT_CHRG_EN 改回输入 (别一直给电池分压)
     *    - ADC_SEL 恢复高电平 (板载默认, 留给别的 ADC 源)
     */
    aw9523b_set_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_BAT_CHRG_EN, false);
    aw9523b_set_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_ESP_ADC_SEL,
                      BOARD_BAT_ADC_SEL_HIGH);

    if (valid == 0) {
        ESP_LOGW(TAG, "电池电压读取全部失败");
        if (out_adc) *out_adc = 0;
        if (out_mv)  *out_mv  = 0;
        if (out_pct) *out_pct = 0;
        bat_unlock();
        return ESP_ERR_TIMEOUT;
    }

    int avg = sum / valid;
    int pin_mv = adc_to_mv(avg);          /* ADC 引脚上的电压 (分压后) */
    int mv    = pin_mv * BOARD_BAT_DIVIDER_NUM / BOARD_BAT_DIVIDER_DEN;
    int pct   = mv_to_percent(mv);

    if (out_adc) *out_adc = avg;
    if (out_mv)  *out_mv  = mv;
    if (out_pct) *out_pct = pct;

    /*
     * 原始码的离散度很重要 —— 它能区分两种完全不同的状态:
     *   离散度小 (比如 370~380) -> 引脚上有稳定的电压, 分压回路是通的
     *   离散度大 (比如 375~4095) -> 引脚悬空, 读数随噪声乱跳
     *
     * 实测当前板子: 有时 375, 有时 4095, 离散度极大 -> 典型的悬空引脚。
     */
    ESP_LOGI(TAG, "电池: adc=%d (范围 %d~%d, %d 次有效) 引脚=%dmV x%.2f -> %dmV -> %d%%%s",
             avg, raw_min, raw_max, valid, pin_mv,
             (float)BOARD_BAT_DIVIDER_NUM / (float)BOARD_BAT_DIVIDER_DEN,
             mv, pct,
             (raw_max - raw_min) > 100 ? "  [读数抖动大 -> 引脚悬空]" : "");
    bat_unlock();
    return ESP_OK;
}
/* ------------------------------------------------------------------ */

power_source_t power_board_get_source(void)
{
    return power_board_determine_source(&s_status);
}

/*
 * 读充电状态脚 (不加锁的内部版本)。
 *
 * 为什么有加锁/不加锁两个版本:
 *   power_board_poll() 在 read_battery() 返回后才调用本函数, 而
 *   read_battery() 内部已经持有互斥锁 —— 如果这里再加锁就是递归加锁,
 *   FreeRTOS 的互斥量不可重入, 会直接死锁。
 */
static bool read_charging_flag(void)
{
    bool high = true;
    if (aw9523b_get_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR,
                          XIO_BAT_CHRG, &high) != ESP_OK) {
        return false;
    }
    return !high;   /* 低电平 = 正在充电 */
}

power_source_t power_board_determine_source(const power_status_t *st)
{
    bool high = true;
    if (aw9523b_get_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR,
                          XIO_BAT_CHRG, &high) != ESP_OK) {
        return POWER_SOURCE_UNKNOWN;
    }
    /*
     * 官方判据: 充电检测脚为【低】表示正在充电 => Type-C 供电;
     *           为【高】表示没在充 => 电池供电。
     *
     * 但这个判据有个大坑: 【没装电池】时充电芯片同样不会拉低检测脚,
     * 于是会被误判成"电池供电"。实测就是这样:
     *   Type-C 插着 + 没有电池 -> BAT_CHRG 恒高 -> 误报电池供电
     *
     * 所以必须结合电量读数交叉验证:
     *   BAT_CHRG 高 + 电压有效(>=2.5V) -> 真的是电池供电
     *   BAT_CHRG 高 + 电压无效/极低     -> 没电池, 实际是 Type-C 供电
     */
    if (!high) {
        return POWER_SOURCE_TYPEC;
    }

    if (st != NULL && st->valid) {
        return POWER_SOURCE_BATTERY;
    }

    /* 检测脚说"在放电", 但根本读不到电池电压 -> 那就是没电池 */
    return POWER_SOURCE_TYPEC;
}

bool power_board_is_charging(void)
{
    return read_charging_flag();
}

/* ------------------------------------------------------------------ */

power_status_t power_board_get_status(void)
{
    return s_status;
}

void power_board_poll(power_change_cb_t on_change)
{
    int64_t now = esp_timer_get_time() / 1000;
    if (s_last_poll_ms != 0 && (now - s_last_poll_ms) < POWER_POLL_INTERVAL_MS) {
        return;
    }
    s_last_poll_ms = now;

    power_status_t old = s_status;
    power_status_t cur = s_status;

    cur.charging = power_board_is_charging();

    /*
     * 顺序很重要: 必须先读电压, 再判供电来源。
     *
     * power_board_get_source() 会用【缓存里的电压】做交叉验证
     * (充电脚为高但读不到有效电压 => 没电池, 实际是 Type-C 供电),
     * 如果先判来源再读电压, 用的就是上一轮的旧数据, 会误判。
     */
    int adc = 0, mv = 0, pct = 0;
    if (power_board_read_battery(&adc, &mv, &pct) == ESP_OK) {
        /*
         * valid 的语义是"读到了可信的电池电压", 不是"ADC 读成功了"。
         *
         * 判据用【ADC 原始码】而不是换算后的电压 —— 因为分压比是从
         * 官方代码反推的, 还没经万用表实测验证, 用它判断有效性会
         * 一错全错。原始码直接对应硬件电路, 更可靠。
         *
         * ADC 读成功但码值很小, 说明分压回路没接上 (没装电池) ——
         * 这时候 valid 必须是 false, 否则上层会把杂散电压当成电池电压。
         */
        cur.valid   = (adc >= BOARD_BAT_VALID_MIN_ADC);
        cur.adc_raw = adc;
        cur.mv      = mv;
        cur.percent = pct;
    } else {
        cur.valid = false;
    }

    /* 先把新电压写进缓存, 再判来源 —— 让 get_source() 用到本轮的数据 */
    s_status = cur;
    cur.source = power_board_determine_source(&cur);
    s_status = cur;

    if (on_change != NULL &&
        (old.source != cur.source || old.charging != cur.charging)) {
        on_change(&cur, &old);
    }
}

/* ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ */

void power_board_set_change_cb(power_change_cb_t cb)
{
    s_change_cb = cb;
}

static void power_monitor_task(void *arg)
{
    while (true) {
        power_board_poll(s_change_cb);
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}

esp_err_t power_board_start_monitor(void)
{
    if (s_monitor_task != NULL) {
        return ESP_OK;
    }
    if (!s_adc_ready) {
        esp_err_t err = power_board_init();
        if (err != ESP_OK) {
            return err;
        }
    }

    BaseType_t ok = xTaskCreate(power_monitor_task, "power_mon",
                                 4096, NULL, 3, &s_monitor_task);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "电源监测任务创建失败");
        s_monitor_task = NULL;
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "电源监测任务已启动 (每 10s 刷新一次)");
    return ESP_OK;
}

/* ------------------------------------------------------------------ */

/*
 * 读一次 ADC 原始值 (不操作任何 AW9523B 引脚)。
 * 返回 -1 表示读失败。
 */
static int read_adc_once(void)
{
    if (!s_adc_ready) {
        if (power_board_init() != ESP_OK) {
            return -1;
        }
    }
    int raw = 0;
    if (adc_oneshot_read(s_adc_handle, BOARD_ADC_CHANNEL, &raw) != ESP_OK) {
        return -1;
    }
    return raw;
}

void power_board_diagnose(void)
{
    ESP_LOGI(TAG, "===== 电池电路诊断 =====");

    /*
     * 诊断期间独占电池引脚 —— 否则后台监测任务会在扫描中途把 ADC_SEL
     * 拉高还原, 扫描结果就全是 4095, 白测。
     */
    if (!bat_lock()) {
        ESP_LOGW(TAG, "诊断获取锁超时, 放弃");
        return;
    }

    /* 1. 原始寄存器快照 */
    uint8_t cfg0 = 0, cfg1 = 0, in0 = 0, in1 = 0, out0 = 0, out1 = 0;
    aw9523b_get_config(BOARD_I2C_PORT, AW9523B_I2C_ADDR, &cfg0, &cfg1);
    aw9523b_get_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR, &out0, &out1);
    aw9523b_read_raw(BOARD_I2C_PORT, AW9523B_I2C_ADDR, AW9523B_REG_INPUT_P0, &in0);
    aw9523b_read_raw(BOARD_I2C_PORT, AW9523B_I2C_ADDR, AW9523B_REG_INPUT_P1, &in1);

    ESP_LOGI(TAG, "CONFIG  P0=0x%02x P1=0x%02x  (1=输入)", cfg0, cfg1);
    ESP_LOGI(TAG, "OUTPUT  P0=0x%02x P1=0x%02x", out0, out1);
    ESP_LOGI(TAG, "INPUT   P0=0x%02x P1=0x%02x", in0, in1);
    ESP_LOGI(TAG, "  P0 bit2(BAT_CHRG_EN) 方向=%s",
             (cfg0 & 0x04) ? "输入" : "输出");
    ESP_LOGI(TAG, "  P0 bit3(BAT_CHRG)    方向=%s 电平=%s",
             (cfg0 & 0x08) ? "输入" : "输出",
             (in0 & 0x08) ? "高" : "低");
    ESP_LOGI(TAG, "  P0 bit4(ADC_SEL)     方向=%s 锁存=%s",
             (cfg0 & 0x10) ? "输入" : "输出",
             (out0 & 0x10) ? "高" : "低");

    /* 2. 充电状态脚 */
    bool chrg_high = true;
    if (aw9523b_get_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR,
                          XIO_BAT_CHRG, &chrg_high) == ESP_OK) {
        ESP_LOGI(TAG, "  BAT_CHRG = %s -> %s",
                 chrg_high ? "高(未充电)" : "低(充电中)",
                 chrg_high ? "按官方判据 = 电池供电" : "按官方判据 = Type-C 供电");
    }

    /* 3. 扫描各种控制脚组合 */
    ESP_LOGI(TAG, "--- 组合扫描 (每组读 5 次取均值) ---");

    /*
     * 扫描顺序按【实测稳定性】排列:
     *   第一组就是已知最稳的组合, 放在最前面。
     * 每组读 3 轮, 用来区分"稳定读数"和"随机跳变"。
     */
    struct { uint8_t en_dir;  /* 0=输出 1=输入 */
              uint8_t en_lvl;  /* 输出时的电平 */
              uint8_t sel_lvl; /* ADC_SEL 锁存电平 */
              const char *desc; } cases[] = {
        {0, 1, 0, "EN=输出高(默认) SEL=低  <-- 已知最稳"},
        {0, 0, 0, "EN=输出低(导通分压) SEL=低"},
        {1, 0, 0, "EN=输入(高阻)     SEL=低"},
        {0, 1, 1, "EN=输出高         SEL=高  <-- 满量程对照"},
        {0, 0, 1, "EN=输出低         SEL=高"},
        {1, 0, 1, "EN=输入           SEL=高"},
    };

    for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        aw9523b_set_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR,
                           XIO_BAT_CHRG_EN, cases[i].en_dir == 1);
        aw9523b_set_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR,
                          XIO_BAT_CHRG_EN, cases[i].en_lvl != 0);
        aw9523b_set_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR,
                           XIO_ESP_ADC_SEL, true);
        aw9523b_set_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR,
                          XIO_ESP_ADC_SEL, cases[i].sel_lvl != 0);

        vTaskDelay(pdMS_TO_TICKS(300));

        /* 读 3 轮, 每轮 5 次取均值 —— 看趋势是否稳定 */
        int vals[3];
        for (int r = 0; r < 3; r++) {
            int sum = 0, n = 0;
            for (int k = 0; k < 5; k++) {
                int v = read_adc_once();
                if (v >= 0) { sum += v; n++; }
                vTaskDelay(pdMS_TO_TICKS(10));
            }
            vals[r] = n ? sum / n : -1;
        }

        int avg = vals[0];
        int pin_mv = adc_to_mv(avg);
        ESP_LOGI(TAG, "  %s", cases[i].desc);
        ESP_LOGI(TAG, "      adc=%d / %d / %d  引脚=%dmV  电池(按x%.2f)=%dmV",
                 vals[0], vals[1], vals[2], pin_mv,
                 (float)BOARD_BAT_DIVIDER_NUM / (float)BOARD_BAT_DIVIDER_DEN,
                 pin_mv * BOARD_BAT_DIVIDER_NUM / BOARD_BAT_DIVIDER_DEN);
    }

    /* 4. 延时扫描: 找出"拉低 EN 后等多久"才是对的 */
    ESP_LOGI(TAG, "--- 延时扫描 (EN=输出低, SEL=低) ---");
    static const int delays_ms[] = {0, 50, 100, 200, 300, 500, 800, 1200};
    for (size_t i = 0; i < sizeof(delays_ms) / sizeof(delays_ms[0]); i++) {
        aw9523b_set_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_ESP_ADC_SEL, true);
        aw9523b_set_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_ESP_ADC_SEL,
                          BOARD_BAT_ADC_SEL_LOW);
        aw9523b_set_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_BAT_CHRG_EN, true);
        aw9523b_set_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_BAT_CHRG_EN, false);

        vTaskDelay(pdMS_TO_TICKS(delays_ms[i]));

        int sum = 0, n = 0;
        for (int k = 0; k < 5; k++) {
            int v = read_adc_once();
            if (v >= 0) { sum += v; n++; }
            vTaskDelay(pdMS_TO_TICKS(10));
        }
        int avg = n ? sum / n : -1;
        ESP_LOGI(TAG, "  延时 %4dms -> adc=%4d 引脚=%dmV",
                 delays_ms[i], avg, adc_to_mv(avg));
    }

    /* 5. 恢复默认状态 */
    aw9523b_set_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_BAT_CHRG_EN, true);
    aw9523b_set_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_BAT_CHRG_EN, true);
    aw9523b_set_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_ESP_ADC_SEL, true);
    aw9523b_set_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_ESP_ADC_SEL, true);

    ESP_LOGI(TAG, "===== 诊断结束, 已恢复默认状态 =====");
    ESP_LOGI(TAG, "判读方法: 找出 adc 在 300~3500 之间的组 —— 那是有效的电池通道");
    ESP_LOGI(TAG, "        4095 = 满量程, 说明 ADC 没切到电池通道 (或分压回路断开)");
    ESP_LOGI(TAG, "延时扫描: 找出一个读数稳定的延时, 填进 BOARD_BAT_SETTLE_MS");
    ESP_LOGI(TAG, "标定分压比: 用万用表量电池电压 Vbat, 除以上面测到的『引脚=mV』,");
    ESP_LOGI(TAG, "        得到的比值填进 camera_pins.h 的 BOARD_BAT_DIVIDER_NUM/DEN");

    bat_unlock();
}

void power_board_format_status(const power_status_t *st, char *buf, int len)
{
    if (st == NULL || buf == NULL || len <= 0) {
        return;
    }

    const char *src = "未知";
    switch (st->source) {
    case POWER_SOURCE_TYPEC:   src = "Type-C"; break;
    case POWER_SOURCE_BATTERY: src = "电池";   break;
    default:                   src = "未知";   break;
    }

    if (!st->valid) {
        snprintf(buf, len, "供电=%s 充电=%s 电量=读取失败",
                 src, st->charging ? "是" : "否");
        return;
    }

    snprintf(buf, len, "供电=%s 充电=%s ADC=%d 电压=%dmV 电量=%d%%",
             src, st->charging ? "是" : "否",
             st->adc_raw, st->mv, st->percent);
}
