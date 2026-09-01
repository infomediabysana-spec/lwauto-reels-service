import asyncio
import os
import re
import time
import uuid

import edge_tts
import requests
from flask import Flask, request, jsonify

from render_reel import render_reel

app = Flask(__name__)

LISTING_BASE_URL = "https://www.lwautogroup.com/inventory"


def build_listing_url(vehicle):
    """Reconstructs the real, live per-vehicle page URL on lwautogroup.com
    from fields already present on every webhook payload (year/make/model/
    trim/id) — no separate lookup needed. Confirmed against the site's own
    generated URLs for all 6 vehicles live when this was built:
    https://www.lwautogroup.com/inventory/{year-make-model[-trim]}-{last 6
    hex chars of the vehicle's id}.html — e.g. the 2009 Mercedes-Benz E 320
    Bluetec (id ...56493e6c) is 2009-mercedes-benz-e-320-bluetec-493e6c.html.
    The site builds its slug straight from the make/model/trim text as
    stored, typos and all (an "Infinti" vehicle nets an "infinti" URL), so
    this reproduces it exactly, data-entry quirks included."""
    year = vehicle.get("year", "")
    make = vehicle.get("make", "")
    model = vehicle.get("model", "")
    trim = vehicle.get("trim", "")
    vehicle_id = vehicle.get("id") or vehicle.get("vehicle_id") or ""

    parts = [str(year), str(make), str(model)]
    if trim:
        parts.append(str(trim))
    base = "-".join(parts)
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    id_suffix = re.sub(r"[^0-9a-f]", "", str(vehicle_id).lower())[-6:]

    if not slug or not id_suffix:
        return None
    return f"{LISTING_BASE_URL}/{slug}-{id_suffix}.html"

API_KEY = os.environ.get("RENDER_API_KEY")  # shared secret Make.com must send
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
REELS_BUCKET = "vehicle-reels"


@app.after_request
def add_cors_headers(resp):
    # Allows testing/calling this from a browser (Render dashboard console,
    # a future admin page, etc.) in addition to Make.com's server-side calls.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route("/render", methods=["OPTIONS"])
def render_options():
    # CORS preflight for the JSON POST — browsers send this before the real
    # request because it has a Content-Type: application/json body.
    return ("", 204)


@app.get("/healthz")
def healthz():
    return {"ok": True}


def _require_api_key():
    if not API_KEY:
        return None  # no key configured yet — allow (dev/first-deploy convenience)
    sent = request.headers.get("X-API-Key")
    if sent != API_KEY:
        return jsonify({"ok": False, "error": "bad or missing X-API-Key"}), 401
    return None


def _upload_to_supabase(local_path, dest_name, content_type="video/mp4"):
    url = f"{SUPABASE_URL}/storage/v1/object/{REELS_BUCKET}/{dest_name}"
    with open(local_path, "rb") as f:
        resp = requests.post(
            url,
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": content_type,
                "x-upsert": "true",
            },
            data=f,
            timeout=60,
        )
    if not resp.ok:
        raise RuntimeError(f"Supabase upload failed ({resp.status_code}): {resp.text[:300]}")
    return f"{SUPABASE_URL}/storage/v1/object/public/{REELS_BUCKET}/{dest_name}"


def _mark_vehicle_rendered(vehicle_id, video_url):
    if not vehicle_id or not SUPABASE_SERVICE_ROLE_KEY:
        return
    url = f"{SUPABASE_URL}/rest/v1/vehicles?id=eq.{vehicle_id}"
    requests.patch(
        url,
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        json={"reel_url": video_url, "reel_status": "rendered"},
        timeout=20,
    )


@app.post("/tts-sample")
def tts_sample():
    """Quick way to preview a voice/line without rendering a full video —
    used to compare candidate edge-tts voices for realism before picking
    a new DEFAULT_VOICE. Uploads the sample mp3 to Supabase storage
    (under samples/) and returns its public URL to listen to directly."""
    auth_err = _require_api_key()
    if auth_err:
        return auth_err

    body = request.get_json(force=True, silent=True) or {}
    text = body.get("text") or "This is a sample voice for Lawrenceville Motors."
    voice = body.get("voice") or "en-US-GuyNeural"

    out_mp3 = f"/tmp/sample_{uuid.uuid4().hex}.mp3"
    try:
        asyncio.run(edge_tts.Communicate(text, voice).save(out_mp3))
        dest_name = f"sample-{voice}-{int(time.time())}.mp3"
        sample_url = _upload_to_supabase(out_mp3, dest_name, content_type="audio/mpeg")
        return jsonify({"ok": True, "voice": voice, "sample_url": sample_url})
    except Exception as e:
        app.logger.exception("tts-sample failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if os.path.exists(out_mp3):
            os.remove(out_mp3)


@app.post("/vehicle-url")
def vehicle_url():
    """Fast, no-render lookup used early in the Make scenario (right after
    the webhook fires) so Facebook/Instagram/GBP/YouTube captions can all
    include a real link to this exact vehicle's page — without waiting on
    the slow video render."""
    auth_err = _require_api_key()
    if auth_err:
        return auth_err

    vehicle = request.get_json(force=True, silent=True) or {}
    url = build_listing_url(vehicle)
    if not url:
        return jsonify({"ok": False, "error": "could not build listing_url — missing year/make/model or id"}), 400
    return jsonify({"ok": True, "listing_url": url})


@app.post("/render")
def render():
    auth_err = _require_api_key()
    if auth_err:
        return auth_err

    vehicle = request.get_json(force=True, silent=True) or {}
    required = ["year", "make", "model", "price", "mileage", "photos"]
    missing = [k for k in required if not vehicle.get(k)]
    if missing:
        return jsonify({"ok": False, "error": f"missing fields: {missing}"}), 400

    out_name = f"{vehicle.get('vehicle_id', uuid.uuid4().hex)}_{int(time.time())}.mp4"
    out_path = f"/tmp/{out_name}"

    try:
        script_text = render_reel(vehicle, out_path)
        video_url = _upload_to_supabase(out_path, out_name)
        _mark_vehicle_rendered(vehicle.get("vehicle_id"), video_url)
        return jsonify({
            "ok": True,
            "video_url": video_url,
            "script": script_text,
            "listing_url": build_listing_url(vehicle),
        })
    except Exception as e:
        app.logger.exception("render failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if os.path.exists(out_path):
            os.remove(out_path)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
