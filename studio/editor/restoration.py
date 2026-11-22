"""Put one accepted pacing cut back without replacing the current timeline."""
import copy
import hashlib

from . import model


class Conflict(ValueError):
    """The saved cut no longer has an unambiguous place in the current sequence."""


def key(pass_id, cut_id):
    return pass_id + ":" + cut_id


def fragment(original, cut, pass_id):
    start, end = cut["start_frame"], cut["end_frame"]
    parents = [i for i in original["items"] if i["kind"] == "video" and
               i["at_frame"] <= start < end <= i["at_frame"] + i["frames"]]
    if len(parents) != 1:
        raise Conflict("This cut does not belong to one saved source section. Restore a revision from History instead.")
    parent = parents[0]
    video = copy.deepcopy(parent)
    video.update(id=hashlib.sha256((key(pass_id, cut["id"]) + ":restored").encode()).hexdigest()[:32],
                 at_frame=start, frames=end - start, source_in=parent["source_in"] + start - parent["at_frame"])
    cues = [copy.deepcopy(i) for i in original["items"] if i.get("parent_id") == parent["id"] and
            start <= i["at_frame"] < i["at_frame"] + i["frames"] <= end]
    for cue in cues:
        cue["parent_id"] = video["id"]
    return video, cues


def restore(document, original, cuts, cut_id, pass_id):
    token = key(pass_id, cut_id)
    restored = document.get("restored_cuts", [])
    if token in restored:
        raise Conflict("This cut was already restored in this revision. Use History to undo a later removal.")
    cut = next((c for c in cuts if c["id"] == cut_id), None)
    if cut is None:
        raise Conflict("Choose a cut that was applied to this project.")
    ordered = sorted(cuts, key=lambda c: c["start_frame"])
    baseline = model.apply(original, [{"type": "sequence.extract", "start_frame": c["start_frame"],
        "end_frame": c["end_frame"], "cut_id": c["id"]} for c in reversed(ordered)])

    def position(candidate):
        return candidate["start_frame"] - sum(c["end_frame"] - c["start_frame"] for c in cuts
                                              if c["end_frame"] <= candidate["start_frame"])

    point = position(cut)
    pictures = sorted((i for i in baseline["items"] if i["kind"] == "video"), key=lambda i: i["at_frame"])
    left = next((i for i in reversed(pictures) if i["at_frame"] + i["frames"] <= point), None)
    right = next((i for i in pictures if i["at_frame"] >= point), None)
    left_gap = point - left["at_frame"] - left["frames"] if left else 0
    right_gap = right["at_frame"] - point if right else 0
    # Adjacent removals share a boundary. Previously restored pieces determine their original order.
    for other in ordered:
        if key(pass_id, other["id"]) not in restored or position(other) != point:
            continue
        if other["end_frame"] <= cut["start_frame"]:
            left, _ = fragment(original, other, pass_id)
            left_gap = 0
        elif other["start_frame"] >= cut["end_frame"]:
            right, _ = fragment(original, other, pass_id)
            right_gap = 0
            break

    current = {i["id"]: i for i in document["items"]}

    def anchor(expected, side):
        if expected is None:
            return None
        item = current.get(expected["id"])
        if item is None or item["kind"] != "video" or item["track_id"] != expected["track_id"]:
            raise Conflict("A picture section beside this cut was removed or split. Restore a revision from History instead.")
        edge = item["source_in"] + (item["frames"] if side == "left" else 0)
        saved_edge = expected["source_in"] + (expected["frames"] if side == "left" else 0)
        if edge != saved_edge:
            raise Conflict("The source edge beside this cut has changed. Restore a revision from History instead.")
        return item["at_frame"] + item["frames"] + left_gap if side == "left" else item["at_frame"] - right_gap

    before, after = anchor(left, "left"), anchor(right, "right")
    if before is None and after is None or before is not None and after is not None and before != after:
        raise Conflict("The picture sections beside this cut no longer meet. Restore a revision from History instead.")
    at = before if before is not None else after
    if at < 0:
        raise Conflict("There is no room at the saved cut boundary. Restore a revision from History instead.")
    video, cues = fragment(original, cut, pass_id)
    inserted = [video, *cues]
    if any(i["id"] in current for i in inserted):
        raise Conflict("Some content from this cut is already in the sequence. Restore a revision from History instead.")
    tracks = {t["id"]: t for t in document["tracks"]}
    affected = inserted + [i for i in document["items"] if i["at_frame"] + i["frames"] > at]
    if any(i["track_id"] not in tracks or tracks[i["track_id"]]["locked"] for i in affected):
        raise Conflict("Unlock the affected picture, caption and title tracks before restoring this cut.")
    result = copy.deepcopy(document)
    length = cut["end_frame"] - cut["start_frame"]
    for item in result["items"]:
        start, end = item["at_frame"], item["at_frame"] + item["frames"]
        if start >= at:
            item["at_frame"] = model.integer(start + length)
        elif end > at:
            if item["kind"] != "title":
                raise Conflict("A picture or caption now crosses this cut boundary. Move it away before restoring the cut.")
            item["frames"] = model.integer(item["frames"] + length, 1)
    for item in inserted:
        item["at_frame"] = model.integer(at + item["at_frame"] - cut["start_frame"])
    result["items"].extend(inserted)
    result["protected"] = list(dict.fromkeys(result["protected"] + [i["id"] for i in inserted]))
    result["restored_cuts"] = [*restored, token]
    model.validate_placement(result)
    return result
