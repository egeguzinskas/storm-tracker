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
glyph_buf = bytearray(8)
glyph = None  # 8x8 scratch framebuffer used to scale up the built-in font

GRAPH_SAMPLE_MS = 90 * 1000  # 128 samples x 90 s = 3.2 hours across the screen
GRAPH_TOP = 37
graph = []  # hPa, one per GRAPH_SAMPLE_MS
graph_last = None


def big_text(s, x, y, scale):
    """Draw text at scale x the 8x8 font size."""
    for ch in s:
        glyph.fill(0)
        glyph.text(ch, 0, 0, 1)
        for cy in range(8):
            for cx in range(8):
                if glyph.pixel(cx, cy):
                    oled.fill_rect(x + cx * scale, y + cy * scale, scale, scale, 1)
        x += (5 if ch == "." else 8) * scale  # tighten the gap around the decimal point
    return x


def draw_graph(wet):
    top, bottom = GRAPH_TOP, 63
    oled.hline(0, bottom, 128, 1)
    if len(graph) < 2:
        oled.text("graph building", 8, 46)
        return
    lo, hi = min(graph), max(graph)
    if hi - lo < 2.0:  # at least a 2 hPa span so sensor noise looks flat
        mid = (hi + lo) / 2
        lo, hi = mid - 1.0, mid + 1.0
    h = bottom - 1 - top
    x0 = 128 - len(graph)  # newest sample at the right edge
    prev = None
    for i, v in enumerate(graph):
        pt = (x0 + i, bottom - 1 - int((v - lo) / (hi - lo) * h + 0.5))
        if prev:
            oled.line(prev[0], prev[1], pt[0], pt[1], 1)
        prev = pt
    if wet:
        oled.fill_rect(0, top, 28, 9, 1)
        oled.text("WET", 2, top + 1, 0)


def show(t, hum, hpa, wet):
    """Big temperature, humidity + pressure, then a pressure graph.
    The station keeps running if the screen is missing."""
    global oled, glyph
    try:
        if oled is None:
            if ssd1306 is None or OLED_ADDR not in i2c.scan():
                return
            oled = ssd1306.SSD1306_I2C(128, 64, i2c, addr=OLED_ADDR)
            import framebuf
            glyph = framebuf.FrameBuffer(glyph_buf, 8, 8, framebuf.MONO_VLSB)
        oled.fill(0)
        temp = "--.-" if t is None else "{:.1f}".format(t * 9 / 5 + 32)
        x = big_text(temp, 0, 0, 3 if len(temp) <= 4 else 2)
        oled.rect(x + 1, 1, 4, 4, 1)  # degree sign
        big_text("F", x + 7, 0, 2)
        line = "{}% ".format("--" if hum is None else hum)
        line += "--" if hpa is None else "{:.1f}hPa".format(hpa)
        oled.text(line[:16], 0, 27)
        draw_graph(wet)
        oled.show()
    except OSError:
        oled = None  # retry on the next loop, e.g. after a loose wire is fixed


def record_graph(now, hpa):
    global graph_last
    if graph_last is None or time.ticks_diff(now, graph_last) >= GRAPH_SAMPLE_MS:
        graph_last = now
        graph.append(hpa)
        if len(graph) > 128:
            graph.pop(0)

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
    if hpa is not None:
        record_graph(time.ticks_ms(), hpa)
    show(t, hum, hpa, wet)
    time.sleep(INTERVAL_S)
