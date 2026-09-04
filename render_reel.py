"""
Lawrenceville Motors — vehicle reel renderer (production core)

Given vehicle data + real photo URLs, produces a finished 9:16 mp4:
Ken Burns pan/zoom across the photos, price/mileage/CTA text overlay,
a voiceover in Alex's own cloned voice (ElevenLabs) reading a script
written in the dealership's brand voice, mixed with a background music bed.

This is the same approach prototyped and tested in the dev sandbox,
adapted to run with real internet access (downloads real photos,
calls the real ElevenLabs/Anthropic services) instead of offline placeholders.
"""
import io
import os
import subprocess
import sys
import tempfile

import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

W, H = 720, 1280  # free-tier CPU is far too slow to encode 1080x1920 in time
FPS = 12  # fewer frames to render/encode; still smooth for a slow Ken Burns pan
SEC_PER_PHOTO = 3.2
# Cap how large a fetched photo is kept before Ken Burns cropping. Phone
# photos can be 3000-4000px wide; without this, every one of the ~100+
# per-frame crop/resize ops re-processes the full-size image, which is what
# blew past the free-tier request timeout during testing. Downsizing once,
# right after download, keeps each frame op cheap.
MAX_SRC_DIM = 1600
MUSIC_DUCK_DB = -22
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
MUSIC_BED_PATH = os.path.join(ASSETS_DIR, "music_bed.mp3")

# Voice: Alex's own ElevenLabs voice clone — same voice_id used by the daily
# educational-reel pipeline (lw-reel-render), so both video pipelines sound
# like the same person.
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
DEFAULT_VOICE = os.environ.get("ELEVENLABS_VOICE_ID", "dAVeTABuBwdSUlO93XJl")

# Script: Claude writes the narration in the dealership's brand voice
# (family owned, no tricks; plain and conversational, not salesy) instead of
# the old fill-in-the-blanks template, which read stiff and generic. Needs
# ANTHROPIC_API_KEY set in Render; if it's missing or the call fails, this
# falls back to the plain template below rather than breaking the render.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

SCRIPT_SYSTEM_PROMPT = """You write short voiceover scripts for Lawrenceville Motors, a family-owned used car dealership in Snellville, GA. Brand voice: family owned, no tricks — plain, conversational, honest. Never salesy, never flowery, no exclamation-point hype, no AI-sounding filler phrases like "look no further" or "you won't want to miss this."

Write ONE tight script (35-55 words, about 15-20 seconds spoken) for a 9:16 Facebook/Instagram Marketplace video for the specific vehicle described. Base it on what's actually distinctive about THIS vehicle (its real features/condition/description) rather than generic filler — lead with whatever's most likely to make a buyer stop scrolling. End with the price, mileage, and a low-key call to action (come see it / message us / call).

Output ONLY the exact words to be spoken aloud. No labels, no headers, no markdown, no stage directions, no quotation marks, no emoji — just the plain narration text, since it is fed directly to text-to-speech."""


def run(cmd):
    print("+", " ".join(cmd), file=sys.stderr)
    subprocess.run(cmd, check=True)


def money(n):
    return f"${n:,.0f}"


def build_script_text(v):
    """Plain template fallback — used only if Claude script generation is
    unavailable (no ANTHROPIC_API_KEY yet, or the API call failed)."""
    year, make, model = v["year"], v["make"], v["model"]
    price, mileage = v["price"], v["mileage"]
    feats = v.get("features") or []
    if isinstance(feats, str):
        feats = [feats]
    lines = [f"Check out this {year} {make} {model}, available now at Lawrenceville Motors."]
    if feats:
        lines.append("It's got " + ", ".join(feats[:3]) + ".")
    if v.get("description"):
        # Ad copy is already written in Alex's plain, no-flourish voice — reuse
        # it directly rather than paraphrasing, capped so the voiceover stays short.
        lines.append(v["description"][:220])
    lines.append(f"Just {mileage:,} miles, priced at {money(price)}.")
    lines.append(f"Come see it in {v.get('location', 'Snellville, GA')}, or message us to schedule a test drive.")
    return " ".join(lines)


def _vehicle_brief(v):
    year, make, model = v.get("year"), v.get("make"), v.get("model")
    trim = v.get("trim")
    feats = v.get("features") or []
    if isinstance(feats, str):
        feats = [feats]
    lines = [f"Vehicle: {year} {make} {model}" + (f" {trim}" if trim else "")]
    lines.append(f"Price: {money(v['price'])}")
    lines.append(f"Mileage: {v['mileage']:,} miles")
    if feats:
        lines.append("Features: " + ", ".join(feats))
    if v.get("description"):
        lines.append(f"Dealer's own notes on this car: {v['description']}")
    lines.append(f"Location: {v.get('location', 'Snellville, GA')}")
    if v.get("phone"):
        lines.append(f"Phone: {v['phone']}")
    return "\n".join(lines)


def generate_script(v):
    """Calls Claude to write a brand-voice narration script for this
    specific vehicle. Raises on any failure — caller decides the fallback."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 300,
            "system": SCRIPT_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": _vehicle_brief(v)}],
        },
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"Anthropic script generation failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
    text = text.strip().strip('"')
    if not text:
        raise RuntimeError("Anthropic returned an empty script")
    return text


def fetch_photo(url, timeout=20):
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    img = Image.open(io.BytesIO(resp.content)).convert("RGB")
    if max(img.size) > MAX_SRC_DIM:
        img.thumbnail((MAX_SRC_DIM, MAX_SRC_DIM), Image.LANCZOS)
    return img


def synthesize_voice(text, out_mp3, voice_id=None):
    """Calls ElevenLabs TTS with Alex's cloned voice and writes an mp3."""
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY not set")
    voice_id = voice_id or DEFAULT_VOICE
    resp = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        json={
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        },
        timeout=60,
    )
    if not resp.ok:
        raise RuntimeError(f"ElevenLabs TTS failed ({resp.status_code}): {resp.text[:300]}")
    with open(out_mp3, "wb") as f:
        f.write(resp.content)


def make_voiceover(text, out_wav, voice=None):
    tmp_mp3 = out_wav + ".src.mp3"
    synthesize_voice(text, tmp_mp3, voice_id=voice)
    # Normalize to wav for consistent ffprobe/ffmpeg handling downstream.
    run(["ffmpeg", "-y", "-i", tmp_mp3, out_wav])
    os.remove(tmp_mp3)


def make_music_bed(duration_s, out_wav):
    if os.path.exists(MUSIC_BED_PATH):
        run([
            "ffmpeg", "-y", "-stream_loop", "-1", "-i", MUSIC_BED_PATH,
            "-t", f"{duration_s}",
            "-af", f"afade=t=in:st=0:d=1.5,afade=t=out:st={max(duration_s - 2, 0)}:d=2",
            "-ac", "2", out_wav,
        ])
        return
    # Fallback placeholder pad if no licensed track has been dropped into
    # assets/music_bed.mp3 yet — swap that file in for real production use.
    run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=220:duration={duration_s}",
        "-f", "lavfi", "-i", f"sine=frequency=277:duration={duration_s}",
        "-filter_complex",
        "[0:a]volume=0.18[a0];[1:a]volume=0.12[a1];[a0][a1]amix=inputs=2:duration=longest,"
        "afade=t=in:st=0:d=1.5,afade=t=out:st={fade_start}:d=2".format(fade_start=max(duration_s - 2, 0)),
        "-ac", "2", out_wav,
    ])


def audio_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def _font(size, bold=False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size)


def _draw_stroked(d, xy, text, font, fill, stroke_fill="black", stroke_width=4, anchor="mm"):
    d.text(xy, text, font=font, fill=fill, anchor=anchor, stroke_width=stroke_width, stroke_fill=stroke_fill)


def build_overlay_layer(title, subtitle, cta):
    """Render the text overlay once onto a transparent layer. Re-drawing
    stroked text on every one of a few hundred frames was a meaningful chunk
    of render time; pasting one pre-rendered layer per frame is much cheaper."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    scale = H / 1920  # positions/font sizes were tuned for a 1080x1920 frame
    _draw_stroked(d, (W / 2, 150 * scale), title, _font(max(int(60 * scale), 14), bold=True), "white")
    _draw_stroked(d, (W / 2, H - 560 * scale), subtitle, _font(max(int(52 * scale), 12), bold=True), "#ffd23f")
    _draw_stroked(d, (W / 2, H - 460 * scale), cta, _font(max(int(38 * scale), 10)), "white", stroke_width=3)
    return layer


def draw_overlay(frame, overlay_layer):
    frame.paste(overlay_layer, (0, 0), overlay_layer)
    return frame


def kenburns_bg_frame(bg_base, frame_idx, total_frames, zoom_in):
    """Cover-crop + Ken Burns zoom/pan, applied to the blurred BACKGROUND
    layer only (never to the actual sharp vehicle photo). This layer exists
    purely to fill the 9:16 frame edge-to-edge behind the real photo, so
    cropping it is fine — nothing important is ever in it."""
    t = frame_idx / max(total_frames - 1, 1)
    z0, z1 = (1.0, 1.16) if zoom_in else (1.16, 1.0)
    zoom = z0 + (z1 - z0) * t

    src_w, src_h = bg_base.size
    target_ratio = W / H
    if src_w / src_h > target_ratio:
        base_h = src_h
        base_w = int(base_h * target_ratio)
    else:
        base_w = src_w
        base_h = int(base_w / target_ratio)

    crop_w = max(int(base_w / zoom), 2)
    crop_h = max(int(base_h / zoom), 2)
    cx, cy = src_w / 2, src_h / 2
    x0 = int(max(0, min(src_w - crop_w, cx - crop_w / 2)))
    y0 = int(max(0, min(src_h - crop_h, cy - crop_h / 2)))
    crop = bg_base.crop((x0, y0, x0 + crop_w, y0 + crop_h))
    return crop.resize((W, H), Image.LANCZOS)


def build_background_base(src):
    """Blurred, darkened, full-frame-covering backdrop that the Ken Burns
    pan/zoom runs on. Because it's blurred, cropping it to fill 9:16 loses
    nothing recognizable — it's just atmosphere behind the real photo."""
    bg = src.copy()
    bg = bg.filter(ImageFilter.GaussianBlur(radius=28))
    bg = ImageEnhance.Brightness(bg).enhance(0.55)
    return bg


def build_foreground(src):
    """The actual vehicle photo, shown in full — 'contain fit' scaled down
    to fit entirely inside the 9:16 frame with no cropping, then centered.
    This is what was missing before: the old code force-cropped every photo
    to fill the frame exactly, which chopped off the sides of the car and,
    combined with the Ken Burns zoom, made it look zoomed-in and blurry."""
    src_w, src_h = src.size
    scale = min(W / src_w, H / src_h)
    fg_w, fg_h = max(int(src_w * scale), 1), max(int(src_h * scale), 1)
    fg = src.resize((fg_w, fg_h), Image.LANCZOS)
    px, py = (W - fg_w) // 2, (H - fg_h) // 2
    return fg, px, py


def render_frames(photo_urls, frames_dir, fps, sec_per_photo, title, subtitle, cta):
    os.makedirs(frames_dir, exist_ok=True)
    overlay_layer = build_overlay_layer(title, subtitle, cta)
    idx = 0
    for i, url in enumerate(photo_urls):
        src = fetch_photo(url)
        # Background (blurred/zoomed) and foreground (sharp/static) are each
        # built once per photo, not once per frame — the per-frame cost is
        # just a crop+resize of the background plus two pastes.
        bg_base = build_background_base(src)
        fg, fg_x, fg_y = build_foreground(src)
        n_frames = int(sec_per_photo * fps)
        zoom_in = (i % 2 == 0)
        for f in range(n_frames):
            frame = kenburns_bg_frame(bg_base, f, n_frames, zoom_in)
            frame.paste(fg, (fg_x, fg_y))
            draw_overlay(frame, overlay_layer)
            frame.save(os.path.join(frames_dir, f"frame_{idx:06d}.jpg"), quality=92)
            idx += 1
    return idx


def encode_video(frames_dir, fps, out_mp4):
    run([
        "ffmpeg", "-y", "-framerate", str(fps),
        "-i", os.path.join(frames_dir, "frame_%06d.jpg"),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-pix_fmt", "yuv420p", out_mp4,
    ])


def mux_audio(video_only, voice_wav, music_wav, out_mp4):
    filt = (
        f"[1:a]volume=1.0[voice];"
        f"[2:a]volume={10 ** (MUSIC_DUCK_DB / 20):.4f}[music];"
        f"[voice][music]amix=inputs=2:duration=first:dropout_transition=1[aout]"
    )
    run([
        "ffmpeg", "-y",
        "-i", video_only, "-i", voice_wav, "-i", music_wav,
        "-filter_complex", filt,
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
        # Puts the moov atom at the front of the file instead of the end —
        # without this, browsers/Facebook's own ingestion have to fetch the
        # whole file before they can start playback (this is very likely why
        # the first test render sat spinning in Chrome's video preview).
        "-movflags", "+faststart",
        "-shortest", out_mp4,
    ])


def render_reel(vehicle, out_mp4):
    """vehicle: dict with year, make, model, price, mileage, features/description,
    phone, location, photos (list of URLs). Returns the voiceover script used."""
    workdir = tempfile.mkdtemp(prefix="reel_")
    frames_dir = os.path.join(workdir, "frames")
    voice_wav = os.path.join(workdir, "voice.wav")
    music_wav = os.path.join(workdir, "music.wav")
    video_only = os.path.join(workdir, "video_only.mp4")

    try:
        script_text = vehicle.get("script")
        if not script_text:
            try:
                script_text = generate_script(vehicle)
            except Exception as e:
                print(f"generate_script failed, using template fallback: {e}", file=sys.stderr)
                script_text = build_script_text(vehicle)
        make_voiceover(script_text, voice_wav, voice=vehicle.get("voice"))
        voice_len = audio_duration(voice_wav)

        photos = vehicle["photos"][:8]  # cap photo count so renders stay fast
        n_photos = max(len(photos), 1)
        sec_per_photo = max(SEC_PER_PHOTO, (voice_len + 1.0) / n_photos)

        title = f"{vehicle['year']} {vehicle['make']} {vehicle['model']}"
        subtitle = f"{money(vehicle['price'])} • {vehicle['mileage']:,} mi"
        cta = f"DM us or call {vehicle.get('phone', '')}"

        render_frames(photos, frames_dir, FPS, sec_per_photo, title, subtitle, cta)
        total_duration = n_photos * sec_per_photo
        make_music_bed(total_duration, music_wav)
        encode_video(frames_dir, FPS, video_only)
        mux_audio(video_only, voice_wav, music_wav, out_mp4)
        return script_text
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)
