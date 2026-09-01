# Lawrenceville Motors — Reel Renderer

Small always-on web service: given a vehicle's photos + data, renders a
9:16 Ken Burns video with TTS voiceover + music bed, uploads it to Supabase
Storage (`vehicle-reels` bucket), and returns the public URL. Make.com calls
this from the existing vehicle-marketing scenario.

## Deploy (Render, free tier)

1. Push this folder to a GitHub repo (web upload works fine, no git CLI needed).
2. On Render: New → Web Service → connect that repo → Environment: Docker → Free plan.
3. Set these environment variables in Render's dashboard (Settings → Environment):
   - `SUPABASE_URL` = `https://njglmzvdrqakvzzoypib.supabase.co`
   - `SUPABASE_SERVICE_ROLE_KEY` = (from Supabase dashboard → Settings → API → service_role key — paste directly here, never share this key elsewhere)
   - `RENDER_API_KEY` = a shared secret you pick (Make.com must send this same value in the `X-API-Key` header on every request)
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
