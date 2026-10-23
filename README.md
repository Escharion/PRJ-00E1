# PRJ-00E1

A lightweight Tkinter movie-night host with a browser-based guest theater. The Python app uses only the standard library; guest video files are stored separately in `~/.local/share/PRJ-00E1/media`.

## Start

Run from the project directory for devices on the same Wi-Fi/LAN:

```sh
python3 watch.py
```

The desktop host shows the local guest URL, a seat key generated for this run, and the shared guest password. Open the displayed URL manually in a browser or share it with invited guests along with the seat key and password. The server admits up to 11 guests and queues additional guests until a seat opens. Closing the host app stops the server.

## Host from your PC for friends on the internet

Install the official `cloudflared` program on the host PC and make sure the `cloudflared` command is available in your terminal:

- [Windows downloads](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
- [macOS and Linux installation](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)

Then start the desktop host with the public option:

```sh
python3 watch.py --public
```

On Windows, use `py watch.py --public` if `python3` is not the Python launcher. The app starts its local theater and opens a Cloudflare Quick Tunnel; when the secure public URL appears, the desktop guest link updates automatically. Share that HTTPS URL, the current seat key, and the guest password only with the friends you invite. No router port forwarding or public IP configuration is needed.

The public link is temporary and changes on each run. Keep the host app open and the PC awake and connected to the internet for the entire screening. Closing the app stops the tunnel and server. Without `--public`, the app stays local; this avoids exposing the theater to the internet unintentionally. If `cloudflared` is missing or cannot connect, the desktop terminal reports the problem and the local theater remains available.

Quick Tunnels are a convenience for small, temporary screenings, not a permanent hosting service. Internet traffic, including video, passes through Cloudflare. Large uploads may be subject to Cloudflare limits; selecting the movie from the host PC avoids uploading the video through the guest browser. The shared guest password is not an individual account credential, so do not post the URL, seat key, or password publicly.

The host terminal accepts `close theater <seconds>`, `restart theater`, `cancel movie`, `pause movie`, and `play movie`. Guests can upload a video, set its displayed title, chat, and control shared play/pause. Supported video extensions are MP4, WebM, OGG, MOV, and M4V; uploads are capped at 3 GB.

Uploading a movie starts shared playback automatically. The local Tkinter screen decodes and displays frames inside its own window; install `ffmpeg`, Pillow, and `ffplay` to enable desktop video and audio. Audio uses an audio-only process (`-nodisp`), not a separate video player. Browser previews start muted for autoplay compatibility and have an Unmute control.

## Cloud hosting on Render

The included [render.yaml](render.yaml) starts a headless cloud service without opening Tkinter. Deploy the repository as a Render Blueprint, then set `PRJ_ADMIN_PASSWORD` in Render to a unique secret of at least 16 characters. The host dashboard is available at `https://<your-service>.onrender.com/host`; guest links use the service root. The cloud dashboard keeps the host commands, upload controls, room log, and chat available in a browser. Local `python3 watch.py` still opens the desktop console.

The Blueprint uses a paid Starter web service and a 5 GB persistent disk mounted at `/var/data`; uploaded movies are saved under `/var/data/media`. Increase the disk size for your library, and note that Render bills for the service and disk. The service binds to Render's `PORT`. Guests and the host dashboard use HTTPS at Render's public URL.

Cloud hosting currently runs one service instance. The room's seats, chat, playback state, and rotating seat key are held in memory, so they reset when the service restarts; the persistent disk preserves uploaded videos only. The existing browser clients poll approximately every 1.5 seconds for chat and state updates. This is suitable for a small room, but is not a WebSocket implementation.

## Network and privacy

The normal displayed URL uses the computer's private LAN address and works only on the same local network. The `--public` option creates a temporary HTTPS tunnel through Cloudflare. Do not share the local `http://` link over the public internet.

Render serves the cloud URL over HTTPS automatically. For a permanent custom domain on your PC, configure a trusted HTTPS tunnel or reverse proxy to forward to the app's local HTTP port, then set `PRJ_PUBLIC_URL=https://your-domain.example` before starting `python3 watch.py`. For a temporary public URL, use `python3 watch.py --public` instead.

The seat key changes each time the app starts. The built-in password is verified with PBKDF2-HMAC-SHA256; only its salted digest is stored in the source. Use this shared room only with people you trust. The browser clients poll for shared chat and playback state about every 1.5 seconds; movie playback itself requires the host server.

## Tests

```sh
python3 -m unittest discover -s tests -v
```
