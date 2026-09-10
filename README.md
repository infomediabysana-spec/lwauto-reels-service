# Lawrenceville Motors — Reel Renderer
_Redeploy trigger: force Render to pick up the edge-tts switch above (was stuck on an old build still calling ElevenLabs)._

Small always-on web service: given a vehicle's photos + data, renders a
9:16 Ken Burns video with a Claude-written, brand-voice script read by a
free Microsoft Edge neural voice (via the `edge-tts` package — no API key,
no account, no per-character cost), mixed with a music bed, and uploads it
to Supabase Storage (`vehicle-reels` bucket), returning the public URL.
Make.com calls this from the existing vehicle-marketing scenario.
(Previously used a cloned ElevenLabs voice shared with the daily educational
reel pipeline in `lw-reel-render`; switched to edge-tts after the ElevenLabs
account ran out of monthly credits.)

## Deploy (Render, free tier)

1. Push this folder to a GitHub repo (web upload works fine, no git CLI needed).
2. On Render: New → Web Service → connect that repo → Environment: Docker → Free plan.
3. Set these environment variables in Render's dashboard (Settings → Environment):
   - `SUPABASE_URL` = `https://njglmzvdrqakvzzoypib.supabase.co`
   - `SUPABASE_SERVICE_ROLE_KEY` = (from Supabase dashboard → Settings → API → service_role key — paste directly here, never share this key elsewhere)
   - `RENDER_API_KEY` = a shared secret you pick (Make.com must send this same value in the `X-API-Key` header on every request)
   - `EDGE_TTS_VOICE` = optional — defaults to `en-US-AndrewMultilingualNeural` (free Microsoft Edge neural voice), only set this to use a different edge-tts voice name
   - `ANTHROPIC_API_KEY` = an Anthropic API key (console.anthropic.com), used to write each vehicle's narration script. If unset, rendering still works but falls back to the old plain-template script.
4. Deploy. First request after idle takes ~30-60s (free tier cold start) — normal.

## API

`POST /render`
Header: `X-API-Key: <RENDER_API_KEY>`
Body:
```json
{
  "vehicle_id": "uuid-from-supabase",
  "year": 2015, "make": "BMW", "model": "428i", "trim": "Gran Coupe",
  "price": 9700, "mileage": 84000,
  "description": "ad copy text (optional, used for the voiceover script)",
  "features": ["clean title", "black on black"],
  "phone": "(770) 294-3036", "location": "Snellville, GA",
  "photos": ["https://.../car_x_1.jpg", "..."]
}
```
Response: `{"ok": true, "video_url": "https://.../vehicle-reels/xxxx.mp4", "script": "..."}`

## Optional: real background music

Drop a royalty-free/licensed track at `assets/music_bed.mp3` and redeploy —
it'll be looped/trimmed/faded automatically. Without it, a synthesized
placeholder pad is used instead (functional but not a real track).
