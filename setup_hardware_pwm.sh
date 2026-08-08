#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "sudo bash setup_hardware_pwm.sh 로 실행하세요." >&2
    exit 1
fi

PWM_ROOT=/sys/class/pwm
BOOT_CONFIG=/boot/firmware/config.txt
OVERLAY_LINE='dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4'

find_pwm0_chip() {
    local chip device
    for chip in "${PWM_ROOT}"/pwmchip*; do
        [[ -e ${chip} ]] || continue
        device=$(readlink -f "${chip}/device")
        if [[ ${device} == *1f00098000.pwm ]]; then
            echo "${chip}"
            return 0
        fi
    done
    return 1
}

PWM_CHIP=$(find_pwm0_chip || true)

if [[ -z ${PWM_CHIP} ]]; then
    if ! grep -Fxq "${OVERLAY_LINE}" "${BOOT_CONFIG}"; then
        if [[ ! -e ${BOOT_CONFIG}.before-hardware-pwm ]]; then
            cp -a "${BOOT_CONFIG}" "${BOOT_CONFIG}.before-hardware-pwm"
        fi
        {
            echo
            echo '# GPIO12/13 RP1 hardware PWM for camera servos'
            echo "${OVERLAY_LINE}"
        } >> "${BOOT_CONFIG}"
        echo "부팅 설정에 RP1 PWM0 오버레이를 추가했습니다."
        echo "백업: ${BOOT_CONFIG}.before-hardware-pwm"
    fi

    echo "RP1 PWM0 오버레이를 런타임으로 적용합니다."
    dtoverlay pwm-2chan pin=12 func=4 pin2=13 func2=4 || true
    sleep 1
    PWM_CHIP=$(find_pwm0_chip || true)

    if [[ -z ${PWM_CHIP} ]]; then
        echo "PWM0 활성화에는 재부팅이 필요합니다." >&2
        echo "sudo reboot 후 이 스크립트를 다시 실행하세요." >&2
        exit 2
    fi
fi

echo "RP1 PWM0 컨트롤러: ${PWM_CHIP}"

# Raspberry Pi 5 RP1: GPIO12=PWM0_CHAN0, GPIO13=PWM0_CHAN1
pinctrl set 12 a0 pd
pinctrl set 13 a0 pd

for channel in 0 1; do
    channel_path=${PWM_CHIP}/pwm${channel}

    if [[ ! -d ${channel_path} ]]; then
        echo "${channel}" > "${PWM_CHIP}/export"
    fi

    for _ in {1..50}; do
        [[ -d ${channel_path} ]] && break
        sleep 0.02
    done

    if [[ ! -d ${channel_path} ]]; then
        echo "PWM 채널 ${channel} 생성 실패" >&2
        exit 1
    fi

    echo 0 > "${channel_path}/enable"
    echo 0 > "${channel_path}/duty_cycle"
    echo 20000000 > "${channel_path}/period"

    chgrp gpio \
        "${channel_path}/enable" \
        "${channel_path}/duty_cycle" \
        "${channel_path}/period" \
        "${channel_path}/polarity"
    chmod g+rw \
        "${channel_path}/enable" \
        "${channel_path}/duty_cycle" \
        "${channel_path}/period" \
        "${channel_path}/polarity"
done

echo "GPIO12/13 하드웨어 PWM 설정 완료 (출력은 비활성 상태)"
pinctrl get 12,13
readlink -f "${PWM_CHIP}/device"
