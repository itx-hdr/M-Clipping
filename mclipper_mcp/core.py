"""The local bridge and its recorded source and export gates. No AI keys are read here."""
import hashlib
from contextlib import contextmanager
import json
import math
import os
from studio.compat import env as compatible_env
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
DIMENSIONS = ("hook", "clarity", "payoff", "energy", "shareability")


def youtube_url(value):
    p = urlsplit(str(value))
    if p.scheme != "https" or p.username or p.password:
        raise ValueError("Use a public HTTPS YouTube video link.")
    if p.hostname in ("youtu.be", "www.youtu.be"):
        vid = p.path.strip("/")
    elif p.hostname in ("youtube.com", "www.youtube.com", "m.youtube.com"):
        vid = parse_qs(p.query).get("v", [""])[0] if p.path == "/watch" else p.path.split("/")[-1] if p.path.startswith(("/shorts/", "/live/")) else ""
    else:
        vid = ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        raise ValueError("Use one YouTube video, not a channel or playlist.")
    return "https://www.youtube.com/watch?v=" + vid


class Bridge:
    def __init__(self, root=ROOT, data=None, transport=None):
        self.root = Path(root)
        self.data = Path(data or compatible_env("MCLIPPER_MCP_DATA") or self.root / "data" / "mcp")
        self.data.mkdir(parents=True, exist_ok=True)
        self.base = compatible_env("MCLIPPER_MCP_URL", "http://127.0.0.1:8765").rstrip("/")
        p = urlsplit(self.base)
        if p.scheme != "http" or p.hostname not in ("127.0.0.1", "localhost", "::1") or p.username or p.password or p.path or p.query or p.fragment:
            raise ValueError("The local MCP bridge requires a loopback M clipper HTTP address.")
        self.http = httpx.Client(base_url=self.base, timeout=45, follow_redirects=False, trust_env=False, transport=transport)
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS history (at REAL, key TEXT, value TEXT)")

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.data / "workflow.sqlite", timeout=15)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def put(self, key, value):
        raw = json.dumps(value, ensure_ascii=False)
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, raw))
            db.execute("INSERT INTO history VALUES (?, ?, ?)", (time.time(), key, raw))
        return value

    def get(self, key):
        with self.db() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def brief(self):
        text = (self.root / "NICHE.md").read_text(encoding="utf-8")
        return {"game": "Grand Theft Auto VI", "language": "English", "minimum_score": 85,
                "allowed_footage": ["player_gameplay", "gameplay_reaction"], "instructions": text,
                "version": hashlib.sha256(text.encode()).hexdigest()}

    def api(self, method, path, **kwargs):
        try:
            r = self.http.request(method, path, **kwargs)
        except httpx.RequestError:
            raise ValueError("M clipper is not reachable. Start it with python -m studio.server.") from None
        if r.status_code in (401, 403):
            raise ValueError("The MCP bridge requires M clipper's built-in local owner mode; this server requires sign-in.")
        if r.status_code >= 300:
            raise ValueError(f"M clipper returned HTTP {r.status_code}; inspect the dashboard before retrying.")
        return r.json()

    @staticmethod
    def digest(path):
        h = hashlib.sha256()
        with Path(path).open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def run(self, args, timeout=45):
        try:
            p = subprocess.run(args, cwd=self.root, capture_output=True, timeout=timeout, creationflags=HIDDEN)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("The media operation did not finish. Keep this source or clip unverified.") from None
        if p.returncode:
            raise ValueError("The media operation failed. Keep this source or clip unverified.")
        return p.stdout

    def app_python(self):
        path = self.root / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
        if not path.is_file():
            raise ValueError("Install M clipper's application environment in .venv first.")
        return str(path)

    def search(self, query="", limit=5):
        query = str(query).strip()[:180]
        if re.search(r"\b(minecraft|elden ring|malenia|only up|chained together|gta\s*(?:5|v)\b)\b", query, re.I):
            raise ValueError("That search is outside the GTA VI gameplay brief.")
        n = max(1, min(8, int(limit)))
        raw = self.run([self.app_python(), "-m", "yt_dlp", "--flat-playlist", "--dump-single-json", "--quiet", "--no-warnings",
                        "--socket-timeout", "12", f"ytsearch{n}:GTA VI GTA 6 gameplay English {query}"])
        entries = json.loads(raw).get("entries") or []
        return {"brief": self.brief()["version"], "sources": [{"url": youtube_url("https://youtu.be/" + e["id"]),
                "title": e.get("title"), "creator": e.get("channel"), "duration": e.get("duration"),
                "views": e.get("view_count"), "status": "unverified"} for e in entries if e.get("id")],
                "note": "Search metadata is untrusted and does not establish GTA VI gameplay authenticity."}

    def sample_source(self, url, at):
        url = youtube_url(url)
        at = float(at)
        if not math.isfinite(at) or at < 0:
            raise ValueError("Choose a finite, nonnegative video time.")
        meta = self.api("GET", "/api/preview", params={"url": url})
        if at + 1 > float(meta.get("duration") or 0):
            raise ValueError("The sample is outside this video.")
        sid = uuid.uuid4().hex
        folder = self.data / "samples" / sid
        folder.mkdir(parents=True)
        self.run([self.app_python(), "-m", "yt_dlp", "--quiet", "--no-warnings", "--no-playlist", "--retries", "0",
                  "--fragment-retries", "0", "--socket-timeout", "10", "--download-sections", f"*{at}-{at + 3}",
                  "--force-keyframes-at-cuts", "-f", "bv*[height<=720]+ba/b[height<=720]", "--merge-output-format", "mp4",
                  "-o", str(folder / "sample.%(ext)s"), url])
        candidates = list(folder.glob("sample.*"))
        media = next((p for p in candidates if p.suffix in (".mp4", ".mkv", ".webm")), None)
        if media is None:
            raise ValueError("No sample was fetched. This source is still unverified.")
        image = folder / "frame.jpg"
        self.run(["ffmpeg", "-y", "-v", "error", "-i", str(media), "-frames:v", "1", "-vf", "scale=960:-2", str(image)])
        record = {"id": sid, "url": url, "at": at, "brief": self.brief()["version"],
                  "image": str(image), "image_sha256": self.digest(image), "created": time.time()}
        self.put("sample:" + sid, record)
        return record

    def review_source(self, url, game, footage_type, authenticity, language, start, end, sample_ids, evidence_urls, notes):
        url = youtube_url(url)
        brief = self.brief()
        if game != brief["game"] or footage_type not in brief["allowed_footage"] or authenticity != "verified" or language != "English":
            raise ValueError("Only verified English GTA VI player gameplay or gameplay reactions qualify. Uncertain footage stays blocked.")
        start, end = float(start), float(end)
        if not all(math.isfinite(t) for t in (start, end)) or start < 0 or not 5 <= end - start <= 180:
            raise ValueError("Review a specific 5–180 second gameplay range.")
        if not 40 <= len(notes.strip()) <= 2000:
            raise ValueError("Record what proves this is authentic player gameplay, including the visible action.")
        if not evidence_urls or len(evidence_urls) > 5:
            raise ValueError("Provide primary evidence links for the authenticity review.")
        for evidence in evidence_urls:
            p = urlsplit(evidence)
            if p.scheme != "https" or p.username or p.password or not p.hostname or not (p.hostname == "rockstargames.com" or p.hostname.endswith(".rockstargames.com")):
                raise ValueError("Use official Rockstar HTTPS evidence links; a video title alone is not proof.")
        samples = [self.get("sample:" + str(sid)) for sid in sample_ids]
        if not 2 <= len(samples) <= 6 or any(not s or s["url"] != url or s["brief"] != brief["version"] or not start <= s["at"] < end or
                                           time.time() - s["created"] > 86400 for s in samples):
            raise ValueError("Inspect at least two recent frames from this reviewed source range under the current brief.")
        if len({s["image_sha256"] for s in samples}) < 2 or max(s["at"] for s in samples) - min(s["at"] for s in samples) < min(5, (end-start)/2):
            raise ValueError("Inspect distinct, separated moments rather than repeated copies of one frame.")
        return self.put("source:" + url, {"url": url, "game": game, "footage_type": footage_type, "language": language,
            "start": start, "end": end, "brief": brief["version"], "sample_ids": sample_ids, "evidence_urls": evidence_urls,
            "notes": notes, "review_basis": "Agent-reviewed samples and primary evidence", "created": time.time()})

    def queue(self, url, max_clips=2):
        url = youtube_url(url)
        review = self.get("source:" + url)
        if not review or review["brief"] != self.brief()["version"] or time.time() - review["created"] > 86400:
            raise ValueError("This source needs a current GTA VI gameplay review before it can be queued.")
        job = self.api("POST", "/api/jobs", json={"url": url, "options": {"range_start": review["start"], "range_end": review["end"],
            "max_clips": max(1, min(4, int(max_clips))), "min_len": 12, "max_len": 45, "viral_min_score": 80, "style": "clean", "layout": "blur", "quality": 1080}})
        self.put("job:" + job["id"], {"source_review": review, "brief": self.brief()["version"]})
        return job

    def clip(self, jid, cid):
        if not ID.fullmatch(jid) or not ID.fullmatch(cid):
            raise ValueError("Invalid job or clip identifier.")
        job = self.api("GET", "/api/jobs/" + jid)
        clip = next((c for c in job.get("clips", []) if c.get("id") == cid), None)
        if clip is None:
            raise ValueError("That clip does not exist for this local owner.")
        return job, clip

    def current_job(self, jid):
        record = self.get("job:" + jid)
        if not record or record["brief"] != self.brief()["version"]:
            raise ValueError("This job has no source review for the current GTA VI brief.")
        return record

    def edit_plan(self, jid, cid, segments, framing, hook, caption_style="clean"):
        from studio import editing
        record = self.current_job(jid)
        self.clip(jid, cid)
        source = record["source_review"]
        plan = editing.cuts(segments, source["start"], source["end"])
        areas = editing.framing(framing)
        capability = self.api("GET", "/api/status").get("editing", {})
        if capability.get("version", 0) < 1:
            raise ValueError("Restart M clipper to load montage and reaction framing before queueing this edit.")
        if not hook.strip() or len(hook) > 60 or len(hook.split()) > 5 or caption_style not in ("clean", "hype", "bounce"):
            raise ValueError("Choose a specific English hook of at most five words and a supported caption style.")
        result = self.api("POST", f"/api/jobs/{jid}/clips/{cid}/render",
                          json={"segments": plan, "framing": areas, "hook": hook, "style": caption_style})
        self.put(f"review:{jid}:{cid}", None)
        self.put(f"inspection:{jid}:{cid}", None)
        return result

    def download_clip(self, jid, cid):
        job, clip = self.clip(jid, cid)
        name = clip.get("file", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.mp4", name):
            raise ValueError("Wait until a rendered clip file exists.")
        folder = self.data / "renders" / jid / cid
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "video.mp4"
        tmp = folder / (uuid.uuid4().hex + ".tmp")
        try:
            with self.http.stream("GET", f"/media/{jid}/{name}") as response:
                if response.status_code != 200:
                    raise ValueError("M clipper could not serve this rendered clip.")
                size = 0
                with tmp.open("wb") as out:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 512 * 1024 * 1024:
                            raise ValueError("This clip is too large for the local review cache.")
                        out.write(chunk)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
        return job, clip, path

    def inspect_clip(self, jid, cid):
        job, clip, path = self.download_clip(jid, cid)
        probe = json.loads(self.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]))
        duration = float(probe["format"]["duration"])
        if not math.isfinite(duration) or not 3 <= duration <= 180:
            raise ValueError("This is not a valid finished short.")
        frames = []
        for i, t in enumerate((0.3, min(1.5, duration/3), duration/2, duration*.8, duration-.2)):
            image = path.parent / f"frame-{i}.jpg"
            self.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1", "-vf", "scale=360:-2", str(image)])
            frames.append({"at": round(t, 2), "path": str(image)})
        audio = path.parent / "audio.wav"
        self.run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", str(audio)])
        self.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"])
        video = next((s for s in probe["streams"] if s["codec_type"] == "video"), {})
        sound = next((s for s in probe["streams"] if s["codec_type"] == "audio"), {})
        technical = video.get("width") == 1080 and video.get("height") == 1920 and video.get("codec_name") == "h264" and sound.get("codec_name") == "aac"
        record = {"job_id": jid, "clip_id": cid, "sha256": self.digest(path), "version": clip.get("version"),
                  "brief": self.brief()["version"], "duration": duration, "media_passed": technical,
                  "frames": frames, "audio": str(audio), "file": str(path), "created": time.time()}
        self.put(f"inspection:{jid}:{cid}", record)
        return {"clip": clip, "source": job.get("source"), "inspection": record,
                "review_note": "Frames cannot verify motion or sound alone. Review the returned audio and local MP4; confirm GTA VI gameplay throughout."}

    def review_clip(self, jid, cid, game, footage_type, language, checks, dimensions, penalty, issues, notes):
        self.current_job(jid)
        _, clip = self.clip(jid, cid)
        if clip.get("status") != "done":
            raise ValueError("Wait for a successful render before recording a final review.")
        inspection = self.get(f"inspection:{jid}:{cid}")
        brief = self.brief()
        if not inspection or inspection["brief"] != brief["version"] or inspection["version"] != clip.get("version"):
            raise ValueError("Inspect this exact rendered version before reviewing it.")
        if game != brief["game"] or footage_type not in brief["allowed_footage"] or language != "English":
            raise ValueError("The rendered clip is outside the GTA VI English gameplay brief.")
        required = ("authentic_gameplay", "truthful_hook", "standalone_context", "payoff_visible", "natural_ending", "captions_checked", "framing_checked", "audio_checked", "watched_full_video")
        if set(dimensions) != set(DIMENSIONS) or any(type(v) is not int or not 0 <= v <= 20 for v in dimensions.values()) or type(penalty) is not int or not 0 <= penalty <= 40:
            raise ValueError("Grade the five named dimensions from 0–20 and the penalty from 0–40.")
        if not isinstance(issues, list) or not 40 <= len(notes.strip()) <= 2000:
            raise ValueError("Record review notes and every unresolved issue.")
        score = max(0, sum(dimensions.values()) - penalty)
        passed = inspection["media_passed"] and all(checks.get(k) is True for k in required) and not issues and score >= brief["minimum_score"] and min(dimensions.values()) >= 17
        return self.put(f"review:{jid}:{cid}", {"score": score, "passed": passed, "dimensions": dimensions, "penalty": penalty,
            "checks": checks, "issues": issues, "notes": notes, "game": game, "footage_type": footage_type, "language": language,
            "sha256": inspection["sha256"], "version": inspection["version"], "brief": brief["version"], "created": time.time()})

    def export(self, jid, cid):
        record = self.current_job(jid)
        review = self.get(f"review:{jid}:{cid}")
        if not review or not review["passed"] or review["brief"] != self.brief()["version"]:
            raise ValueError("This clip has not passed final GTA VI and quality review. Keep it as a draft.")
        _, clip, path = self.download_clip(jid, cid)
        if clip.get("status") != "done":
            raise ValueError("A pending or failed render cannot be exported as ready.")
        if clip.get("version") != review["version"] or self.digest(path) != review["sha256"]:
            raise ValueError("The render changed after review. Inspect and review the new version.")
        folder = self.data / "exports" / jid / cid
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, folder / "short.mp4")
        self.run(["ffmpeg", "-y", "-v", "error", "-ss", "0.5", "-i", str(path), "-frames:v", "1", str(folder / "cover.jpg")])
        text = str(clip.get("title", "")) + "\n\n" + str(clip.get("description", "")) + "\n\nSource: " + record["source_review"]["url"]
        text += "\n\n" + " ".join("#" + re.sub(r"[^A-Za-z0-9_]", "", str(tag)) for tag in clip.get("hashtags", []))
        (folder / "upload-text.txt").write_text(text, encoding="utf-8")
        (folder / "review.json").write_text(json.dumps({"review": review, "source_review": record["source_review"]}, indent=2), encoding="utf-8")
        return {"file": str(folder / "short.mp4"), "cover": str(folder / "cover.jpg"), "upload_text": str(folder / "upload-text.txt"),
                "score": review["score"], "niche": self.brief()["game"]}
