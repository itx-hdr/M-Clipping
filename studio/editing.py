"""Validated source cuts, crop rectangles and caption times for an editorial render."""
import math


def number(value):
    if isinstance(value, bool):
        raise ValueError("Use a number for video times and crop positions.")
    n = float(value)
    if not math.isfinite(n):
        raise ValueError("Video times and crop positions must be finite.")
    return n


def cuts(value, lower=0.0, upper=float("inf")):
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ValueError("Choose one to eight source sections.")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"start", "end"}:
            raise ValueError("Each section needs only start and end times.")
        a, b = round(number(item["start"]), 2), round(number(item["end"]), 2)
        if not lower <= a < b <= upper or b-a < .2 or (result and a < result[-1]["end"]):
            raise ValueError("Sections must be in order, must not overlap, and must stay inside the source range.")
        result.append({"start": a, "end": b})
    if not 3 <= sum(c["end"]-c["start"] for c in result) <= 180:
        raise ValueError("The edited short must be 3 to 180 seconds long.")
    return result


def framing(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"gameplay", "reaction"}:
        raise ValueError("Choose both the gameplay area and the reaction camera area.")
    result = {}
    for name, rect in value.items():
        if not isinstance(rect, dict) or set(rect) != {"x", "y", "width", "height"}:
            raise ValueError("Each area needs x, y, width and height as fractions of the source image.")
        r = {k: number(v) for k, v in rect.items()}
        if not (0 <= r["x"] < 1 and 0 <= r["y"] < 1 and .02 <= r["width"] <= 1 and .02 <= r["height"] <= 1
                and r["x"]+r["width"] <= 1.00000001 and r["y"]+r["height"] <= 1.00000001):
            raise ValueError("The selected areas must fit inside the source image.")
        result[name] = r
    return result


def timeline(words, segments):
    result, offset = [], 0.0
    for index, c in enumerate(segments):
        for w in words:
            if w["s"] >= c["start"] - .001 and w["e"] <= c["end"] + .001:
                result.append(dict(w, s=round(w["s"]-c["start"]+offset, 3), e=round(w["e"]-c["start"]+offset, 3),
                                   _cut=index, _cut_end=round(offset+c["end"]-c["start"], 3)))
        offset += c["end"]-c["start"]
    return result, round(offset, 3)


def check_speech_boundaries(words, segments):
    for c in segments:
        if any(w["s"]+.04 < edge < w["e"]-.04 for edge in (c["start"], c["end"]) for w in words):
            raise ValueError("A cut crosses a spoken word. Move it to a word ending or a pause.")
