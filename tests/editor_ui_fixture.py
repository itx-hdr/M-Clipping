"""Disposable dashboard fixtures for manual editor checks; no AI keys or real media."""
import os
import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    with tempfile.TemporaryDirectory(prefix="mclipper-editor-ui-") as data:
        os.environ.update(MCLIPPER_DATA=data, DATABASE_URL="", MCLIPPER_ACCOUNTS="0",
            MCLIPPER_FETCH_MODEL="0", MCLIPPER_UPDATE_CHECK="0", MCLIPPER_HOST="127.0.0.1",
            MCLIPPER_PORT="18825", MCLIPPER_PUBLIC_URL="http://127.0.0.1:18825", MCLIPPER_AUTO_RELOAD="0",
            MCLIPPER_AI_KEY="", MCLIPPER_AI_KEY_FILE="", MCLIPPER_AI_MODELS="", MCLIPPER_SMTP_HOST="",
            GOOGLE_CLIENT_ID="", GOOGLE_CLIENT_SECRET="", GOOGLE_CLIENT_FILE="")
        from studio import auth, brain, ctx, db, server
        from studio.editor import exports, model, store
        if "--slow-replies" in sys.argv:
            @server.app.middleware("http")
            async def slow_replies(request, call_next):
                response = await call_next(request)
                # The save has committed before its reply arrives, like a slow connection.
                if request.url.path.endswith("/commands"):
                    await asyncio.sleep(8)
                return response
        brain.editor_pacing_suggestions = lambda *args: {"summary": "Disposable provider", "cuts": []}
        brain.editor_text_suggestions = lambda *args: []
        db.init()
        try:
            with ctx.as_user(auth.local_user().id):
                for name in ("UI reliability A", "UI reliability B"):
                    doc = model.import_clip({"start": 0, "end": 10}, [
                        {"s": 1, "e": 2, "w": "Setup."}, {"s": 3, "e": 4, "w": "Again."},
                        {"s": 7, "e": 8, "w": "Payoff."}], 10)
                    doc["canvas"] = {"width": 320, "height": 180}
                    project = store.create(model.identifier(), name, doc, {})
                    directory = exports.folder(project["id"])
                    directory.mkdir(parents=True)
                    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "10", "-c:v", "libx264",
                        "-c:a", "aac", "-f", "mp4", str(directory / "source.media")], check=True)
            server.main()
        finally:
            db.engine.dispose()


if __name__ == "__main__":
    main()
