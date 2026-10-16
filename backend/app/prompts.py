"""Prompts you copy into Codex.

Codex uses computer use in YOUR browser: it opens each link, lines up the
view, saves a screenshot under the exact file name given, and logs what the
address bar showed afterwards so maply can check the shot landed in the right
place. Nothing in this app talks to Codex or OpenAI directly.
"""

from __future__ import annotations

from app.config import get_settings


def screenshot_command(browser: str, path: str) -> str:
    return (
        f"b=$(osascript -e 'tell application \"{browser}\" to get bounds of front window'); "
        'IFS=", " read -r x1 y1 x2 y2 <<< "$b"; '
        f'screencapture -x -R"$x1,$y1,$((x2-x1)),$((y2-y1))" "{path}"'
    )


def build_prompt(*, job_name: str, slug: str, shots: list, label: str, log_name: str, total: int) -> str:
    """`shots` are objects/dicts with code, look, url, filename."""
    settings = get_settings()
    browser = settings.browser_app
    folder = f"{settings.host_jobs_dir}/{slug}"
    get = (lambda s, k: s[k]) if shots and isinstance(shots[0], dict) else getattr
    lines = []
    for s in shots:
        lines.append(f"{get(s, 'code')}: {get(s, 'look')}\n  open: {get(s, 'url')}\n  save as: images/{get(s, 'filename')}")
    shot_list = "\n\n".join(lines)
    example_path = f"{folder}/images/{get(shots[0], 'filename')}" if shots else f"{folder}/images/<save as>"

    return f"""Capture Google Street View photos for the maply job "{job_name}" ({label}: {len(shots)} of {total} shots).

Use computer use to operate my web browser ({browser}) on this Mac. This is an approved wheelchair-accessibility research task: photograph the footpaths and kerbs at exactly the places listed below. Work through the shots in order and don't skip any.

Job folder (create the subfolders if they are missing):
{folder}

For each shot:
1. Open the shot's link in {browser}: paste it into the address bar of one tab and press Enter. It opens Street View at the planned spot, already facing the planned direction.
2. Wait until the picture is sharp and fully loaded.
3. If Google shows a map, a "no imagery" message, an indoor photo or a user-uploaded photo sphere instead of street imagery, log the shot as no_imagery and move on.
4. Line up the shot. You should be looking at what the shot describes, facing its compass heading (within about 15°), with the kerb and footpath clearly in frame. Drag the view sideways to centre the footpath if needed, but don't zoom. If a vehicle or object completely hides the footpath, you may click once along the street to the nearest clear spot within about 10 m and face the same heading again. If the footpath still can't be seen, log the shot as blocked.
5. Close anything covering the picture (the side panel, popups, banners) without accepting anything or changing settings. Never sign in or solve a CAPTCHA. If one appears, stop and tell me.
6. Save a screenshot of the {browser} window to the exact "save as" path inside the job folder, as a PNG. On macOS this command captures just the browser window (shown for the first shot; use each shot's own path):
   {screenshot_command(browser, example_path)}
7. Add one line to results/{log_name} (create the file if needed). Each line is one JSON object:
   {{"shot": "P001-L1", "status": "captured", "file": "images/<save as>", "final_url": "<address bar URL after lining up>", "image_date": "<e.g. Mar 2023, or empty>", "notes": ""}}
   - status is captured, no_imagery or blocked.
   - final_url is the full URL from the address bar after you lined up the shot. It records where the camera really is and which way it faces, so always include it.
   - image_date is the "Image capture" month and year Street View shows, if you can see it.
   - notes: anything unusual, e.g. "moved 8 m to avoid a truck".

Shots (what to look at, the link to open, and the file to save):

{shot_list}

When you have worked through every shot, reply with how many shots were captured, had no imagery, or were blocked."""
