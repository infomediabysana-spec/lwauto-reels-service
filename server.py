import asyncio
import os
import time
import uuid

import edge_tts
import requests
from flask import Flask, request, jsonify

from render_reel import render_reel

app = Flask(__name__)

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
        return jsonify({"ok": True, "video_url": video_url, "script": script_text})
    except Exception as e:
        app.logger.exception("render failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if os.path.exists(out_path):
            os.remove(out_path)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
