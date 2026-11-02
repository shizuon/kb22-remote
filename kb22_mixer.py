"""The KB22 mixer has a signed Q25 matrix; faders/pan/mute/solo are host state."""
import copy
import math
from kb22_protocol import Frame, ProtocolError, f32

SOURCES = (("IN12", "IN 1 / 2", 1), ("OTGIN", "OTG IN", 5), ("FX", "FX", 7),
           ("PB12", "Playback 1/2", 9), ("PB34", "Playback 3/4", 11),
           ("PB56", "Playback 5/6", 13), ("PB78", "Playback 7/8", 15))
BUSES = ("A", "B", "C")


def default_channel(link=True):
    return {"gain": 0, "pan": 50, "mute": False, "solo": False, "polarity": False}


def default_strip(source):
    linked = source != "IN12"
    left, right = default_channel(), default_channel()
    if linked:
        left["pan"], right["pan"] = 0, 100
    return {"link": linked, "left": left, "right": right}


def default_mixer():
    return {bus: {source: default_strip(source) for source, _, _ in SOURCES} for bus in BUSES}


def validate_mixer(mixer):
    if not isinstance(mixer, dict) or set(mixer) != set(BUSES):
        raise ProtocolError("混音配置必须包含 Mix A、B、C")
    for bus, strips in mixer.items():
        if not isinstance(strips, dict) or set(strips) != {s for s, _, _ in SOURCES}:
            raise ProtocolError(f"Mix {bus} 的输入通道不完整")
        for source, strip in strips.items():
            if not isinstance(strip, dict) or set(strip) != {"link", "left", "right"} or type(strip["link"]) is not bool:
                raise ProtocolError("立体声配置无效")
            for side in ("left", "right"):
                values = strip[side]
                if not isinstance(values, dict) or set(values) != {"gain", "pan", "mute", "solo", "polarity"}:
                    raise ProtocolError("混音参数不完整")
                for key, low, high in (("gain", -90, 12), ("pan", 0, 100)):
                    value = values[key]
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                        raise ProtocolError(f"{key} 必须在 {low}–{high} 之间")
                if any(type(values[k]) is not bool for k in ("mute", "solo", "polarity")):
                    raise ProtocolError("混音开关值无效")
    return copy.deepcopy(mixer)


def patch_mixer(mixer, bus, source, change):
    if bus not in BUSES or source not in {s for s, _, _ in SOURCES} or not isinstance(change, dict):
        raise ProtocolError("无效混音通道")
    result = copy.deepcopy(mixer)
    strip = result[bus][source]
    if "link" in change:
        if type(change["link"]) is not bool:
            raise ProtocolError("联动值无效")
        strip["link"] = change["link"]
        if strip["link"]:
            for key in ("gain", "mute", "solo", "polarity"):
                strip["right"][key] = strip["left"][key]
            strip["left"]["pan"], strip["right"]["pan"] = 0, 100
    for side in ("left", "right"):
        if side in change:
            if not isinstance(change[side], dict) or set(change[side]) - set(strip[side]):
                raise ProtocolError("无效混音参数")
            strip[side].update(change[side])
            if strip["link"]:
                other = "right" if side == "left" else "left"
                strip[other].update({k: v for k, v in change[side].items() if k != "pan"})
    if set(change) - {"link", "left", "right"}:
        raise ProtocolError("未知混音参数")
    return validate_mixer(result)


def matrix_frames(mixer, bus, source=None):
    """Mirror native f32 pan multiplication after the f64 -> Q25 conversion."""
    validate_mixer(mixer)
    if bus not in BUSES:
        raise ProtocolError("无效混音总线")
    strips = mixer[bus]
    solo_active = any(s[side]["solo"] for s in strips.values() for side in ("left", "right"))
    base = 0x4100 + BUSES.index(bus) * 0x200
    frames = []
    for name, _, offset in SOURCES:
        if source is not None and source != name:
            continue
        for side, delta in (("left", 0), ("right", 1)):
            params = strips[name][side]
            muted = params["mute"] or (solo_active and not params["solo"])
            level = 0 if muted or params["gain"] <= -90 else math.trunc(10 ** (params["gain"] / 20) * 2**25)
            sign = -1 if params["polarity"] else 1
            for out, pan in ((0, 100 - params["pan"]), (0x100, params["pan"])):
                raw = sign * math.trunc(f32(f32(f32(level) * f32(pan)) / 100))
                frames.append(Frame(base + out + offset + delta, raw))
    return frames


def read_native_mixer(sections):
    result = default_mixer()
    found = False
    def boolean(value):
        return str(value).lower() in ("true", "1")
    for bus in BUSES:
        for source, _, _ in SOURCES:
            section = sections.get("Mixer_" + source, {})
            prefix = "Mix" + bus + "_"
            if prefix + "Link" not in section:
                continue
            found = True
            strip = result[bus][source]
            strip["link"] = boolean(section[prefix + "Link"])
            for side, tail in (("left", "Left"), ("right", "Right")):
                for field, ini in (("gain", "GAIN"), ("mute", "MUTE"), ("solo", "SOLO"), ("polarity", "ANTI")):
                    text = section.get(prefix + ini + ("" if strip["link"] else tail), "0")
                    strip[side][field] = float(text) if field == "gain" else boolean(text)
                strip[side]["pan"] = float(section.get(prefix + "Balance" + ("Linked" if strip["link"] else "Unlink") + tail, "50"))
    return validate_mixer(result) if found else None
