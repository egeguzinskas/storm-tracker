import time
import dht
from machine import Pin, I2C, ADC
from bmp280 import BMP280

try:
    import ssd1306
except ImportError:
    ssd1306 = None

DHT_PIN = 4
I2C_SDA = 21
I2C_SCL = 22
RAIN_PWR_PIN = 25  # powers the rain module only while reading, to slow plate corrosion
RAIN_AO_PIN = 34
RAIN_DO_PIN = 35
OLED_ADDR = 0x3C
INTERVAL_S = 2  # DHT11 needs at least 1s between reads

TREND_SAMPLE_MS = 10 * 60 * 1000  # store a pressure sample every 10 min
TREND_WINDOW_MS = 3 * 60 * 60 * 1000  # standard 3-hour pressure tendency
TREND_MIN_MS = 30 * 60 * 1000  # need 30 min of history before reporting

dht_sensor = dht.DHT11(Pin(DHT_PIN))
i2c = I2C(0, sda=Pin(I2C_SDA), scl=Pin(I2C_SCL), freq=100000)
bmp = BMP280(i2c)
rain_pwr = Pin(RAIN_PWR_PIN, Pin.OUT, value=0)
rain_ao = ADC(Pin(RAIN_AO_PIN))
rain_ao.atten(ADC.ATTN_11DB)  # full 0-3.3V range
rain_do = Pin(RAIN_DO_PIN, Pin.IN)
time.sleep(2)  # let the sensors settle after power-up

oled = None


def show(lines):
    """Draw up to 8 lines on the SSD1306. The station keeps running if the screen is missing."""
    global oled
    try:
        if oled is None:
            if ssd1306 is None or OLED_ADDR not in i2c.scan():
                return
            oled = ssd1306.SSD1306_I2C(128, 64, i2c, addr=OLED_ADDR)
        oled.fill(0)
        oled.fill_rect(0, 0, 128, 10, 1)
        oled.text("STORM TRACKER", 12, 1, 0)
        for i, line in enumerate(lines):
            oled.text(line[:16], 0, 14 + i * 10)
        oled.show()
    except OSError:
        oled = None  # retry on the next loop, e.g. after a loose wire is fixed


def short_trend(trend):
    # "steady (+0.2 hPa/3h)" -> "steady +0.2/3h"; "trend in 30 min" stays as is
    return trend.replace(" hPa/3h", "/3h").replace("(", "").replace(")", "")

history = []  # (ticks_ms, hPa)


def pressure_trend(now, hpa):
    if not history or time.ticks_diff(now, history[-1][0]) >= TREND_SAMPLE_MS:
        history.append((now, hpa))
        while time.ticks_diff(now, history[0][0]) > TREND_WINDOW_MS:
            history.pop(0)
    span = time.ticks_diff(now, history[0][0])
    if span < TREND_MIN_MS:
        return "trend in {} min".format((TREND_MIN_MS - span + 59999) // 60000)
    # scale the change to hPa per 3 hours
    rate = (hpa - history[0][1]) * TREND_WINDOW_MS / span
    if rate > 1.0:
        word = "rising"
    elif rate < -1.0:
        word = "falling"
    else:
        word = "steady"
    return "{} ({:+.1f} hPa/3h)".format(word, rate)


def read_rain():
    """Return (wet, wetness %). DO goes low when wet; AO drops as the plate gets wetter."""
    rain_pwr.value(1)
    time.sleep_ms(50)
    ao = sum(rain_ao.read_u16() for _ in range(16)) // 16
    wet = rain_do.value() == 0
    rain_pwr.value(0)
    return wet, (65535 - ao) * 100 // 65535


while True:
    parts = []
    screen = []
    hum = trend = None
    try:
        t, hpa = bmp.read()
        parts.append("Temp: {:.1f} C ({:.1f} F)".format(t, t * 9 / 5 + 32))
    except OSError as e:
        t = hpa = None
        print("BMP280 read failed:", e)
    try:
        dht_sensor.measure()
        if t is None:
            t = dht_sensor.temperature()
            parts.append("Temp: {} C ({:.1f} F)".format(t, t * 9 / 5 + 32))
        hum = dht_sensor.humidity()
        parts.append("Humidity: {} %".format(hum))
    except OSError as e:
        print("DHT11 read failed:", e)
    if hpa is not None:
        trend = pressure_trend(time.ticks_ms(), hpa)
        parts.append("Pressure: {:.1f} hPa, {}".format(hpa, trend))
    wet, wetness = read_rain()
    parts.append("Rain: {} ({}%)".format("WET" if wet else "dry", wetness))
    if parts:
        print("  ".join(parts))
    if t is not None:
        screen.append("{:.1f}F  {:.1f}C".format(t * 9 / 5 + 32, t))
    screen.append("Humidity: {}%".format(hum) if hum is not None else "Humidity: --")
    if hpa is not None:
        screen.append("{:.1f} hPa".format(hpa))
        screen.append(short_trend(trend))
    else:
        screen.append("Pressure: --")
    screen.append("Rain: {} {}%".format("WET" if wet else "dry", wetness))
    show(screen)
    time.sleep(INTERVAL_S)
