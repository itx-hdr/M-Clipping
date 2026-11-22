"""Recoverable transcript pacing passes and revision-bound cut proposals."""
import hashlib
import json
import time

import sqlalchemy as sa
from fastapi import HTTPException

from .. import brain, ctx, db, jobs, safe
from . import exports, model, store

WINDOW = 5 * 60 * 30
MAX_WINDOWS = 24
passes = sa.Table(
    "editor_first_cuts", db.meta,
    sa.Column("id", sa.String(40), primary_key=True),
    sa.Column("project_id", sa.String(40), nullable=False, index=True),
    sa.Column("user_id", sa.String(40), nullable=False, index=True),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("document", sa.Text, nullable=False),
    sa.Column("instruction", sa.String(1000), nullable=False),
    sa.Column("digest", sa.String(64), nullable=False),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("windows", sa.Text, nullable=False),
    sa.Column("error", sa.String(500), nullable=False),
    sa.Column("created", sa.Float, nullable=False),
)


def public(row):
    windows = json.loads(row["windows"])
    return {**{key: row[key] for key in ("id", "revision", "instruction", "status", "error")},
            "windows": windows, "progress": round(100 * sum(w["status"] != "pending" for w in windows) / max(1, len(windows)))}


def get(connection, project_id, pass_id):
    store.get(connection, project_id)
    row = connection.execute(passes.select().where(passes.c.id == pass_id, passes.c.project_id == project_id,
                             passes.c.user_id == ctx.uid())).mappings().first()
    if row is None:
        raise HTTPException(404, "No such first-cut proposal.")
    return dict(row)


def active(connection, owner):
    return connection.execute(sa.select(passes.c.id).where(passes.c.user_id == owner,
                              passes.c.status.in_(("queued", "running")))).first() is not None


def phrases(document):
    """Group adjacent saved words. Missing captions never become removal candidates."""
    pictures = {i["id"]: i for i in document["items"] if i["kind"] == "video"}
    cues = sorted((i for i in document["items"] if i["kind"] == "caption" and i.get("parent_id") in pictures),
                  key=lambda i: (i["at_frame"], i["id"]))
    groups, group = [], []
    for cue in cues:
        if group and (cue["parent_id"] != group[-1]["parent_id"] or
                      cue["at_frame"] - (group[-1]["at_frame"] + group[-1]["frames"]) > 23 or
                      cue["at_frame"] + cue["frames"] - group[0]["at_frame"] > 360 or len(group) >= 32 or
                      group[-1]["text"].rstrip().endswith((".", "!", "?"))):
            groups.append(group)
            group = []
        group.append(cue)
    if group:
        groups.append(group)
    locked = {t["id"] for t in document["tracks"] if t["locked"]}
    protected = set(document["protected"])
    result = []
    for group in groups:
        parent = pictures[group[0]["parent_id"]]
        start, end = group[0]["at_frame"], max(c["at_frame"] + c["frames"] for c in group)
        if start < parent["at_frame"] or end > parent["at_frame"] + parent["frames"] or end - start > 900:
            continue
        text = " ".join(c["text"] for c in group).strip()
        if not text or len(text) > 2000:
            continue
        eligible = True
        for item in document["items"]:
            a, b = item["at_frame"], item["at_frame"] + item["frames"]
            if (b > start and item["track_id"] in locked or
                    a < end and b > start and item["id"] in protected or
                    item["kind"] == "caption" and (a < start < b or a < end < b)):
                eligible = False
                break
        ident = hashlib.sha256(":".join(c["id"] for c in group).encode()).hexdigest()[:32]
        result.append({"id": ident, "text": text, "start_frame": start, "end_frame": end,
                       "source_in": parent["source_in"] + start - parent["at_frame"],
                       "eligible": eligible})
    return result


def make_windows(document):
    duration = max((i["at_frame"] + i["frames"] for i in document["items"]), default=0)
    if not 0 < duration <= WINDOW * MAX_WINDOWS:
        raise ValueError("This pacing pass supports sequences up to two hours long.")
    evidence = phrases(document)
    windows = []
    for start in range(0, duration, WINDOW):
        end = min(start + WINDOW, duration)
        local = [p for p in evidence if start <= p["start_frame"] < end and p["end_frame"] <= end]
        context = [p["text"] for p in evidence if p["end_frame"] <= start][-2:]
        context += [p["text"] for p in evidence if p["start_frame"] >= end][:2]
        status, summary = "pending", ""
        if not any(p["eligible"] for p in local):
            status, summary = "retained", "No removable transcript evidence. This window stays as it is."
        elif len(local) > 120 or sum(len(p["text"]) for p in local) > 20000:
            status, summary = "retained", "Transcript exceeds this pass's review limit. This window stays as it is."
        windows.append({"start_frame": start, "end_frame": end, "status": status, "summary": summary,
                        "phrases": local, "context": context, "cuts": [], "error": ""})
    return windows


def submit(project_id, body):
    revision = model.integer(body.get("revision"))
    instruction, op_id = body.get("instruction", "Remove clear repetitions while keeping the full story."), body.get("op_id")
    if not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 1000:
        raise ValueError("Describe your pacing brief in one to 1,000 characters.")
    if not isinstance(op_id, str) or not 8 <= len(op_id) <= 80:
        raise ValueError("Give this request a unique ID.")
    pass_id = hashlib.sha256((ctx.uid() + ":" + project_id + ":" + op_id).encode()).hexdigest()[:32]
    digest = hashlib.sha256(store.encode(body).encode()).hexdigest()
    with exports.lock, db.engine.begin() as connection:
        project = store.get(connection, project_id)
        prior = connection.execute(passes.select().where(passes.c.id == pass_id)).mappings().first()
        if prior:
            if prior["digest"] != digest:
                raise HTTPException(409, "This request ID was used for a different pacing brief.")
            return public(prior)
        if project["revision"] != revision:
            raise HTTPException(409, "Reload the saved draft before planning a first cut.")
        if active(connection, ctx.uid()):
            raise HTTPException(429, "Wait for your current pacing pass or cancel it first.")
        windows = make_windows(json.loads(project["document"]))
        row = dict(id=pass_id, project_id=project_id, user_id=ctx.uid(), revision=revision,
                   document=project["document"], instruction=instruction.strip(), digest=digest,
                   status="queued", windows=store.encode(windows), error="", created=time.time())
        connection.execute(passes.insert().values(**row))
    jobs.submit_heavy(pass_id, lambda: run(pass_id))
    ctx.bump()
    return public(row)


def operations(windows, selected=None):
    cuts = [c for w in windows for c in w["cuts"] if selected is None or c["id"] in selected]
    cuts.sort(key=lambda c: c["start_frame"], reverse=True)
    return [{"type": "sequence.extract", "start_frame": c["start_frame"], "end_frame": c["end_frame"],
             "cut_id": c["id"]} for c in cuts]


def validate_cuts(document, windows):
    ops = operations(windows)
    duration = max(i["at_frame"] + i["frames"] for i in document["items"])
    if len(ops) > 60 or sum(o["end_frame"] - o["start_frame"] for o in ops) > duration // 4:
        raise ValueError("The proposed removals exceed the conservative limit of 60 cuts or 25% of the sequence.")
    if any(b["end_frame"] > a["start_frame"] for a, b in zip(ops, ops[1:])):
        raise ValueError("The proposed cuts overlap.")
    return model.apply(document, ops) if ops else document


def run(pass_id):
    with db.engine.begin() as connection:
        row = connection.execute(passes.select().where(passes.c.id == pass_id, passes.c.user_id == ctx.uid())).mappings().first()
        if row is None or row["status"] != "queued":
            return
        if connection.execute(passes.update().where(passes.c.id == pass_id, passes.c.status == "queued")
                              .values(status="running")).rowcount != 1:
            return
        row = dict(row)
    ctx.bump()
    windows, document = json.loads(row["windows"]), json.loads(row["document"])
    for window in windows:
        if window["status"] != "pending":
            continue
        with db.engine.connect() as connection:
            current = connection.execute(sa.select(passes.c.status).where(passes.c.id == pass_id)).scalar()
        if current != "running":
            return
        try:
            answer = brain.editor_pacing_suggestions(window["phrases"], window["context"], row["instruction"])
            if (not isinstance(answer, dict) or not isinstance(answer.get("summary"), str) or
                    not isinstance(answer.get("cuts"), list) or len(answer["cuts"]) > 5):
                raise ValueError("The AI returned an invalid pacing proposal.")
            available = {p["id"]: p for p in window["phrases"] if p["eligible"]}
            cuts, seen = [], set()
            for proposal in answer["cuts"]:
                ident = proposal.get("phrase_id") if isinstance(proposal, dict) else None
                if (not isinstance(ident, str) or ident not in available or ident in seen or
                        not isinstance(proposal.get("reason"), str) or not proposal["reason"].strip()):
                    raise ValueError("The AI proposed a cut without valid, removable transcript evidence.")
                seen.add(ident)
                cuts.append({**available[ident], "reason": brain._text(proposal["reason"], 500)})
            window["cuts"] = cuts
            validate_cuts(document, windows)
            window.update(status="reviewed", summary=brain._text(answer["summary"], 1000), error="")
        except Exception as error:
            window.update(status="failed", cuts=[], summary="This window stays as it is.", error=safe.friendly(error, 400))
        with db.engine.begin() as connection:
            updated = connection.execute(passes.update().where(passes.c.id == pass_id, passes.c.status == "running")
                                         .values(windows=store.encode(windows)))
            if updated.rowcount != 1:
                return
        ctx.bump()
    failed = any(w["status"] == "failed" for w in windows)
    with db.engine.begin() as connection:
        connection.execute(passes.update().where(passes.c.id == pass_id, passes.c.status == "running")
                           .values(status="partial" if failed else "ready", error="", windows=store.encode(windows)))
    ctx.bump()


def control(project_id, pass_id, decision):
    with exports.lock, db.engine.begin() as connection:
        row = get(connection, project_id, pass_id)
        if decision == "cancel":
            connection.execute(passes.update().where(passes.c.id == pass_id, passes.c.status.in_(("queued", "running")))
                               .values(status="cancelled"))
        elif decision == "dismiss":
            connection.execute(passes.update().where(passes.c.id == pass_id, passes.c.status.in_(("ready", "partial", "failed")))
                               .values(status="dismissed"))
        elif decision == "retry":
            if row["status"] not in ("partial", "failed"):
                raise HTTPException(409, "Only an interrupted or partial pacing pass can be retried.")
            if store.get(connection, project_id)["revision"] != row["revision"]:
                raise HTTPException(409, "The draft changed. Start a pacing pass for the new revision.")
            if active(connection, ctx.uid()):
                raise HTTPException(429, "Wait for your current pacing pass or cancel it first.")
            windows = json.loads(row["windows"])
            for window in windows:
                if window["status"] == "failed":
                    window.update(status="pending", error="", summary="", cuts=[])
            connection.execute(passes.update().where(passes.c.id == pass_id)
                               .values(status="queued", error="", windows=store.encode(windows)))
        else:
            raise ValueError("Choose cancel, dismiss or retry.")
        result = public(get(connection, project_id, pass_id))
    if decision == "retry":
        jobs.submit_heavy(pass_id, lambda: run(pass_id))
    ctx.bump()
    return result


def accept(connection, project_id, base, document, body):
    """Called inside the revision transaction; the browser never supplies executable AI edits."""
    if not isinstance(body["first_cut_id"], str):
        raise ValueError("Choose an existing pacing proposal.")
    row = get(connection, project_id, body["first_cut_id"])
    selected = body.get("cut_ids")
    windows = json.loads(row["windows"])
    available = {c["id"] for w in windows for c in w["cuts"]}
    if (row["status"] not in ("ready", "partial") or row["revision"] != base or
            any(key in body for key in ("ops", "suggestion_id", "restore_revision", "restore_item_id", "restore_cut"))):
        raise HTTPException(409, "This pacing proposal is no longer available for the saved revision.")
    if (not isinstance(selected, list) or not selected or len(selected) > 60 or
            any(not isinstance(i, str) or i not in available for i in selected) or len(set(selected)) != len(selected)):
        raise ValueError("Select existing cuts from this pacing proposal.")
    result = model.apply(document, operations(windows, set(selected)))
    for window in windows:
        for cut in window["cuts"]:
            cut["applied"] = cut["id"] in selected
    changed = connection.execute(passes.update().where(passes.c.id == row["id"], passes.c.status == row["status"])
                                 .values(status="applied", windows=store.encode(windows)))
    if changed.rowcount != 1:
        raise HTTPException(409, "This pacing proposal changed in another window.")
    return result


def restore(connection, project_id, document, body):
    from . import restoration
    target = body["restore_cut"]
    if (not isinstance(target, dict) or set(target) != {"pass_id", "cut_id"} or
            any(not isinstance(target[k], str) for k in target) or
            any(k in body for k in ("ops", "suggestion_id", "first_cut_id", "restore_revision", "restore_item_id"))):
        raise ValueError("Choose one saved pacing cut to restore.")
    row = get(connection, project_id, target["pass_id"])
    if row["status"] != "applied":
        raise HTTPException(409, "Only applied pacing cuts can be restored.")
    cuts = [c for w in json.loads(row["windows"]) for c in w["cuts"] if c.get("applied")]
    try:
        return restoration.restore(document, json.loads(row["document"]), cuts, target["cut_id"], row["id"])
    except restoration.Conflict as error:
        raise HTTPException(409, str(error)) from error


def recover():
    """Keep completed windows and require an explicit retry after an interrupted AI call."""
    with db.engine.begin() as connection:
        connection.execute(passes.update().where(passes.c.status == "running").values(status="failed",
            error="The server restarted during this pass. Retry to review the remaining windows."))
        waiting = list(connection.execute(passes.select().where(passes.c.status == "queued")
                                          .order_by(passes.c.created)).mappings())
    for row in waiting:
        with ctx.as_user(row["user_id"]):
            jobs.submit_heavy(row["id"], lambda pass_id=row["id"]: run(pass_id))
            ctx.bump()
