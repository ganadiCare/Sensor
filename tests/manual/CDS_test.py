import spidev
import time

spi = spidev.SpiDev()
spi.open(0, 0)  # SPI 버스 0, 디바이스 0
spi.max_speed_hz = 1350000

def read_channel(channel):
    adc = spi.xfer2([1, (8 + channel) << 4, 0])
    data = ((adc[1] & 3) << 8) + adc[2]
    return data

try:
    print("조도센서 테스트 시작... (Ctrl+C로 종료)")
    while True:
        value = read_channel(0)
        print(f"조도값: {value}")
        time.sleep(0.5)

except KeyboardInterrupt:
    print("종료")
    spi.close()