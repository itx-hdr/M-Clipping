"""Render a saved sequence using bounded picture sections and exact cue times."""
import shutil
import tempfile
from pathlib import Path

from .. import config, media, render


def duration(document):
    return max((i["at_frame"] + i["frames"] for i in document["items"]), default=0)


def captions(document):
    width, height = document["canvas"]["width"], document["canvas"]["height"]
    size = max(16, round(min(width, height) * .065))
    margin = round(width * .06)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Text,Poppins ExtraBold,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,3,0,2,{margin},{margin},{round(height * .18)},1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for item in document["items"]:
        if item["kind"] not in ("caption", "title"):
            continue
        text = render._clean(item["text"])
        if not text:
            continue
        style = render.STYLES.get(item.get("style"), render.STYLES["clean"])
        position = f"\\an8\\pos({width//2},{round(height*.08)})" if item["kind"] == "title" else ""
        overrides = "{" + position + f"\\fn{style['font']}\\c&H{style['base']}&" + "}"
        events.append(f"Dialogue: 1,{render._ts(item['at_frame']/30)},"
                      f"{render._ts((item['at_frame']+item['frames'])/30)},Text,,0,0,0,,{overrides}{text}")
    return header + "\n".join(events) + "\n"


def sections(document):
    cursor = 0
    for item in sorted((i for i in document["items"] if i["kind"] == "video"), key=lambda i: i["at_frame"]):
        if item["at_frame"] > cursor:
            yield None, 0, item["at_frame"] - cursor
        yield item, item["source_in"], item["frames"]
        cursor = item["at_frame"] + item["frames"]
    if duration(document) > cursor:
        yield None, 0, duration(document) - cursor


def picture_filter(item, width, height, info):
    if (width, height) == (render.W, render.H):
        if item.get("framing"):
            return render._reaction_filter(item["framing"], info["width"], info["height"])
        _, foreground, _, _ = render.geometry(info["width"], info["height"], item.get("layout", "blur"))
        return render._filter(item.get("layout", "blur"), foreground, item.get("focus_x"), info["width"], info["height"])
    return (f"[0:v]fps=30,scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1[v0];")


def render_sequence(source, document, destination, progress=lambda value: None):
    total = duration(document)
    if total < 1:
        raise ValueError("Add a picture section or text before rendering.")
    width, height = document["canvas"]["width"], document["canvas"]["height"]
    if width % 2 or height % 2:
        raise ValueError("The output width and height must be even numbers.")
    info = media.probe(source)
    with tempfile.TemporaryDirectory(prefix="pke") as short:
        work = Path(short)
        (work / "f").mkdir()
        for font in (config.ROOT / "studio" / "fonts").glob("*.ttf"):
            shutil.copyfile(font, work / "f" / font.name)
        (work / "text.ass").write_text(captions(document), encoding="utf-8")
        parts, completed = [], 0
        for item, start, count in sections(document):
            for offset in range(0, count, 900):
                frames = min(900, count - offset)
                name = f"part{len(parts)}.mkv"
                base = ["ffmpeg", "-y", "-v", "error", "-filter_complex_threads", "1"]
                if item:
                    base += ["-ss", str((start + offset) / 30), "-t", str(frames / 30), "-i", str(source)]
                    graph = picture_filter(item, width, height, info)
                    graph += (f"[v0]tpad=stop_mode=clone:stop_duration=1,trim=end_frame={frames},setpts=N/(30*TB)[v];"
                              f"[0:a:0]aresample=48000:async=1:first_pts=0,aformat=channel_layouts=stereo,"
                              f"apad,atrim=end_sample={frames*1600},asetpts=N/SR/TB[a]")
                else:
                    base += ["-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r=30",
                             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
                    graph = (f"[0:v]trim=end_frame={frames},setpts=N/(30*TB)[v];"
                             f"[1:a]atrim=end_sample={frames*1600},asetpts=N/SR/TB[a]")
                progress(90 * completed / total)
                media.run(base + ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-c:v", "libx264",
                                 "-preset", "ultrafast", "-crf", "16", "-pix_fmt", "yuv420p", "-threads", "2",
                                 "-c:a", "pcm_s16le", name], cwd=work, timeout=max(600, frames // 3))
                parts.append(name)
                completed += frames
        (work / "parts.txt").write_text("".join(f"file '{name}'\n" for name in parts), encoding="utf-8")
        progress(92)
        media.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "1", "-i", "parts.txt",
                   "-vf", "ass=text.ass:fontsdir=f", "-af", f"apad,atrim=end_sample={total*1600}",
                   "-frames:v", str(total), "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                   "-threads", "2", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", "-b:a", "160k",
                   "-movflags", "+faststart", "output.mp4"], cwd=work, timeout=max(600, total // 3))
        check = media.probe(work / "output.mp4")
        if (check["width"], check["height"]) != (width, height) or abs(check["duration"] - total/30) > .1:
            raise RuntimeError("The exported size or duration does not match the sequence.")
        if check["video_codec"] != "h264" or check["audio_codec"] != "aac":
            raise RuntimeError("The exported file has an unexpected media format.")
        progress(98)
        shutil.copyfile(work / "output.mp4", destination)
    return check
