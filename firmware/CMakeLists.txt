cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(kanhu_box)

# ============================================================================
# 模型分区的构建陷阱 (2026-11-08 实测踩坑记录)
# ----------------------------------------------------------------------------
# esp-sr 组件里:
#     add_custom_command(OUTPUT ${image_file}
#                        DEPENDS ${SDKCONFIG}
#                        COMMAND python movemodel.py ...)
#
# 它只 DEPENDS sdkconfig —— 也就是说【改了 commands_cn.txt 也不会重新打包】!
# 结果: build/srmodels/srmodels.bin 还是老的, 刷进去板子照样 panic,
#       而日志里 idf.py build 显示 "Project build complete", 极具欺骗性。
#
# 而且 movemodel.py 每次会 rmtree(build/srmodels) 重建整个目录,
# 所以【不要】往 build/srmodels/ 里手工塞 commands_cn_kanhu.txt 之类的东西,
# 一重跑就没了 (而且那个残留文件会跟着被打进 srmodels.bin, 破坏 FST 编译)。
#
# 正确做法:
#   1. 改 model/multinet_model/fst/commands_cn.txt (组件源目录)
#   2. 跑 build_models 目标 (下面定义的), 它会强制重跑 movemodel.py
#   3. 再 idf.py build / flash
#
# 目标用法:
#     idf.py build_models
# ============================================================================

set(MVMODEL_EXE
    "${CMAKE_CURRENT_LIST_DIR}/managed_components/espressif__esp-sr/model/movemodel.py")

add_custom_target(build_models
    COMMENT "强制重新打包 MultiNet7 模型 (srmodels.bin)"
    COMMAND ${CMAKE_COMMAND} -E rm -rf "${CMAKE_CURRENT_BINARY_DIR}/srmodels"
    COMMAND python ${MVMODEL_EXE}
            -d1 "${CMAKE_CURRENT_SOURCE_DIR}/sdkconfig"
            -d2 "${CMAKE_CURRENT_SOURCE_DIR}/managed_components/espressif__esp-sr"
            -d3 "${CMAKE_CURRENT_BINARY_DIR}"
    COMMENT "srmodels.bin 已重新生成"
    VERBATIM)

# 每次编译都顺手确认模型产物是最新的, 并把校验信息打出来。
# 不自动重跑 (怕每次 build 都多花几秒), 但会警告。
add_custom_target(check_models
    COMMENT "校验 srmodels.bin 里的 commands_cn.txt 条数"
    COMMAND python "${CMAKE_CURRENT_LIST_DIR}/tools/check_srmodels.py"
            "${CMAKE_CURRENT_BINARY_DIR}/srmodels/srmodels.bin"
    VERBATIM)

# ============================================================================
# esp-dl 算子/内核补充 (2026-11-08 实测踩坑)
# ----------------------------------------------------------------------------
# esp-dl 是"按需裁剪"的: 每个模型组件用 esp_dl_compile_add_yml() 声明
# 自己要的算子和 Conv 内核, esp-dl 只编译这些。多组件共用一个 esp-dl 时,
# 所有 yml 会合并 —— 少一个算子, 对应模型反序列化就失败:
#
#     E dl::Model: Do not support GlobalAveragePool, please implement and register it first.
#     E dl::Model: There's no input in model.
#     Guru Meditation Error: LoadProhibited  EXCVADDR: 0x00000014
#     -> 板子无限重启 (每次重启播一声开机自检音 = "一直嘀嘀嘀")
#
# esp-sr 只声明了唤醒词要的 10 个算子, 人形检测 (pedestrian_detect)
# 需要的 Concat/GlobalAveragePool/Relu/Resize/Sigmoid 全都不在里面。
# pedestrian_detect 自己没注册 yml, 所以没人补 —— 这份 yml 就是来补的。
#
# 详见 espdl_compile_req.yml 里的完整说明。
# ============================================================================
idf_build_get_property(build_components BUILD_COMPONENTS)
if("espressif__esp-dl" IN_LIST build_components)
    set(_espdl_dir "${CMAKE_CURRENT_LIST_DIR}/managed_components/espressif__esp-dl")
else()
    set(_espdl_dir "${CMAKE_CURRENT_LIST_DIR}/managed_components/esp-dl")
endif()

if(EXISTS "${_espdl_dir}/cmake/compile_register.cmake")
    include("${_espdl_dir}/cmake/compile_register.cmake")
    esp_dl_compile_add_yml("${CMAKE_CURRENT_LIST_DIR}/espdl_compile_req.yml")
else()
    message(WARNING
        "esp-dl 的 compile_register.cmake 不存在, 人形检测模型可能因缺算子而崩溃。"
        "请确认 managed_components 下有 esp-dl 组件。")
endif()
