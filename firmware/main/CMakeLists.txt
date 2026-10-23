idf_component_register(SRCS "main.c" "aw9523b.c"
                       INCLUDE_DIRS "."
                       REQUIRES esp32-camera driver esp_psram
                                esp_wifi esp_http_server esp_timer nvs_flash)
