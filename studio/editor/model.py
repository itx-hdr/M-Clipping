"""Small, deterministic edits to a saved timeline."""
import copy
import hashlib
import math
import uuid


def identifier():
    return uuid.uuid4().hex


def frames(seconds):
    value = float(seconds)
    if not math.isfinite(value) or value < 0:
        raise ValueError("Choose a valid time inside the source.")
    return int(math.floor(value * 30 + .5))


def blank():
    return {"schema_version": 1, "fps": {"num": 30, "den": 1},
            "canvas": {"width": 1080, "height": 1920},
            "tracks": [{"id": "picture", "kind": "video", "locked": False},
                       {"id": "captions", "kind": "caption", "locked": False},
                       {"id": "titles", "kind": "title", "locked": False}],
            "items": [], "protected": []}


def import_clip(clip, words, source_seconds, offset=0):
    document = blank()
    sections = clip.get("segments") or [{"start": clip["start"], "end": clip["end"]}]
    cursor = 0
    for section in sections:
        start, end = frames(section["start"] - offset), frames(section["end"] - offset)
        if not 0 <= start < end <= frames(source_seconds):
            raise ValueError("This cut needs source footage that is no longer available.")
        item = {"id": identifier(), "track_id": "picture", "kind": "video", "at_frame": cursor,
                "frames": end - start, "source_in": start, "source_frames": frames(source_seconds),
                "layout": clip.get("layout", "blur"), "framing": copy.deepcopy(clip.get("framing")),
                "focus_x": clip.get("_focus_x")}
        document["items"].append(item)
        for word in words:
            if section["start"] <= word["s"] < word["e"] <= section["end"]:
                at = cursor + frames(word["s"] - section["start"])
                stop = min(cursor + end - start, cursor + frames(word["e"] - section["start"]))
                if stop > at:
                    document["items"].append({"id": identifier(), "track_id": "captions", "kind": "caption",
                                              "parent_id": item["id"], "at_frame": at, "frames": stop - at,
                                              "text": str(word["w"]), "style": clip.get("style", "clean")})
        cursor += end - start
    if clip.get("hook"):
        document["items"].append({"id": identifier(), "track_id": "titles", "kind": "title",
                                  "at_frame": 0, "frames": min(cursor, 54), "text": clip["hook"]})
    return document


def integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= 1000000000:
        raise ValueError("Use a whole frame number inside the allowed range.")
    return value


def restore_item(document, saved, item_id):
    """Restore one item and its attached cues while retaining unrelated edits."""
    original = next((i for i in saved["items"] if i["id"] == item_id), None)
    if original is None:
        raise ValueError("This item does not exist in the selected revision.")
    result = copy.deepcopy(document)
    restored = [original] + [i for i in saved["items"] if i.get("parent_id") == item_id]
    replaced = {item_id} | {i["id"] for i in result["items"] if i.get("parent_id") == item_id}
    existing = {i["id"]: i for i in result["items"] if i["id"] not in replaced}
    if any(i["id"] in existing for i in restored):
        raise ValueError("A linked caption has moved to another section. Restore the whole revision to recover that arrangement.")
    tracks = {t["id"]: t for t in result["tracks"]}
    affected = restored + [i for i in result["items"] if i["id"] in replaced]
    if any(i["track_id"] not in tracks or tracks[i["track_id"]]["locked"] for i in affected):
        raise ValueError("Unlock the affected tracks before restoring this item.")
    if original.get("parent_id") and original["parent_id"] not in existing:
        raise ValueError("Restore the parent picture section before its caption.")
    if original.get("parent_id"):
        parent = existing[original["parent_id"]]
        old_parent = next(i for i in saved["items"] if i["id"] == original["parent_id"])
        if any(parent.get(key) != old_parent.get(key) for key in ("at_frame", "source_in", "frames")):
            raise ValueError("The parent section's timing has changed. Restore that section before restoring this caption.")
    result["items"] = list(existing.values()) + copy.deepcopy(restored)
    result["protected"] = [i for i in result["protected"] if i not in replaced]
    result["protected"].extend(i["id"] for i in restored)
    validate_placement(result)
    return result


def validate_placement(document):
    pictures = sorted((i for i in document["items"] if i["kind"] == "video"), key=lambda i: i["at_frame"])
    if any(a["at_frame"] + a["frames"] > b["at_frame"] for a, b in zip(pictures, pictures[1:])):
        raise ValueError("These picture sections overlap. Move the section to an empty space.")


def split_item(document, item, children, operation):
    offset = integer(operation.get("offset_frames"), 1)
    if item["kind"] != "video" or offset >= item["frames"]:
        raise ValueError("Choose a split inside a picture section.")
    new_id = operation.get("new_id")
    if not isinstance(new_id, str) or not 8 <= len(new_id) <= 80:
        raise ValueError("Give the new section a unique ID.")
    if any(i["id"] == new_id for i in document["items"]):
        raise ValueError("That section ID is already in use.")
    cut = item["at_frame"] + offset
    if any(c["at_frame"] < cut < c["at_frame"] + c["frames"] for c in children):
        raise ValueError("The split crosses a caption. Choose a word boundary.")
    right = dict(item, id=new_id, at_frame=cut, source_in=item["source_in"] + offset,
                 frames=item["frames"] - offset)
    item["frames"] = offset
    document["items"].append(right)
    for cue in children:
        if cue["at_frame"] >= cut:
            cue["parent_id"] = new_id
    if item["id"] in document["protected"]:
        document["protected"].append(new_id)


def trim_item(document, item, children, operation):
    start, length = integer(operation.get("source_in")), integer(operation.get("frames"), 1)
    if item["kind"] != "video" or start + length > item["source_frames"]:
        raise ValueError("Keep the section inside its source footage.")
    delta = start - item["source_in"]
    end = item["at_frame"] + length
    for cue in children:
        cue_start = cue["at_frame"] - delta
        cue_end = cue_start + cue["frames"]
        if cue_start < item["at_frame"] < cue_end or cue_start < end < cue_end:
            raise ValueError("The trim crosses a caption. Choose a word boundary.")
        cue["at_frame"] = cue_start
    discarded = {c["id"] for c in children if c["at_frame"] < item["at_frame"] or c["at_frame"] >= end}
    document["items"] = [i for i in document["items"] if i["id"] not in discarded]
    document["protected"] = [i for i in document["protected"] if i not in discarded]
    item.update(source_in=start, frames=length)


def apply(document, operations):
    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
        raise ValueError("Send one to 100 edits together.")
    result = copy.deepcopy(document)
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("Each edit needs a type and a target.")
        kind = operation.get("type")
        if kind == "sequence.extract":
            result = extract(result, operation)
            continue
        if kind == "text.add":
            track_id = operation.get("track_id")
            track = next((t for t in result["tracks"] if t["id"] == track_id), None)
            if track is None or track["kind"] not in ("caption", "title") or track["locked"]:
                raise ValueError("Choose an unlocked title or caption track.")
            text, new_id = operation.get("text"), operation.get("new_id")
            if not isinstance(text, str) or not 1 <= len(text) <= 2000:
                raise ValueError("Enter one to 2,000 characters of text.")
            if not isinstance(new_id, str) or not 8 <= len(new_id) <= 80 or any(i["id"] == new_id for i in result["items"]):
                raise ValueError("Give this text item a unique ID.")
            result["items"].append({"id": new_id, "track_id": track_id, "kind": track["kind"], "text": text,
                                    "at_frame": integer(operation.get("at_frame")), "frames": integer(operation.get("frames"), 1),
                                    "style": "clean"})
            continue
        if kind == "track.lock":
            track = next((t for t in result["tracks"] if t["id"] == operation.get("track_id")), None)
            if track is None or type(operation.get("locked")) is not bool:
                raise ValueError("Choose a track and whether to lock it.")
            track["locked"] = operation["locked"]
            continue
        item = next((i for i in result["items"] if i["id"] == operation.get("item_id")), None)
        if item is None:
            raise ValueError("That timeline item no longer exists.")
        affected = [i for i in result["items"] if i.get("parent_id") == item["id"]] + [item]
        locked = {t["id"] for t in result["tracks"] if t["locked"]}
        if any(i["track_id"] in locked for i in affected):
            raise ValueError("Unlock the affected tracks before editing them.")
        if kind == "item.text":
            text = operation.get("text")
            if item["kind"] not in ("title", "caption") or not isinstance(text, str) or len(text) > 2000:
                raise ValueError("Choose a title or caption and enter up to 2,000 characters.")
            item["text"] = text
            if item["id"] not in result["protected"]:
                result["protected"].append(item["id"])
        elif kind == "item.move":
            delta = integer(operation.get("at_frame")) - item["at_frame"]
            for target in affected:
                target["at_frame"] = integer(target["at_frame"] + delta)
        elif kind == "item.remove":
            removed = {i["id"] for i in affected}
            result["items"] = [i for i in result["items"] if i["id"] not in removed]
            result["protected"] = [i for i in result["protected"] if i not in removed]
        elif kind == "item.split":
            split_item(result, item, affected[:-1], operation)
        elif kind == "item.trim":
            trim_item(result, item, affected[:-1], operation)
        elif kind == "item.protect":
            if type(operation.get("protected")) is not bool:
                raise ValueError("Choose whether to protect this item from AI changes.")
            result["protected"] = [i for i in result["protected"] if i != item["id"]]
            if operation["protected"]:
                result["protected"].append(item["id"])
        else:
            raise ValueError("This edit is not supported yet.")
    validate_placement(result)
    return result


def extract(document, operation):
    """Remove a timeline interval and close the gap, keeping source and caption links."""
    start = integer(operation.get("start_frame"))
    end = integer(operation.get("end_frame"), 1)
    cut_id = operation.get("cut_id")
    duration = max((i["at_frame"] + i["frames"] for i in document["items"]), default=0)
    if not start < end <= duration or end - start == duration:
        raise ValueError("Choose a cut inside the sequence and leave some footage to play.")
    if not isinstance(cut_id, str) or not 8 <= len(cut_id) <= 80:
        raise ValueError("Give this cut a unique ID.")
    locked = {t["id"] for t in document["tracks"] if t["locked"]}
    protected = set(document["protected"])
    for item in document["items"]:
        a, b = item["at_frame"], item["at_frame"] + item["frames"]
        if b > start and item["track_id"] in locked:
            raise ValueError("This cut would change a locked track. Unlock it first.")
        if a < end and b > start and item["id"] in protected:
            raise ValueError("This cut would remove protected content. Keep that section or allow AI changes first.")
        if item["kind"] == "caption" and (a < start < b or a < end < b):
            raise ValueError("The cut crosses a caption. Choose a word boundary.")
    result = copy.deepcopy(document)
    kept, splits = [], {}
    existing_ids = {i["id"] for i in document["items"]}
    for item in result["items"]:
        a, b = item["at_frame"], item["at_frame"] + item["frames"]
        if b <= start:
            kept.append(item)
        elif a >= end:
            item["at_frame"] -= end - start
            kept.append(item)
        elif a >= start and b <= end:
            continue
        elif item["kind"] == "video" and a < start and b > end:
            new_id = hashlib.sha256((item["id"] + ":" + cut_id).encode()).hexdigest()[:32]
            if new_id in existing_ids:
                raise ValueError("This cut ID has already been used in the sequence.")
            right = dict(item, id=new_id, at_frame=start, source_in=item["source_in"] + end - a, frames=b - end)
            item["frames"] = start - a
            kept.extend((item, right))
            splits[item["id"]] = new_id
        else:
            # A text layer spanning the cut stays continuous. Picture edges keep their source position.
            removed = min(b, end) - max(a, start)
            item["frames"] -= removed
            if a >= start:
                item["at_frame"] = start
                if item["kind"] == "video":
                    item["source_in"] += end - a
            kept.append(item)
    for item in kept:
        parent = item.get("parent_id")
        if parent in splits and item["at_frame"] >= start:
            item["parent_id"] = splits[parent]
    result["items"] = kept
    remaining = {i["id"] for i in kept}
    if any(i.get("parent_id") and i["parent_id"] not in remaining for i in kept):
        raise ValueError("This cut would separate a moved caption from its picture section.")
    result["protected"] = [i for i in result["protected"] if i in remaining]
    validate_placement(result)
    return result
