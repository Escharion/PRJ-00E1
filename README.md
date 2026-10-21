# PRJ-00E1

A lightweight Tkinter movie-night host with a browser-based guest theater. The Python app uses only the standard library; guest video files are stored separately in `~/.local/share/PRJ-00E1/media`.

## Start

Run from the project directory:

```sh
python3 watch.py
```

The desktop host shows the current guest URL, the seat key generated for this run, and the shared password: `hello_hello3_whatch_with me`. Open the displayed URL manually in a browser or share it with invited guests along with the current seat key and password. The server admits up to 11 guests and queues additional guests until a seat opens. Closing the host app stops the server.

The host terminal accepts `close theater <seconds>`, `restart theater`, `cancel movie`, `pause movie`, and `play movie`. Guests can upload a video, set its displayed title, chat, and control shared play/pause. Supported video extensions are MP4, WebM, OGG, MOV, and M4V; uploads are capped at 3 GB.

Uploading a movie starts shared playback automatically. The local Tkinter screen decodes and displays frames inside its own window; install `ffmpeg`, Pillow, and `ffplay` to enable desktop video and audio. Audio uses an audio-only process (`-nodisp`), not a separate video player. Browser previews start muted for autoplay compatibility and have an Unmute control.

## Cloud hosting on Render

The included [render.yaml](render.yaml) starts a headless cloud service without opening Tkinter. Deploy the repository as a Render Blueprint, then set `PRJ_ADMIN_PASSWORD` in Render to a unique secret of at least 16 characters. The host dashboard is available at `https://<your-service>.onrender.com/host`; guest links use the service root. The cloud dashboard keeps the host commands, upload controls, room log, and chat available in a browser. Local `python3 watch.py` still opens the desktop console.

The Blueprint uses a paid Starter web service and a 5 GB persistent disk mounted at `/var/data`; uploaded movies are saved under `/var/data/media`. Increase the disk size for your library, and note that Render bills for the service and disk. The service binds to Render's `PORT`. Guests and the host dashboard use HTTPS at Render's public URL.

Cloud hosting currently runs one service instance. The room's seats, chat, playback state, and rotating seat key are held in memory, so they reset when the service restarts; the persistent disk preserves uploaded videos only. The existing browser clients poll approximately every 1.5 seconds for chat and state updates. This is suitable for a small room, but is not a WebSocket implementation.

## Network and privacy

The displayed URL uses the computer's private LAN address. It works for devices on the same local network. To reach friends over the internet, the host must separately configure a trusted HTTPS tunnel or HTTPS reverse proxy; router forwarding alone does not make the private address public and would expose credentials and video over unencrypted HTTP. The app does not create or manage a public tunnel.

Render serves the cloud URL over HTTPS automatically. For local hosting, run a trusted HTTPS tunnel or reverse proxy that forwards to the app's local HTTP port, then launch with `PRJ_PUBLIC_URL=https://your-tunnel.example python3 watch.py` so the desktop displays the HTTPS address. Do not share the local `http://` link over the public internet.

The seat key changes each time the app starts. The built-in password is verified with PBKDF2-HMAC-SHA256; only its salted digest is stored in the source. Use this shared room only with people you trust. The offline browser screen is available after a guest has visited while the server is reachable; movie playback itself requires the host server.

## Tests

```sh
python3 -m unittest discover -s tests -v
```
