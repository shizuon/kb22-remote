"""Typed KB22 controls and wire codec, independent of macOS and the HTTP server.

Addresses and conversions are derived from the locally installed KB Control Center
ARM64 command handlers. No vendor executable is needed at runtime. Incoming CRCs
were checked against recorded KB22 reports (see tests/fixtures/pro-upload.json).
"""
from dataclasses import dataclass
import math
import struct


class ProtocolError(ValueError):
    pass


def crc16(data):
    result = 0xffff
    for byte in data:
        result ^= byte
        for _ in range(8):
            result = (result >> 1) ^ 0xa001 if result & 1 else result >> 1
    return result


@dataclass(frozen=True)
class Frame:
    address: int
    value: int
    kind: int = 0x20
    index: int = 1
    channel: int = 1

    @property
    def key(self):
        return f"{self.address:04x}:{self.index}:{self.channel}"

    def pack(self, *, checksum=False):
        if not (0 <= self.address <= 65535 and -2**31 <= self.value < 2**31):
            raise ProtocolError("无效 USB 指令")
        payload = bytes((self.kind, self.index, self.channel))
        payload += struct.pack(">Hi", self.address, self.value)
        # The vendor's command serializer writes 00 00 in the CRC field. Device
        # reports include MODBUS CRC, but commands must mirror the native bytes.
        crc = crc16(payload) if checksum else 0
        return b"\x22\x33" + payload + struct.pack(">H", crc) + b"\x66\x77"

    def public(self):
        return {"address": f"{self.address:04x}", "value": self.value,
                "kind": self.kind, "index": self.index, "channel": self.channel}


def unpack_report(raw):
    # IOHID delivers a trailing zero on this device; some hidapi versions include
    # the report-id prefix instead. Neither byte belongs to the protocol frame.
    if raw[:3] == b"\x00\x22\x33":
        raw = raw[1:]
    if len(raw) < 15 or raw[:2] != b"\x22\x33" or raw[13:15] != b"\x66\x77":
        return None
    if crc16(raw[2:11]) != int.from_bytes(raw[11:13], "big"):
        return None
    return Frame(int.from_bytes(raw[5:7], "big"),
                 int.from_bytes(raw[7:11], "big", signed=True), raw[2], raw[3], raw[4])


def f32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def output_banked(address):
    # Full uploads send eight output memories in order, bracketed by 6b12.
    return address in (0x3103, 0x3203, 0x3108) or 0x6100 <= address <= 0x6b11


def hifi_memory_control(key):
    return isinstance(key, str) and (key.startswith('eq.hifi') or key == 'hifi.eqCount')


HIFI_COUNT_IDS = ('hifi.eqCount', 'eq.hifiL.count', 'eq.hifiR.count')


def validate_count_aliases(changes):
    for key in HIFI_COUNT_IDS:
        if key in changes:
            CONTROLS[key].validate(changes[key])
            if key != 'hifi.eqCount' and 'hifi.eqCount' in changes and changes[key] != changes['hifi.eqCount']:
                raise ProtocolError('左右共同段数与单侧段数冲突')
    for side in 'LR':
        count = changes.get(f'eq.hifi{side}.count', changes.get('hifi.eqCount'))
        if count is None:
            continue
        flags = [f'eq.hifi{side}.{band}.enabled' for band in range(1, 11)]
        if any(key in changes for key in flags):
            if not all(key in changes for key in flags) or sum(changes[key] is True for key in flags) != count:
                raise ProtocolError('启用段数必须与十段各自的启用状态一致')


@dataclass(frozen=True)
class Control:
    id: str
    label: str
    page: str
    group: str
    addresses: tuple
    minimum: float = 0
    maximum: float = 1
    step: float = 1
    unit: str = ""
    kind: str = "number"
    codec: str = "int"
    choices: tuple = ()
    modes: tuple = ("pro", "game")
    hint: str = ""
    confirm: str = ""
    rate_limit: int = 0
    # Logical banks use a selector frame immediately before the parameter frame.
    selector: tuple = ()
    readonly: bool = False
    banked: bool = False

    def validate(self, value):
        if self.readonly:
            raise ProtocolError("此项为只读")
        if self.kind == "toggle":
            if type(value) is not bool:
                raise ProtocolError(f"{self.label} 必须是开关值")
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ProtocolError(f"{self.label} 必须是有限数值")
        if self.choices and self.kind != "bitmask" and value not in [x[0] for x in self.choices]:
            raise ProtocolError(f"{self.label} 的选项无效")
        if not self.minimum <= value <= self.maximum:
            raise ProtocolError(f"{self.label} 必须在 {self.minimum:g}–{self.maximum:g} {self.unit} 之间")
        # Do not silently round a value received from an API client.
        if self.step and abs((value - self.minimum) / self.step - round((value - self.minimum) / self.step)) > 1e-6:
            raise ProtocolError(f"{self.label} 的步进是 {self.step:g}")
        return value

    def raw_value(self, value):
        value = self.validate(value)
        if self.codec == "tenths":
            return math.trunc(f32(f32(value) * 10))
        if self.codec == "q10000":
            return math.trunc(f32(f32(value) * 10000))
        if self.codec == "percent":
            return math.trunc(value * 100)
        if self.codec == "attenuation":
            return math.trunc(value * -2)
        if self.codec == "hifi_volume":
            # HiFi's knob counts up from 0 to 198. Native display is
            # (raw - 198) / 2 dB (formatter 0x1000da738).
            return int(198 + value * 2)
        if self.codec == "hifi_count":
            return (1 << 16) | (int(value) << 8) | int(value)
        if self.codec in ("hifi_count_left", "hifi_count_right"):
            raise ProtocolError("单侧 EQ 段数需要和另一侧段数一起编码")
        if self.codec == "hifi_balance":
            # Native settings callback 0x1000b50ac transforms half-dB steps:
            # negative positions become abs(step)*5, positive ones step*500.
            # Sending a signed integer here makes firmware clamp the value.
            return int(value * (1000 if value >= 0 else -10))
        if self.codec == "half":
            return int(value * 2)
        if self.codec == "negative":
            return -int(value)
        if self.codec == "type":
            return int(value) + 1
        if self.codec == "pingpong":
            return 1 if value else 2
        if self.codec == "q25":
            return 0 if value <= self.minimum else math.trunc(10 ** (value / 20) * 2**25)
        return int(value)

    def value_from_raw(self, raw):
        if self.kind == "toggle":
            return raw == 1 if self.codec == "pingpong" else bool(raw)
        if self.codec == "tenths":
            return raw / 10
        if self.codec == "q10000":
            return raw / 10000
        if self.codec == "percent":
            return raw / 100
        if self.codec == "attenuation":
            return raw / -2
        if self.codec == "hifi_volume":
            return (raw - 198) / 2
        if self.codec == "hifi_count":
            left, right = raw & 0xff, (raw >> 8) & 0xff
            return left if left == right else None
        if self.codec == "hifi_count_left":
            return raw & 0xff
        if self.codec == "hifi_count_right":
            return (raw >> 8) & 0xff
        if self.codec == "hifi_balance":
            return raw / 1000 if raw >= 100 else raw / -10
        if self.codec == "half":
            return raw / 2
        if self.codec == "negative":
            return -raw
        if self.codec == "type":
            return raw - 1
        if self.codec == "q25":
            return self.minimum if raw == 0 else round(20 * math.log10(abs(raw) / 2**25), 3)
        return raw

    def frames(self, value):
        raw = self.raw_value(value)
        if self.codec == "boost":
            return [Frame(0x6101 if raw == 4 else 0x6101 + 0x100 * raw, 0 if raw == 4 else 1)]
        return ([Frame(*self.selector)] if self.selector else []) + [Frame(a, raw) for a in self.addresses]

    def public(self):
        return {"id": self.id, "label": self.label, "page": self.page, "group": self.group,
                "min": self.minimum, "max": self.maximum, "step": self.step, "unit": self.unit,
                "kind": self.kind, "choices": [{"value": v, "label": l} for v, l in self.choices],
                "modes": self.modes, "hint": self.hint, "confirm": self.confirm,
                "rateLimit": self.rate_limit, "readonly": self.readonly, "banked": self.banked}


CONTROLS = {}


def add(id, label, page, group, addresses, minimum=0, maximum=1, **kwargs):
    if isinstance(addresses, int):
        addresses = (addresses,)
    control = Control(id, label, page, group, tuple(addresses), minimum, maximum, **kwargs)
    if id in CONTROLS:
        raise RuntimeError("duplicate control: " + id)
    CONTROLS[id] = control
    return control


def toggle(id, label, page, group, addresses, **kwargs):
    return add(id, label, page, group, addresses, kind="toggle", **kwargs)


def choice(id, label, page, group, addresses, options, **kwargs):
    if isinstance(options[0], str):
        options = tuple(enumerate(options))
    return add(id, label, page, group, addresses, min(v for v, _ in options), max(v for v, _ in options),
               kind="select", choices=tuple(options), **kwargs)


SOURCES = ((1, "Mix A"), (2, "Mix B"), (3, "Mix C"), (4, "IN 1"), (5, "IN 2"),
           (6, "IN 1+2"), (7, "OTG IN"), (8, "FX"), (9, "Playback 1/2"),
           (10, "Playback 3/4"), (11, "Playback 5/6"), (12, "Playback 7/8"))
OUTPUTS = ("全部", "线路输出", "耳机 6.35 mm", "耳机 3.5 mm", "全部耳机",
           "线路 + 耳机 3.5 mm", "线路 + 耳机 6.35 mm")
FILTERS = ("峰值", "低通", "高通", "低架", "高架")
ALL_MODES = ("pro", "game", "hifi")

# Physical inputs.
for ch, label in ((1, "IN 1"), (2, "IN 2"), (3, "OTG IN")):
    root, base = f"input.{ch}", 0x2000 + ch * 0x100
    add(root + ".gain", "输入增益" if ch < 3 else "音量", "input", label, base + 5,
        0 if ch < 3 else -90, 60 if ch < 3 else 12, unit="dB")
    toggle(root + ".mute", "静音", "input", label, base + 1)
    toggle(root + ".polarity", "反相", "input", label, base + 9)
    if ch < 3:
        toggle(root + ".instrument", "乐器高阻抗", "input", label, base + 3)
        toggle(root + ".phantom", "48V 幻象电源", "input", label, base + 2,
               confirm="仅在麦克风需要 48V 供电时开启；开启前请调低输入增益。")
    if ch == 2:
        choice(root + ".source", "输入接口", "input", label, base + 6, ("XLR / TRS", "3.5 mm 耳麦"))

# Output register values are attenuation in half-decibel steps. 60 = -30 dB.
choice("output.source", "输出信号源", "output", "耳机与线路", 0x3102, SOURCES, modes=("pro",))
choice("output.selection", "输出接口", "output", "耳机与线路", 0x3106, OUTPUTS)
add("output.options", "输出轮换列表", "output", "耳机与线路", 0x3107, 1, 127,
    kind="bitmask", choices=tuple((1 << i, name) for i, name in enumerate(OUTPUTS)),
    hint="选择设备按键轮换时包含的输出组合")
add("output.volume", "输出音量", "output", "耳机与线路", (0x3103, 0x3203), -99, 0,
    step=.5, unit="dB", codec="attenuation", banked=True)
toggle("output.mute", "输出静音", "output", "耳机与线路", (0x3104, 0x3204))
toggle("output.highGain", "耳机高增益", "output", "耳机与线路", 0x3108, banked=True)
choice("output.crossfeed", "耳机互馈", "output", "耳机与线路", 0x1116,
       ("关闭", "Studio", "Room"), modes=("game",))
choice("otg.source", "信号源", "output", "OTG OUT", 0x3302, SOURCES, modes=("pro",))
add("otg.volume", "音量", "output", "OTG OUT", (0x3303, 0x3403), -90, 12, unit="dB")
toggle("otg.mute", "静音", "output", "OTG OUT", (0x3304, 0x3404))

# Loopback stereo pairs use linear Q25, unlike analogue output attenuation.
for pair in range(3):
    base, root, label = 0x9100 + pair * 0x200, f"loopback.{pair + 1}", f"Loopback {pair * 2 + 1}/{pair * 2 + 2}"
    choice(root + ".source", "信号源", "loopback", label, base + 2, SOURCES, modes=("pro",))
    for suffix, title, addresses in (("volume", "联动音量", (base + 3, base + 0x103)),
                                     ("left", "左声道", (base + 3,)), ("right", "右声道", (base + 0x103,))):
        add(root + "." + suffix, title, "loopback", label, addresses, -90, 12, step=.1,
            unit="dB", codec="q25", modes=("pro",))
    toggle(root + ".mute", "静音", "loopback", label, (base + 4, base + 0x104), modes=("pro",))

# Dynamics and noise reduction. Parameters are integer units except ratio (x10).
for ch in (1, 2):
    for effect, base, title in (("compressor", 0x7200 + 0x100 * ch, "压缩"),
                                ("expander", 0x7400 + 0x100 * ch, "扩展")):
        root, group = f"{effect}.{ch}", f"IN {ch} · {title}"
        toggle(root + ".enabled", "启用", "dynamics", group, base + 1, rate_limit=48000, modes=("pro",))
        for key, name, offset, lo, hi, unit, step, codec in (
            ("threshold", "阈值", 2, -36, 0, "dB", 1, "int"),
            ("ratio", "比率", 3, 1, 20, ":1", .1, "tenths"),
            ("attack", "启动", 4, 1, 500, "ms", 1, "int"),
            ("release", "释放", 5, 1, 2000, "ms", 1, "int"),
            ("gain", "补偿增益", 6, 0, 36, "dB", 1, "int"),
            ("mix", "干湿比", 8, 0, 100, "%", 1, "int")):
            if key == 'ratio' and effect == 'compressor':
                hi = 51
            add(root + "." + key, name, "dynamics", group, base + offset, lo, hi, unit=unit,
                step=step, codec=codec, rate_limit=48000, modes=("pro",))
        toggle(root + ".autoGain", "自动补偿", "dynamics", group, base + 7, rate_limit=48000, modes=("pro",))
    toggle(f"denoise.{ch}.enabled", "降噪", "input", f"IN {ch}", 0x7003 + ch * 0x100, rate_limit=48000)
    add(f"denoise.{ch}.level", "降噪强度", "input", f"IN {ch}", 0x7002 + ch * 0x100, 0, 70, rate_limit=48000)
    toggle(f"ducking.{ch}.enabled", "触发闪避", "dynamics", "闪避 Ducking", 0x7607 + ch * 0x100, rate_limit=48000)
    add(f"ducking.{ch}.map", f"IN {ch} 压低的通道", "dynamics", "闪避 Ducking", 0x7704 + ch, 0, 31,
        kind="bitmask", choices=((1, "OTG IN"), (2, "Playback 1/2"), (4, "Playback 3/4"),
                                (8, "Playback 5/6"), (16, "Playback 7/8")), rate_limit=48000, modes=("pro",))
for key, label, address, lo, hi, unit in (("threshold", "触发阈值", 0x7701, -60, 0, "dB"),
                                        ("attack", "启动时间", 0x7702, 1, 1000, "ms"),
                                        ("reduction", "衰减量", 0x7703, 0, 60, "dB"),
                                        ("release", "释放时间", 0x7704, 1, 5000, "ms")):
    add("ducking." + key, label, "dynamics", "闪避 Ducking", address, lo, hi, unit=unit, rate_limit=48000)

# Professional PEQ: four bands per input and ten per output channel.
for target, group, count, family, side in (("in1", "IN 1 · 4 段", 4, 0x5000, 0),
                                         ("in2", "IN 2 · 4 段", 4, 0x5000, 8),
                                         ("outL", "输出左 · 10 段", 10, 0x6000, 0),
                                         ("outR", "输出右 · 10 段", 10, 0x6000, 8)):
    root = "eq." + target
    if target in ("in1", "in2"):
        toggle(root + ".enabled", "EQ 总开关", "eq", group, family + 0xb07 + side, modes=("pro",), rate_limit=48000)
        add(root + ".preamp", "EQ 输出增益", "eq", group, family + 0xb06 + side, -12, 12,
            unit="dB", modes=("pro",), rate_limit=48000)
    for band in range(1, count + 1):
        prefix, base = root + f".{band}", family + band * 0x100 + side
        kwargs = dict(modes=("pro",), rate_limit=48000 if family == 0x5000 else 192000, banked=family == 0x6000)
        toggle(prefix + ".enabled", "启用", "eq", group, base + 1, **kwargs)
        choice(prefix + ".type", "类型", "eq", group, base + 2, FILTERS, codec="type", **kwargs)
        add(prefix + ".gain", "增益", "eq", group, base + 3, -12, 12, step=.1, unit="dB", codec="tenths", **kwargs)
        add(prefix + ".frequency", "频率", "eq", group, base + 4, 20, 20000, unit="Hz", **kwargs)
        add(prefix + ".q", "Q", "eq", group, base + 5, .1, 20, step=.001, codec="q10000", **kwargs)
toggle("eq.output.enabled", "输出 EQ", "eq", "输出 EQ", 0x6b07, modes=("pro",), rate_limit=192000, banked=True)
add("eq.output.preamp", "前级增益", "eq", "输出 EQ", 0x6b11, -12, 12, unit="dB", modes=("pro",), rate_limit=192000, banked=True)

# Reverb and delay: the named profile presets are local UI state. The DSP accepts
# one active set of Room/DryWet/Decay/Width controls at a time.
for key, label, addr, lo, hi, unit in (("input1", "IN 1 发送", 0x8101, -90, 0, "dB"),
                                     ("input2", "IN 2 发送", 0x8201, -90, 0, "dB"),
                                     ("pan1", "IN 1 声像", 0x8105, 0, 100, "%"),
                                     ("pan2", "IN 2 声像", 0x8205, 0, 100, "%"),
                                     ("return", "效果返回", 0x8301, -90, 0, "dB"),
                                     ("delayMix", "延迟干湿比", 0x8502, 0, 100, "%"),
                                     ("time", "延迟时间", 0x8504, 1, 1500, "ms"),
                                     ("bpm", "速度", 0x8506, 120, 2500, "BPM"),
                                     ("feedback", "反馈", 0x8507, 0, 99, "%"),
                                     ("room", "房间大小", 0x8602, 1, 6, "档"),
                                     ("reverbMix", "混响干湿比", 0x8603, 0, 100, "%"),
                                     ("decay", "衰减", 0x8604, 0, 18, "档")):
    add("fx." + key, label, "fx", "延迟 / 混响", addr, lo, hi, unit=unit, modes=("pro",), rate_limit=48000)
toggle("fx.mute", "效果返回静音", "fx", "延迟 / 混响", 0x8303, modes=("pro",), rate_limit=48000)
toggle("fx.delayBypass", "旁通延迟", "fx", "延迟 / 混响", 0x850a, modes=("pro",), rate_limit=48000)
toggle("fx.pingpong", "乒乓延迟", "fx", "延迟 / 混响", 0x8501, codec="pingpong", modes=("pro",), rate_limit=48000)
choice("fx.process", "处理方式", "fx", "延迟 / 混响", 0x8304, ("串联", "并联"), modes=("pro",), rate_limit=48000)
choice("fx.unit", "时间基准", "fx", "延迟 / 混响", 0x8503, ((1, "时间"), (2, "BPM")), modes=("pro",), rate_limit=48000)
choice("fx.reverb", "混响类型", "fx", "延迟 / 混响", 0x8601, ((1, "旁通"), (2, "Studio"), (3, "Live"), (4, "Hall")), modes=("pro",), rate_limit=48000)
add("fx.width", "空间宽度", "fx", "延迟 / 混响", 0x8605, 0, 1, step=.05, codec="percent", modes=("pro",), rate_limit=48000)

# Common system settings. HiFi has separate addresses for most menu settings.
choice("device.brightness", "屏幕亮度", "device", "屏幕与设备", 0x1102, ("暗", "正常", "亮"))
choice("device.meterStyle", "电平表样式", "device", "屏幕与设备", 0x1103, ("白色", "彩色"), modes=("pro",))
toggle("device.autoPower", "自动待机", "device", "屏幕与设备", 0x1104)
choice("device.volumeMemory", "音量记忆", "device", "屏幕与设备", 0x110e, ("不记忆", "跟随输出"), modes=("pro",))
choice("device.eqMemory", "EQ 记忆", "device", "屏幕与设备", 0x110f, ("不记忆", "跟随输出"), modes=("pro",))
choice("device.language", "设备语言", "device", "屏幕与设备", 0x1113, ("English", "简体中文"))

# GAME displays a reordered subset of PRO actions, but keeps the same wire IDs.
PRO_BUTTONS = ("IN 1 高阻", "IN 2 高阻", "IN 2 耳麦", "IN 1 48V", "IN 2 48V", "IN 1 静音",
               "IN 2 静音", "IN 1+2 静音", "IN 1 EQ", "IN 2 EQ", "输出 EQ", "耳机增益",
               "输出选择", "输出静音", "亮度", "息屏")
GAME_BUTTONS = tuple((value, PRO_BUTTONS[value]) for value in
                     (5, 6, 12, 14, 15, 11, 13, 0, 1, 2, 3, 4, 7))
HIFI_BUTTONS = ("输入选择", "输出选择", "亮度", "息屏", "静音", "PEQ 选择", "PCM 滤波", "耳机增益", "PEQ 开关")
for mode, options in (("pro", PRO_BUTTONS), ("game", GAME_BUTTONS)):
    for key, label, address in (("knob", "双击旋钮", 0x1110), ("longUp", "长按上键", 0x1111), ("longDown", "长按下键", 0x1112)):
        choice(f"{mode}.button.{key}", label, "device", "按键功能", address, options, modes=(mode,))

# HiFi uses its own menu and PEQ register families.
HIFI = dict(modes=("hifi",))
choice("hifi.input", "输入接口", "input", "HiFi 输入", 0x1320, ("OTG", "USB-C"), **HIFI)
add("hifi.volume", "输出音量", "output", "HiFi 输出", 0x1389, -99, 0, step=.5, unit="dB", codec="hifi_volume", **HIFI)
toggle("hifi.mute", "静音", "output", "HiFi 输出", 0x1369, **HIFI)
choice("hifi.output", "输出接口", "output", "HiFi 输出", 0x1330, OUTPUTS, **HIFI)
add("hifi.options", "输出轮换列表", "output", "HiFi 输出", 0x1331, 1, 127, kind="bitmask",
    choices=tuple((1 << i, name) for i, name in enumerate(OUTPUTS)), **HIFI)
choice("hifi.pcm", "PCM 滤波", "output", "HiFi 输出", 0x1332, ("快速滚降", "慢速滚降", "最小相位"), **HIFI)
toggle("hifi.highGain", "耳机高增益", "output", "HiFi 输出", 0x1333, **HIFI)
add("hifi.balance", "左右平衡", "output", "HiFi 输出", 0x1334, -9.5, 9.5, step=.5, unit="dB", codec="hifi_balance", hint="负值偏左，正值偏右；每步 0.5 dB", **HIFI)
choice("hifi.volumeStep", "音量步进", "output", "HiFi 输出", 0x1335, ("0.5 dB", "1 dB"), **HIFI)
toggle("hifi.polarity", "反相", "output", "HiFi 输出", 0x1336, **HIFI)
toggle("hifi.peq", "PEQ 总开关", "eq", "HiFi PEQ", 0x1340, **HIFI)
choice("hifi.preset", "设备 PEQ 槽位", "eq", "HiFi PEQ", 0x1341, ("EQ 1", "EQ 2", "EQ 3"), **HIFI)
add("hifi.eqCount", "启用前 N 段（左右声道）", "eq", "HiFi PEQ", 0x138b, 0, 10, codec="hifi_count", hint="设置后启用前 N 段，旁通其余频段；显示值为实际启用数量", **HIFI)
for side, base in (("L", 0x1500), ("R", 0x1400)):
    root, group = "eq.hifi" + side, "HiFi " + ("左" if side == "L" else "右") + " · 10 段"
    add(root + ".count", "启用前 N 段（" + ("左" if side == "L" else "右") + "声道）", "eq", group,
        0x138b, 0, 10, codec="hifi_count_" + ("left" if side == "L" else "right"), hint="设置后启用前 N 段，旁通其余频段；各段频率、增益和 Q 保留", **HIFI)
    add(root + ".preamp", "前级增益", "eq", group, base + 0xc1, -12, 12, step=.1, unit="dB", codec="tenths", **HIFI)
    for band in range(1, 11):
        prefix, address = root + f".{band}", base + band * 0x10
        toggle(prefix + ".enabled", "启用", "eq", group, address + 5, **HIFI)
        choice(prefix + ".type", "类型", "eq", group, address + 1, FILTERS, codec="type", **HIFI)
        add(prefix + ".gain", "增益", "eq", group, address + 3, -12, 12, step=.1, codec="tenths", unit="dB", **HIFI)
        add(prefix + ".frequency", "频率", "eq", group, address + 2, 20, 20000, unit="Hz", **HIFI)
        add(prefix + ".q", "Q", "eq", group, address + 4, .1, 20, step=.001, codec="q10000", **HIFI)
choice("hifi.brightness", "屏幕亮度", "device", "HiFi 屏幕与设备", 0x1310, ("暗", "正常", "亮"), **HIFI)
choice("hifi.meterStyle", "电平表样式", "device", "HiFi 屏幕与设备", 0x1311, ("白色", "彩色"), **HIFI)
choice("hifi.iconStyle", "图标样式", "device", "HiFi 屏幕与设备", 0x1312, ("填充", "线框"), **HIFI)
toggle("hifi.autoPower", "自动待机", "device", "HiFi 屏幕与设备", 0x1350, **HIFI)
for key, name, address in (("volumeMemory", "音量记忆", 0x1351), ("eqMemory", "EQ 记忆", 0x1352)):
    choice("hifi." + key, name, "device", "HiFi 屏幕与设备", address, ("不记忆", "跟随输出", "跟随输入"), **HIFI)
for key, name, address in (("knob", "短按旋钮", 0x1353), ("longUp", "长按上键", 0x1354),
                            ("shortDown", "短按下键", 0x1355), ("longDown", "长按下键", 0x1356)):
    choice("hifi.button." + key, name, "device", "HiFi 按键功能", address, HIFI_BUTTONS, **HIFI)
choice("hifi.language", "设备语言", "device", "HiFi 屏幕与设备", 0x1357, ("English", "简体中文"), **HIFI)

# Game routing is boolean; it must never pass through the Q25 Pro mixer.
GAME = dict(modes=("game",))
for source, label, offset in (("in1", "IN 1", 1), ("in2", "IN 2", 2), ("otg", "OTG IN", 5),
                               ("pb12", "Playback 1/2", 9), ("pb34", "Playback 3/4", 11), ("pb56", "Playback 5/6", 13)):
    for destination, name, base in (("hp", "发送至耳机 / 线路", 0x4100), ("otg", "发送至 OTG OUT", 0x4300), ("recording", "发送至 Recording", 0x4500)):
        if source == "otg" and destination == "otg":
            continue
        toggle(f"game.route.{source}.{destination}", name, "mixer", label, base + offset, **GAME)
    if source.startswith("pb"):
        base = 0xa100 + (offset - 9) * 0x100
        add(f"game.{source}.volume", "音量", "mixer", label, base + 2, -90, 12, unit="dB", **GAME)
        toggle(f"game.{source}.mute", "静音", "mixer", label, base + 3, **GAME)
for ch in (1, 2):
    add(f"game.voice.{ch}", "人声增强", "input", f"IN {ch}", 0x7202 + ch * 0x100, 0, 36, codec="negative", **GAME)
    add(f"game.ducking.{ch}", "闪避深度", "dynamics", "电竞闪避", 0x7608 + ch * 0x100, 0, 100, unit="%", **GAME)
choice("game.boost", "耳机增强模式", "output", "电竞声音增强", (0x6101, 0x6201, 0x6301, 0x6401),
       ("弱 LOW", "中 MID", "强 HIGH", "自定义 CUSTOM", "关闭 BYPASS"), codec="boost", **GAME)
for key, name, address in (("low", "低频", 0x6413), ("mid", "中频", 0x6414), ("high", "高频", 0x6415), ("gain", "增强", 0x730c)):
    add("game.boost." + key, name, "output", "电竞声音增强", address, 0, 88, **GAME)
add("game.recording.volume", "录音音量", "output", "Recording", (0x9103, 0x9203), -90, 12, unit="dB", codec="q25", **GAME)
toggle("game.recording.mute", "录音静音", "output", "Recording", (0x9104, 0x9204), **GAME)
choice("fx.channel", "乒乓起始声道", "fx", "延迟 / 混响", 0x8508, ("左", "右"), modes=("pro",), rate_limit=48000)
choice("fx.subdivision", "延迟节拍", "fx", "延迟 / 混响", 0x8505,
       tuple(f"1/{denom}{suffix}" for denom in (64, 32, 16, 8, 4, 2) for suffix in ("T", "", "D")),
       modes=("pro",), rate_limit=48000,
       hint="T 为三连音，D 为附点；范围和速度限制与 KB22 原软件一致")


def schema():
    return [control.public() for control in CONTROLS.values()]


def factory_reset_frame(mode):
    if mode not in ('pro', 'game', 'hifi'):
        raise ProtocolError('恢复出厂前需要确认工作模式')
    return Frame(0x1358 if mode == 'hifi' else 0x110c, 1)


def encode_changes(changes, *, hifi_preset=0):
    if not isinstance(changes, dict) or not changes or len(changes) > 600:
        raise ProtocolError("请求必须包含 1–600 个控制项")
    frames = []
    validate_count_aliases(changes)
    counts = None
    if any(key in changes for key in HIFI_COUNT_IDS):
        if isinstance(hifi_preset, bool) or hifi_preset not in (0, 1, 2):
            raise ProtocolError("HiFi EQ 预设无效")
        common = changes.get('hifi.eqCount')
        counts = [changes.get('eq.hifi' + side + '.count', common) for side in 'LR']
        if any(value is None for value in counts):
            raise ProtocolError("尚未读到另一侧的 HiFi EQ 段数")
        for side, value in zip('LR', counts):
            CONTROLS['eq.hifi' + side + '.count'].validate(value)
            if common is not None and value != common:
                raise ProtocolError("左右共同段数与单侧段数冲突")
        counts = ((int(hifi_preset) + 1) << 16) | (int(counts[1]) << 8) | int(counts[0])
    for key, value in changes.items():
        control = CONTROLS.get(key)
        if control is None:
            raise ProtocolError("未知控制项：" + str(key))
        if key in HIFI_COUNT_IDS:
            control.validate(value)
            if counts is not None:
                frames.append(Frame(0x138b, counts))
                counts = None
            continue
        encoded = control.frames(value)
        frames.extend(encoded)
    # Native KB22 effect sends update the UI register plus two Q25 audio
    # coefficients. The application supplies both fields from one locked state.
    for ch in (1, 2):
        if f"fx.input{ch}" in changes and f"fx.pan{ch}" in changes:
            gain, pan = changes[f"fx.input{ch}"], changes[f"fx.pan{ch}"]
            level = 0 if gain <= -90 else math.trunc(10 ** (gain / 20) * 2**25)
            for offset, proportion in ((6, 100 - pan), (7, pan)):
                raw = math.trunc(f32(f32(f32(level) * f32(proportion)) / 100))
                frames.append(Frame(0x8000 + ch * 0x100 + offset, raw))
    return frames
