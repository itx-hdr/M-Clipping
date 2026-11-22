# M clipper MCP connection

This optional local connection lets Codex operate M clipper through its existing API. It keeps the clipping brief
in `NICHE.md` and review history in the ignored `data/mcp` folder. It supports the built-in local owner mode;
an installation with sign-in enabled needs an authenticated integration before it can use these tools.

## Install and register

Run these from the project folder after installing M clipper in `.venv`. The bridge uses the application's existing
Python environment and needs no extra MCP SDK installation. It implements the MCP stdio tools transport.

```powershell
$mclipperMcpPython = (Resolve-Path .venv\Scripts\python.exe).Path
$mclipperMcpEntry = (Resolve-Path run_mclipper_mcp.py).Path
codex mcp add mclipper -- $mclipperMcpPython $mclipperMcpEntry
codex mcp get mclipper
```

Restart the MCP connection in the desktop app's MCP settings after registration. The command is absolute, so
Codex can launch the bridge from another working directory. The default M clipper address is
`http://127.0.0.1:8765`; set `MCLIPPER_MCP_URL` for a different loopback port. Do not put an AI key in the MCP config.

## Workflow

1. `get_niche_brief`, `mclipper_status`, and, when needed, `start_mclipper`.
2. `search_sources` and `preview_source` return unverified candidates within the GTA VI search niche.
3. `sample_source` returns actual source frames at requested times. Review at least two separated samples and
   compare the footage with primary evidence. Samples, titles and transcripts do not independently prove authenticity.
4. `review_source` records the agent's assessment of a specific 5–180 second range. Only verified English GTA VI
   player gameplay or reactions to it qualify. Cinematics, trailer-only reactions, other games and uncertainty block queueing.
5. `queue_source` queues that exact reviewed range. `get_job` reports progress and errors.
6. `inspect_clip` returns the exact rendered file's opening, middle, payoff and ending frames, audio and local video
   path. Watch the full MP4 using the host's available media tools; still frames cannot establish motion or audio timing.
7. `render_clip` requests repairs within the approved source range, using M clipper's existing renderer. Every new
   version needs another inspection. `edit_clip_plan` adds up to eight chronological source sections and optional
   separate gameplay/reaction rectangles. Rectangles use x, y, width and height as 0–1 fractions of the source.
   The renderer preserves each rectangle's proportions and places captions in a separate band. Cuts cannot
   overlap, reorder events, escape the reviewed source range, or cross a known spoken word. Caption times are
   rebuilt for the retained sections; the original source times remain in the saved plan.
   Use `framing={}` for ordinary framing, or the two named rectangles for a custom composition. The agent still
   needs to select and visually check these areas; the tool does not automatically recognize faces or game HUD.
8. `review_clip` requires recorded checks for the niche, hook, context, payoff, ending, captions, framing, audio
   and full-video viewing. It calculates the score from five dimensions and penalties. A minimum of 85 overall,
   at least 17 in each dimension, a successful media check and no unresolved issues are required for export.
9. `export_clip` checks the current file's hash against the reviewed file. It writes the MP4, cover, posting text
   and source/review evidence. It does not publish the video.

Old jobs can be inspected, but cannot be exported through this workflow without a current niche source review.
The bridge governs its own tools; it does not restrict manual dashboard jobs or manual file copies.

These gates require honest source and editorial review by the connected agent. They cannot guarantee that footage
is authentic or that a short will go viral. Reviews and failed attempts remain in the history; changing a score
without new evidence is not a quality improvement.

## Verify

```powershell
.venv\Scripts\python.exe -m unittest tests.test_mcp_bridge -v
.venv\Scripts\python.exe tests\mcp_smoke.py
```

The tests use fake HTTP responses and a temporary database. They cover niche mismatch, uncertain sources,
missing evidence, source-range binding, stale briefs, missing audio review, weak dimensions and changed render
bytes. A separate MCP client smoke test should initialize the stdio process, list tools, and call
`get_niche_brief` and `mclipper_status` before using it for real jobs.

Source discovery and sampling use M clipper's existing yt-dlp environment and ffmpeg. An inaccessible video or a
timed-out sample remains unverified. The MCP bridge uses the normal M clipper queue and model fallback chain;
it is not a separate rendering service or an unattended scheduler.

Existing registrations using `run_peakcut_mcp.py`, `peakcut_status`, or `start_peakcut` continue to work through compatibility aliases. New registrations use the M clipper names above.
