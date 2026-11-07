idf_component_register(SRCS "main.c" "aw9523b.c" "lcd_st7789.c" "audio_board.c" "g711.c"
                       "jpeg_yield.cpp"
                       "lcd_prompt.c" "lcd_text.c" "lcd_font.c"
                       "activity_detect.cpp" "time_sync.c"
                       "push_client.c" "reliability.c"
                       "power_board.c"
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
                                esp-dl)
