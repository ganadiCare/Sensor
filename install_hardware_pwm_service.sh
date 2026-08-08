#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "sudo bash install_hardware_pwm_service.sh 로 실행하세요." >&2
    exit 1
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SERVICE_NAME=hardware-pwm-setup.service

if [[ -n ${SUDO_USER:-} && ${SUDO_USER} != root ]]; then
    if ! id -nG "${SUDO_USER}" | tr ' ' '\n' | grep -Fxq gpio; then
        usermod -aG gpio "${SUDO_USER}"
        echo "${SUDO_USER} 사용자를 gpio 그룹에 추가했습니다. 재로그인 후 적용됩니다."
    fi
fi

install -m 0755 \
    "${SCRIPT_DIR}/setup_hardware_pwm.sh" \
    /usr/local/sbin/sensor-hardware-pwm-setup
install -m 0644 \
    "${SCRIPT_DIR}/${SERVICE_NAME}" \
    "/etc/systemd/system/${SERVICE_NAME}"

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"

echo
echo "하드웨어 PWM 부팅 자동 설정을 설치했습니다."
systemctl --no-pager --full status "${SERVICE_NAME}"
