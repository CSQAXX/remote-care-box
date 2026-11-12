idf_component_register(SRCS "main.c" "aw9523b.c" "lcd_st7789.c" "audio_board.c" "g711.c"
                       "jpeg_yield.cpp"
                       "lcd_prompt.c" "lcd_text.c" "lcd_font.c"
                       "activity_detect.cpp" "time_sync.c"
                       "push_client.c" "reliability.c"
                       "power_board.c"
                       "ai_client.c"
                       "wake_word.c"
                       "voice_mode.c"
                       INCLUDE_DIRS "."
                       REQUIRES esp32-camera driver esp_psram
                                esp_wifi esp_http_server esp_timer nvs_flash
                                esp_lcd esp_mm esp_codec_dev
                                esp_http_client
                                esp_driver_usb_serial_jtag
                                esp_netif
                                # 电池电量检测 (XIO2 导通分压 + ADC1_CH0 采样)
                                esp_adc
                                # 可靠性 §3.9: TCP 推模式 (socket) + 看门狗
                                lwip esp_system
                                # 活动检测 (§3.5): esp-dl + pedestrian_detect
                                pedestrian_detect
                                # esp-dl 的核心库 (dl::detect / dl::image)
                                esp-dl
                                # AI 对话客户端: mbedtls SHA256 (HMAC) + 系统根证书包 (HTTPS 校验)
                                mbedtls esp-tls
                                # 离线唤醒词「晴老师」: MultiNet7 中文自由指令模型 (mn7_cn)
                                esp-sr)

# ---------------------------------------------------------------------------
# 临时降级 (2026-11-08): esp-dl 模型算子不匹配导致开机 panic
#
# 现象: "Do not support GlobalAveragePool" -> "There's no input in model"
#       -> LoadProhibited (EXCVADDR 0x14) -> 无限重启。
# 根因: pedestrian_detect 模型需要的算子没被注册进这版 esp-dl,
#       get_raw_model() 内部加载时就崩, 应用层 nullptr 检查根本来不及跑。
#
# activity_detect.h 本来就设计了 ENABLE_HUMAN_DETECT=0 的降级路径
# (关掉人形识别, 运动检测/活动检测/看护规则照常)。这里用编译期宏覆盖,
# 【源码一行不动】, 等 esp-dl 算子修好后删掉下面这一行即可恢复人形识别。
# ---------------------------------------------------------------------------
target_compile_definitions(${COMPONENT_TARGET} PRIVATE "ENABLE_HUMAN_DETECT=0")

# ---------------------------------------------------------------------------
# PC_VOICE_PATCH v1 (删掉这整段即可还原) —— 「小电脑」语音遥控我自己的电脑
#
# 纯追加, 上面 idf_component_register(...) 一行都没动。
# 想关掉: idf.py -DENABLE_PC_VOICE=0 build  (或把下面 set 的 1 改成 0)
# ---------------------------------------------------------------------------
set(ENABLE_PC_VOICE 1 CACHE BOOL "「小电脑」语音遥控我的电脑")
if(ENABLE_PC_VOICE)
    target_sources(${COMPONENT_TARGET} PRIVATE "pc_voice.c")
    # 编译器把 snprintf 的有意截断当错误处理, 关掉这个文件上该警告的 error 化 (逻辑不动)
    set_source_files_properties(pc_voice.c PROPERTIES COMPILE_FLAGS "-Wno-error=format-truncation")
    target_compile_definitions(${COMPONENT_TARGET} PRIVATE "ENABLE_PC_VOICE=1")
endif()
