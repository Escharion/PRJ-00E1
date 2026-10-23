"""Desktop host and lightweight browser server for PRJ-00E1."""

from __future__ import annotations

import json
import hmac
import mimetypes
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.cookies import CookieError, SimpleCookie
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO
from urllib.parse import unquote, urlparse


if TYPE_CHECKING:
	import tkinter as tk
	from tkinter import filedialog, messagebox, ttk
	from PIL import Image, ImageTk
	tk_widgets_unavailable = False
else:
	try:
		import tkinter as tk
		from tkinter import filedialog, messagebox, ttk
	except ModuleNotFoundError:
		tk = None
		filedialog = messagebox = ttk = None
		tk_widgets_unavailable = True
	else:
		tk_widgets_unavailable = False
	try:
		from PIL import Image, ImageTk
	except ModuleNotFoundError:
		Image = ImageTk = None

from backend.user import user_login


APP_NAME = "PRJ-00E1"
PORT = 8765
MAX_SEATS = 11
MAX_UPLOAD_BYTES = 3 * 1024 * 1024 * 1024
HOST_SESSION_SECONDS = 12 * 60 * 60
WEB_ROOT = Path(__file__).resolve().parent / "frontend"
ASSET_ROOT = Path(__file__).resolve().parent / "asset"
MEDIA_ROOT = Path(os.environ.get("PRJ_MEDIA_DIR", Path.home() / ".local" / "share" / APP_NAME / "media"))
ALLOWED_VIDEO_SUFFIXES = {".mp4", ".webm", ".ogg", ".mov", ".m4v"}
BACKGROUND_VIDEOS = {
	"7cbf3710848bf214460dd25f8964724a.mp4",
	"1094007582ad766e7a3bf6a90a5fa229.mp4",
}
CLOUDFLARE_URL_PATTERN = re.compile(
	r"https://[a-z0-9-]+\.trycloudflare\.com(?=[/: \t\r\n]|$)",
	re.IGNORECASE,
)


def current_time() -> str:
	"""Format local time for the connection log and chat."""
	return datetime.now().astimezone().strftime("%H:%M:%S")


def read_exactly(stream: BinaryIO, byte_count: int) -> bytes:
	"""Accumulate short pipe reads until a complete frame or end-of-stream."""
	buffer = bytearray()
	while len(buffer) < byte_count:
		chunk = stream.read(byte_count - len(buffer))
		if not chunk:
			break
		buffer.extend(chunk)
	return bytes(buffer)


def local_ip_address() -> str:
	"""Find the LAN address used for guest links without sending any traffic."""
	connection = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	try:
		connection.connect(("192.0.2.1", 80))
		return connection.getsockname()[0]
	except OSError:
		return "127.0.0.1"
	finally:
		connection.close()


class TheaterState:
	"""Protect shared room state so desktop and browser threads stay consistent."""

	def __init__(self, seat_key: str) -> None:
		self.lock = threading.RLock()
		self.seat_key = seat_key
		self.sessions: dict[str, dict[str, object]] = {}
		self.host_sessions: dict[str, float] = {}
		self.events: deque[dict[str, str]] = deque(maxlen=100)
		self.messages: deque[dict[str, str]] = deque(maxlen=100)
		self.movie_name = ""
		self.movie_path: Path | None = None
		self.playing = False
		self.position = 0.0
		self.position_updated = time.monotonic()
		self.closed = False
		self.close_deadline: float | None = None
		self.close_message = ""
		self.host_log("Theater server starting")

	def host_log(self, text: str) -> None:
		"""Append one timestamped line to the host-only terminal history."""
		self.events.append({"time": current_time(), "text": text})

	def active_count(self) -> int:
		"""Count admitted guests, never counting the host desktop as a seat."""
		return sum(bool(session["active"]) for session in self.sessions.values())

	def _promote_waiting(self) -> None:
		"""Admit the earliest waiting guests whenever seats become available."""
		waiting = sorted(
			((token, session) for token, session in self.sessions.items()
			 if not session["active"]),
			key=lambda item: float(item[1]["order"]),
		)
		for token, session in waiting:
			if self.active_count() >= MAX_SEATS:
				break
			session["active"] = True
			self.host_log(f"{session['name']} admitted from waiting room")

	def _expire_sessions(self) -> None:
		"""Release seats for browsers that stopped polling the server."""
		cutoff = time.monotonic() - 20
		for token in [
			token for token, session in self.sessions.items()
			if float(session["last_seen"]) < cutoff
		]:
			session = self.sessions.pop(token)
			self.host_log(f"{session['name']} disconnected")
		self._promote_waiting()

	def _close_theater(self) -> None:
		"""Stop playback and remove every guest when the countdown finishes."""
		self.position = 0.0
		self.playing = False
		self.movie_path = None
		self.movie_name = ""
		self.sessions.clear()
		self.closed = True
		self.close_deadline = None
		self.close_message = ""
		self.host_log("Theater closed; all guests signed out")

	def state_for(self, token: str | None = None) -> dict[str, object]:
		"""Return the public room snapshot and refresh this browser's heartbeat."""
		with self.lock:
			now = time.monotonic()
			self._expire_sessions()
			if self.close_deadline is not None and now >= self.close_deadline:
				self._close_theater()

			caller = self.sessions.get(token or "")
			if caller is not None:
				caller["last_seen"] = now
			guest_list = [
				{"username": str(session["name"]), "joined": str(session["joined"])}
				for session in self.sessions.values() if session["active"]
			]
			position = self.position
			if self.playing and not self.closed:
				position += now - self.position_updated
			countdown = (
				max(0, int(self.close_deadline - now + 0.999))
				if self.close_deadline is not None else None
			)
			return {
				"active": len(guest_list),
				"movie": self.movie_name,
				"guests": guest_list,
				"messages": list(self.messages),
				"events": list(self.events) if token is None else [],
				"playing": self.playing,
				"position": position,
				"closed": self.closed,
				"close_countdown": countdown,
				"close_message": self.close_message,
				"waiting": caller is not None and not bool(caller["active"]),
				"admitted": caller is not None and bool(caller["active"]),
			}

	def add_guest(self, username: str) -> tuple[str, bool]:
		"""Create a new guest session, admitting it or placing it in the queue."""
		with self.lock:
			self._expire_sessions()
			if self.closed:
				raise ValueError("The theater is closed. Please try again later.")
			if self.active_count() >= MAX_SEATS and len(self.sessions) >= 50:
				raise ValueError("The waiting room is full. Please try again later.")
			token = secrets.token_urlsafe(32)
			admitted = self.active_count() < MAX_SEATS
			self.sessions[token] = {
				"name": username.strip(),
				"joined": current_time(),
				"last_seen": time.monotonic(),
				"active": admitted,
				"order": time.monotonic(),
			}
			self.host_log(f"{username.strip()} joined" if admitted else f"{username.strip()} entered waiting room")
			return token, admitted

	def require_active(self, token: str) -> dict[str, object]:
		"""Refresh a session and reject writes from waiting or disconnected guests."""
		self._expire_sessions()
		session = self.sessions.get(token)
		if session is None or not session["active"] or self.closed:
			raise PermissionError("Join an available theater seat before continuing.")
		session["last_seen"] = time.monotonic()
		return session

	def create_host_session(self) -> str:
		"""Create a time-limited browser session for the cloud host console."""
		with self.lock:
			token = secrets.token_urlsafe(32)
			self.host_sessions[token] = time.monotonic()
			return token

	def require_host(self, token: str) -> None:
		"""Refresh a cloud host session or reject the request."""
		with self.lock:
			last_seen = self.host_sessions.get(token)
			if last_seen is None or time.monotonic() - last_seen > HOST_SESSION_SECONDS:
				self.host_sessions.pop(token, None)
				raise PermissionError("Host session expired. Sign in again.")
			self.host_sessions[token] = time.monotonic()

	def set_movie(self, movie_path: Path, movie_name: str) -> None:
		"""Publish a selected video and begin shared playback from the start."""
		with self.lock:
			self.position = 0.0
			self.position_updated = time.monotonic()
			self.playing = True
			self.movie_path = movie_path
			self.movie_name = movie_name[:120]
			self.closed = False
			self.close_deadline = None
			self.close_message = ""
			self.host_log(f"Now showing: {self.movie_name}")

	def control(self, action: str) -> None:
		"""Apply a shared playback, movie cancellation, or restart action."""
		with self.lock:
			now = time.monotonic()
			if action == "pause":
				if self.playing:
					self.position += now - self.position_updated
					self.playing = False
					self.host_log("Playback paused")
			elif action == "play" and self.movie_path is not None and not self.closed:
				self.position_updated = now
				self.playing = True
				self.host_log("Playback started")
			elif action == "cancel":
				self.position = 0.0
				self.playing = False
				self.movie_path = None
				self.movie_name = ""
				self.host_log("Movie canceled")
			elif action == "restart":
				self.sessions.clear()
				self.closed = False
				self.close_deadline = None
				self.close_message = ""
				self.host_log("Theater restarted; waiting for guests")
			else:
				raise ValueError("Unsupported theater action.")

	def close_after(self, seconds: int) -> None:
		"""Schedule a theater closure, then sign out all browser guests."""
		with self.lock:
			self.close_deadline = time.monotonic() + seconds
			self.close_message = f"Theater closing in {seconds} seconds"
			self.host_log(self.close_message)

	def send_message(self, username: str, text: str) -> None:
		"""Add a short plain-text message to the shared theater chat."""
		with self.lock:
			safe_text = " ".join(text.split())[:500]
			if safe_text:
				self.messages.append({"username": username, "time": current_time(), "text": safe_text})


class TheaterHandler(BaseHTTPRequestHandler):
	"""Serve the guest page and small JSON/video endpoints on the host PC."""

	state: TheaterState
	cloud_mode = False
	admin_password = ""

	def log_message(self, format: str, *args: object) -> None:
		"""Keep default HTTP request chatter out of the host terminal."""
		return

	def _send_json(
		self,
		data: dict[str, object],
		status: int = 200,
		cookie: tuple[str, str, int] | None = None,
	) -> None:
		"""Write a compact no-store JSON response."""
		payload = json.dumps(data, ensure_ascii=True).encode("utf-8")
		self.send_response(status)
		self.send_header("Content-Type", "application/json; charset=utf-8")
		self.send_header("Content-Length", str(len(payload)))
		self.send_header("Cache-Control", "no-store")
		self.send_header("Access-Control-Allow-Origin", "same-origin")
		if cookie is not None:
			name, value, max_age = cookie
			secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
			self.send_header(
				"Set-Cookie",
				f"{name}={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}{secure}",
			)
		self.end_headers()
		self.wfile.write(payload)

	def _cookie(self, name: str) -> str:
		"""Read a named browser cookie without accepting malformed cookie headers."""
		cookies = SimpleCookie()
		try:
			cookies.load(self.headers.get("Cookie", ""))
		except CookieError:
			return ""
		cookie = cookies.get(name)
		return cookie.value if cookie is not None else ""

	def _read_json(self) -> dict[str, object]:
		"""Parse a bounded JSON request body."""
		length = min(int(self.headers.get("Content-Length", "0")), 16_384)
		return json.loads(self.rfile.read(length).decode("utf-8"))

	def _token(self, body: dict[str, object] | None = None) -> str:
		"""Read a session token from a JSON body or browser request header."""
		return str((body or {}).get("token", self.headers.get("X-Session-Token", "")))

	def do_GET(self) -> None:
		"""Serve app assets, public room snapshots, or a byte-range video."""
		route = urlparse(self.path).path
		if route in ("/", "/index.html"):
			self._serve_file(WEB_ROOT / "index.html")
		elif route in ("/host", "/host.html"):
			if self.cloud_mode:
				self._serve_file(WEB_ROOT / "host.html")
			else:
				self.send_error(404)
		elif route == "/service-worker.js":
			self._serve_file(WEB_ROOT / "service-worker.js", "application/javascript; charset=utf-8")
		elif route.startswith("/assets/"):
			asset_name = Path(route).name
			if asset_name not in BACKGROUND_VIDEOS:
				self.send_error(404)
				return
			asset_path = ASSET_ROOT / asset_name
			if not asset_path.is_file():
				self.send_error(404)
				return
			self._serve_video(asset_path)
		elif route == "/api/state":
			token = self.headers.get("X-Session-Token", "")
			self._send_json(self.state.state_for(token))
		elif route == "/api/host/state":
			try:
				self.state.require_host(self.headers.get("X-Host-Token", ""))
				snapshot = self.state.state_for()
				snapshot["seat_key"] = self.state.seat_key
				self._send_json(snapshot)
			except PermissionError as error:
				self._send_json({"error": str(error)}, 401)
		elif route == "/host-media":
			try:
				if not self.cloud_mode:
					self.send_error(404)
					return
				self.state.require_host(self._cookie("PRJ_HOST_SESSION"))
				movie_path = self.state.movie_path
				if movie_path is None or not movie_path.is_file():
					self.send_error(404, "No movie is currently available")
					return
				self._serve_video(movie_path)
			except PermissionError as error:
				self.send_error(403, str(error))
		elif route == "/media":
			token = self.headers.get("X-Session-Token", "") or self._cookie("PRJ_SESSION")
			try:
				with self.state.lock:
					self.state.require_active(token)
					movie_path = self.state.movie_path
				if movie_path is None or not movie_path.is_file():
					self.send_error(404, "No movie is currently available")
					return
				self._serve_video(movie_path)
			except PermissionError as error:
				self.send_error(403, str(error))
		else:
			self.send_error(404)

	def do_POST(self) -> None:
		"""Handle login, chat, playback controls, and streaming file upload."""
		route = urlparse(self.path).path
		if route == "/api/login":
			self._login()
		elif route == "/api/host/login":
			self._host_login()
		elif route == "/api/host/control":
			self._host_control()
		elif route == "/api/host/command":
			self._host_command()
		elif route == "/api/host/chat":
			self._host_chat()
		elif route == "/api/chat":
			self._chat()
		elif route == "/api/control":
			self._control()
		elif route == "/api/upload":
			self._upload()
		elif route == "/api/leave":
			self._leave()
		else:
			self.send_error(404)

	def _login(self) -> None:
		"""Verify the seat credentials and return the guest's queue status."""
		try:
			body = self._read_json()
			username = str(body.get("username", "")).strip()
			if not user_login(username, str(body.get("seat_key", "")), str(body.get("password", "")), self.state.seat_key):
				self._send_json({"error": "Username, seat key, or password is incorrect."}, 401)
				return
			with self.state.lock:
				if self.state.closed:
					self._send_json({"error": "The theater is closed. Please try again later."}, 409)
					return
				token, admitted = self.state.add_guest(username)
				self._send_json(
					{"token": token, "admitted": admitted},
					cookie=("PRJ_SESSION", token, 12 * 60 * 60),
				)
		except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
			self._send_json({"error": str(error)}, 400)

	def _host_login(self) -> None:
		"""Authenticate the cloud host using its separately configured secret."""
		if not self.cloud_mode or not self.admin_password:
			self.send_error(404)
			return
		try:
			body = self._read_json()
			password = str(body.get("password", ""))
			if not hmac.compare_digest(password, self.admin_password):
				self._send_json({"error": "Host password is incorrect."}, 401)
				return
			token = self.state.create_host_session()
			self._send_json(
				{"token": token},
				cookie=("PRJ_HOST_SESSION", token, HOST_SESSION_SECONDS),
			)
		except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
			self._send_json({"error": str(error)}, 400)

	def _host_control(self) -> None:
		"""Apply playback, cancellation, or restart controls from the cloud host."""
		try:
			body = self._read_json()
			self.state.require_host(self.headers.get("X-Host-Token", ""))
			action = str(body.get("action", ""))
			if action not in {"play", "pause", "cancel", "restart"}:
				raise ValueError("Unsupported host control.")
			self.state.control(action)
			self._send_json({"ok": True})
		except (ValueError, PermissionError, json.JSONDecodeError) as error:
			self._send_json({"error": str(error)}, 403)

	def _host_command(self) -> None:
		"""Run a bounded host-terminal command from the cloud console."""
		try:
			body = self._read_json()
			self.state.require_host(self.headers.get("X-Host-Token", ""))
			command = str(body.get("command", "")).strip().lower().split()
			if command[:2] == ["close", "theater"] and len(command) == 3:
				seconds = int(command[2])
				if not 1 <= seconds <= 3600:
					raise ValueError("Countdown must be between 1 and 3600 seconds.")
				self.state.close_after(seconds)
			elif command == ["restart", "theater"]:
				self.state.control("restart")
			elif command == ["cancel", "movie"]:
				self.state.control("cancel")
			elif command == ["pause", "movie"]:
				self.state.control("pause")
			elif command == ["play", "movie"]:
				self.state.control("play")
			else:
				raise ValueError("Use: close theater <seconds>, restart theater, cancel movie, pause movie, play movie")
			self._send_json({"ok": True})
		except (ValueError, PermissionError, json.JSONDecodeError) as error:
			self._send_json({"error": str(error)}, 403)

	def _host_chat(self) -> None:
		"""Send a message from the authenticated cloud host console."""
		try:
			body = self._read_json()
			self.state.require_host(self.headers.get("X-Host-Token", ""))
			self.state.send_message("HOST", str(body.get("text", "")))
			self._send_json({"ok": True})
		except (ValueError, PermissionError, json.JSONDecodeError) as error:
			self._send_json({"error": str(error)}, 403)

	def _chat(self) -> None:
		"""Authenticate a guest chat message before broadcasting it."""
		try:
			body = self._read_json()
			with self.state.lock:
				session = self.state.require_active(self._token(body))
				self.state.send_message(str(session["name"]), str(body.get("text", "")))
			self._send_json({"ok": True})
		except (ValueError, PermissionError, json.JSONDecodeError) as error:
			self._send_json({"error": str(error)}, 403)

	def _control(self) -> None:
		"""Let any admitted guest control the room's shared playback."""
		try:
			body = self._read_json()
			with self.state.lock:
				self.state.require_active(self._token(body))
				action = str(body.get("action", ""))
				if action not in {"play", "pause", "cancel"}:
					raise PermissionError("That action is reserved for the theater host.")
				self.state.control(action)
			self._send_json({"ok": True})
		except (ValueError, PermissionError, json.JSONDecodeError) as error:
			self._send_json({"error": str(error)}, 403)

	def _upload(self) -> None:
		"""Stream a browser-selected video into the app's separate media folder."""
		temporary_path: Path | None = None
		try:
			host_token = self.headers.get("X-Host-Token", "")
			if host_token:
				self.state.require_host(host_token)
				username = "HOST"
			else:
				token = self.headers.get("X-Session-Token", "")
				with self.state.lock:
					session = self.state.require_active(token)
					username = str(session["name"])
			filename = Path(unquote(self.headers.get("X-Filename", "movie.mp4"))).name
			suffix = Path(filename).suffix.lower()
			length = int(self.headers.get("Content-Length", "0"))
			if suffix not in ALLOWED_VIDEO_SUFFIXES:
				raise ValueError("Choose an MP4, WebM, OGG, MOV, or M4V video.")
			if length <= 0 or length > MAX_UPLOAD_BYTES:
				raise ValueError("The video must be smaller than 3 GB.")

			MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
			temporary_path = MEDIA_ROOT / f"upload-{secrets.token_hex(8)}.part"
			with temporary_path.open("wb") as movie_file:
				remaining = length
				while remaining:
					chunk = self.rfile.read(min(1024 * 1024, remaining))
					if not chunk:
						raise ValueError("The video upload ended unexpectedly.")
					movie_file.write(chunk)
					remaining -= len(chunk)
			requested_title = " ".join(unquote(self.headers.get("X-Movie-Title", "")).split())
			title = (requested_title or Path(filename).stem.strip())[:120] or "Untitled movie"
			final_path = MEDIA_ROOT / f"{secrets.token_hex(8)}{suffix}"
			temporary_path.replace(final_path)
			temporary_path = None
			self.state.set_movie(final_path, title)
			with self.state.lock:
				self.state.host_log(f"Video uploaded by {username}")
			self._send_json({"ok": True, "movie": title})
		except (OSError, ValueError, PermissionError) as error:
			if temporary_path is not None:
				temporary_path.unlink(missing_ok=True)
			self._send_json({"error": str(error)}, 400)

	def _leave(self) -> None:
		"""Sign out a guest and immediately offer its seat to the queue."""
		try:
			body = self._read_json()
			with self.state.lock:
				token = self._token(body)
				session = self.state.sessions.pop(token, None)
				if session is not None:
					self.state.host_log(f"{session['name']} left the theater")
				self.state._promote_waiting()
			self._send_json({"ok": True}, cookie=("PRJ_SESSION", "", 0))
		except (ValueError, json.JSONDecodeError):
			self._send_json({"error": "Invalid leave request."}, 400)

	def _serve_file(self, path: Path, content_type: str | None = None) -> None:
		"""Serve one static browser asset with a type and content length."""
		try:
			payload = path.read_bytes()
		except OSError:
			self.send_error(404)
			return
		self.send_response(200)
		self.send_header("Content-Type", content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
		self.send_header("Content-Length", str(len(payload)))
		self.send_header("Cache-Control", "no-cache")
		if path.name == "service-worker.js":
			self.send_header("Service-Worker-Allowed", "/")
		self.end_headers()
		self.wfile.write(payload)

	def _serve_video(self, path: Path) -> None:
		"""Support browser seeking using a single HTTP byte range."""
		size = path.stat().st_size
		start, end, status = 0, size - 1, 200
		requested_range = self.headers.get("Range", "")
		if requested_range.startswith("bytes="):
			try:
				first, last = requested_range[6:].split("-", 1)
				start = int(first) if first else max(0, size - int(last))
				end = min(int(last), size - 1) if last else size - 1
				if start > end or start >= size:
					raise ValueError
				status = 206
			except ValueError:
				self.send_error(416, "Requested range is not satisfiable")
				return

		self.send_response(status)
		self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "video/mp4")
		self.send_header("Accept-Ranges", "bytes")
		self.send_header("Content-Length", str(end - start + 1))
		if status == 206:
			self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
		self.end_headers()
		with path.open("rb") as movie_file:
			movie_file.seek(start)
			remaining = end - start + 1
			while remaining:
				chunk = movie_file.read(min(1024 * 1024, remaining))
				if not chunk:
					break
				try:
					self.wfile.write(chunk)
				except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
					return
				remaining -= len(chunk)


class TheaterServer(ThreadingHTTPServer):
	"""Allow concurrent browser polling and uploads without blocking the UI."""

	daemon_threads = True
	allow_reuse_address = True


class HostApp:
	"""Build the Tkinter host console and poll the shared theater snapshot."""

	def __init__(self, root: tk.Tk, state: TheaterState, guest_url: str) -> None:
		self.root = root
		self.state = state
		self.guest_url = guest_url
		self.local_guest_url = guest_url
		self.command_history: list[str] = []
		self.history_index = 0
		self.last_event_marker: tuple[str, str] | None = None
		self.last_message_marker: tuple[str, str, str] | None = None
		self.preview_process: subprocess.Popen[bytes] | None = None
		self.audio_process: subprocess.Popen[bytes] | None = None
		self.preview_movie_path: Path | None = None
		self.preview_was_playing = False
		self.preview_image: object | None = None
		self.preview_after_id: str | None = None
		self.preview_frames: queue.Queue[bytes] = queue.Queue(maxsize=2)
		self.preview_reader: threading.Thread | None = None
		self.preview_eof = threading.Event()
		self.public_link_updates: queue.Queue[tuple[str, str]] = queue.Queue()
		self.tunnel_process: subprocess.Popen[str] | None = None
		self.root.title(f"{APP_NAME} | Theater Host")
		self.root.geometry("1200x760")
		self.root.minsize(920, 620)
		self.root.configure(bg="#050a12")

		style = ttk.Style()
		style.theme_use("clam")
		style.configure("Host.TFrame", background="#080f1c")
		style.configure("Host.TLabel", background="#080f1c", foreground="#d6e9ff", font=("TkDefaultFont", 10))
		style.configure("Title.TLabel", background="#080f1c", foreground="#f4f9ff", font=("TkDefaultFont", 18, "bold"))
		style.configure("Accent.TButton", background="#087bdb", foreground="#ffffff", padding=(12, 8), font=("TkDefaultFont", 10, "bold"))
		style.map("Accent.TButton", background=[("active", "#159bff")])

		self.root.columnconfigure(0, weight=1)
		self.root.rowconfigure(1, weight=1)
		header = ttk.Frame(self.root, style="Host.TFrame", padding=16)
		header.grid(row=0, column=0, sticky="ew")
		ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(side="left")
		ttk.Label(header, text="HOST CONSOLE  /  ONE SHARED SCREEN", style="Host.TLabel").pack(side="left", padx=20)
		self.count_label = ttk.Label(header, text="0", style="Title.TLabel")
		self.count_label.pack(side="right", padx=8)
		ttk.Label(header, text="SEATS", style="Host.TLabel").pack(side="right")

		body = ttk.Frame(self.root, style="Host.TFrame", padding=(12, 0, 12, 12))
		body.grid(row=1, column=0, sticky="nsew")
		body.columnconfigure(0, weight=1, uniform="panel")
		body.columnconfigure(1, weight=2, uniform="panel")
		body.columnconfigure(2, weight=1, uniform="panel")
		body.rowconfigure(0, weight=1)
		self._build_terminal(body)
		self._build_stage(body)
		self._build_chat(body)
		self.root.after(500, self._refresh)
		self.root.protocol("WM_DELETE_WINDOW", self._shutdown)

	def _panel(self, parent: tk.Widget, title: str, column: int) -> tk.Frame:
		"""Create a restrained framed panel with a compact title."""
		panel = tk.Frame(parent, bg="#0c1625", highlightbackground="#17314b", highlightthickness=1)
		panel.grid(row=0, column=column, sticky="nsew", padx=5)
		panel.rowconfigure(1, weight=1)
		panel.columnconfigure(0, weight=1)
		tk.Label(panel, text=title, bg="#0c1625", fg="#84b9e8", font=("TkDefaultFont", 9, "bold"), padx=12, pady=12).grid(row=0, column=0, sticky="w")
		return panel

	def _build_terminal(self, parent: tk.Widget) -> None:
		"""Create the host-only, command-enabled terminal and connection log."""
		panel = self._panel(parent, "TERMINAL / CONNECTION LOG", 0)
		self.terminal = tk.Text(panel, bg="#060c14", fg="#b6d9f8", insertbackground="#38b6ff", relief="flat", wrap="word", font=("TkFixedFont", 9), padx=10, pady=8, state="disabled")
		self.terminal.grid(row=1, column=0, sticky="nsew", padx=8)
		self.terminal.bind("<Return>", self._run_command)
		self.terminal.bind("<Up>", self._history_up)
		self.terminal.bind("<Down>", self._history_down)
		self.terminal_input = tk.Entry(panel, bg="#101d2c", fg="#eff7ff", insertbackground="#38b6ff", relief="flat", font=("TkFixedFont", 10))
		self.terminal_input.grid(row=2, column=0, sticky="ew", padx=8, pady=8, ipady=7)
		self.terminal_input.insert(0, "type a host command...")
		self.terminal_input.bind("<FocusIn>", lambda _event: self._clear_placeholder())
		self.terminal_input.bind("<Return>", self._run_command)
		self.terminal_input.bind("<Up>", self._history_up)
		self.terminal_input.bind("<Down>", self._history_down)

	def _build_stage(self, parent: tk.Widget) -> None:
		"""Create the host's theater screen, room link, and movie controls."""
		panel = self._panel(parent, "THEATER SCREEN", 1)
		panel.columnconfigure(0, weight=1)
		panel.rowconfigure(1, weight=1)
		self.screen = tk.Frame(panel, bg="#03070d", highlightbackground="#128de0", highlightthickness=2)
		self.screen.grid(row=1, column=0, sticky="nsew", padx=14, pady=(6, 12))
		self.preview_label = tk.Label(self.screen, bg="#03070d")
		self.preview_label.place(relwidth=1, relheight=1)
		self.movie_label = tk.Label(self.screen, text="READY FOR THE FIRST SCREENING", bg="#03070d", fg="#8bcfff", font=("TkDefaultFont", 15, "bold"), wraplength=440)
		self.movie_label.place(relx=0.5, rely=0.5, anchor="center")
		self.title_entry = tk.Entry(panel, bg="#101d2c", fg="#eff7ff", insertbackground="#38b6ff", relief="flat", font=("TkDefaultFont", 10))
		self.title_entry.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 8), ipady=7)
		self.title_entry.insert(0, "Movie title")
		self.title_entry.bind("<FocusIn>", lambda _event: self._clear_title_placeholder())
		buttons = tk.Frame(panel, bg="#0c1625")
		buttons.grid(row=3, column=0, sticky="ew", padx=12)
		self._button(buttons, "UPLOAD MOVIE", self._choose_movie, True).pack(side="left", padx=2)
		self._button(buttons, "PLAY / PAUSE", self._toggle_playback).pack(side="left", padx=2)
		self._button(buttons, "CANCEL", lambda: self.state.control("cancel")).pack(side="left", padx=2)
		self.link_label = tk.Label(panel, text=f"GUEST LINK  {self.guest_url}", bg="#0c1625", fg="#7dc6ff", font=("TkFixedFont", 9), cursor="hand2", wraplength=520, justify="left")
		self.link_label.grid(row=4, column=0, sticky="ew", padx=14, pady=(14, 3))
		self.link_label.bind("<Button-1>", lambda _event: self._copy_text(self.guest_url))
		self.key_label = tk.Label(panel, text=f"SEAT KEY  {self.state.seat_key}    PASSWORD  hello_hello3_whatch_with me", bg="#0c1625", fg="#a8b9ce", font=("TkFixedFont", 9), wraplength=520, justify="left")
		self.key_label.grid(row=5, column=0, sticky="ew", padx=14, pady=(0, 5))
		self.live_label = tk.Label(panel, text="● LIVE STREAMING", bg="#0c1625", fg="#20df83", font=("TkDefaultFont", 11, "bold"))
		self.live_label.grid(row=6, column=0, pady=(4, 12))
		self.live_colors = ["#f04d67", "#4287ff", "#4be6a2"]
		self.live_color_index = 0
		self._pulse_live()

	def _build_chat(self, parent: tk.Widget) -> None:
		"""Create the host chat panel shared with admitted browser guests."""
		panel = self._panel(parent, "THEATER CHAT", 2)
		self.chat_view = tk.Text(panel, bg="#060c14", fg="#d6e9ff", relief="flat", wrap="word", font=("TkDefaultFont", 9), padx=8, pady=8, state="disabled")
		self.chat_view.grid(row=1, column=0, sticky="nsew", padx=8)
		self.chat_entry = tk.Entry(panel, bg="#101d2c", fg="#eff7ff", insertbackground="#38b6ff", relief="flat")
		self.chat_entry.grid(row=2, column=0, sticky="ew", padx=8, pady=8, ipady=7)
		self.chat_entry.insert(0, "Message guests...")
		self.chat_entry.bind("<FocusIn>", lambda _event: self._clear_chat_placeholder())
		self.chat_entry.bind("<Return>", self._send_host_message)

	def start_public_tunnel(self, port: int) -> None:
		"""Expose the local theater through an opt-in Cloudflare quick tunnel."""
		cloudflared = shutil.which("cloudflared")
		if cloudflared is None:
			self._append_terminal(
				"Public link unavailable: install cloudflared from "
				"https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
			)
			return
		try:
			process = subprocess.Popen(
				[
					cloudflared, "tunnel", "--url", f"http://127.0.0.1:{port}",
					"--no-autoupdate",
				],
				stdin=subprocess.DEVNULL,
				stdout=subprocess.PIPE,
				stderr=subprocess.STDOUT,
				text=True,
				encoding="utf-8",
				errors="replace",
				bufsize=1,
			)
		except OSError as error:
			self._append_terminal(f"Could not start Cloudflare Tunnel: {error}")
			return

		self.tunnel_process = process
		self.link_label.configure(text="PUBLIC GUEST LINK  Waiting for Cloudflare...")
		threading.Thread(
			target=self._read_tunnel_output,
			args=(process,),
			name="cloudflare-tunnel-output",
			daemon=True,
		).start()

	def _read_tunnel_output(self, process: subprocess.Popen[str]) -> None:
		"""Find the public URL while continuing to drain tunnel logs."""
		output_tail: deque[str] = deque(maxlen=6)
		public_url_found = False
		if process.stdout is not None:
			for line in process.stdout:
				public_url = cloudflare_url_from_line(line)
				if public_url and not public_url_found:
					self.public_link_updates.put(("url", public_url))
					public_url_found = True
				elif line.strip():
					output_tail.append(line.strip())

		process.poll()
		if not public_url_found:
			details = " ".join(output_tail)
			message = "Cloudflare Tunnel stopped before creating a public link."
			if details:
				message = f"{message} {details}"
			self.public_link_updates.put(("error", message))
		elif process.returncode is not None:
			self.public_link_updates.put(
				("error", "Cloudflare Tunnel stopped; the public link is no longer available.")
			)

	def _refresh_public_link(self) -> None:
		"""Apply tunnel status updates on the Tkinter UI thread."""
		while True:
			try:
				status, value = self.public_link_updates.get_nowait()
			except queue.Empty:
				return
			if status == "url":
				self.guest_url = f"{value}/"
				self.link_label.configure(text=f"GUEST LINK  {self.guest_url}")
				self._append_terminal(f"Public guest link ready: {self.guest_url}")
			else:
				self.guest_url = self.local_guest_url
				self.link_label.configure(text=f"LOCAL GUEST LINK  {self.guest_url}")
				self._append_terminal(value)

	def _button(self, parent: tk.Widget, text: str, command: object, accent: bool = False) -> tk.Button:
		"""Create a fixed-size host action button with consistent styling."""
		return tk.Button(parent, text=text, command=command, bg="#087bdb" if accent else "#15283b", fg="#ffffff", activebackground="#159bff", activeforeground="#ffffff", relief="flat", padx=10, pady=7, font=("TkDefaultFont", 9, "bold"), cursor="hand2")

	def _copy_text(self, text: str) -> None:
		"""Copy a guest link or seat key to the desktop clipboard."""
		self.root.clipboard_clear()
		self.root.clipboard_append(text)
		self._append_terminal(f"Copied: {text}")

	def _clear_placeholder(self) -> None:
		"""Remove the terminal prompt hint when the host starts typing."""
		if self.terminal_input.get() == "type a host command...":
			self.terminal_input.delete(0, "end")

	def _clear_title_placeholder(self) -> None:
		"""Remove the movie title hint before the host edits the name."""
		if self.title_entry.get() == "Movie title":
			self.title_entry.delete(0, "end")

	def _clear_chat_placeholder(self) -> None:
		"""Remove the chat hint when the host focuses the message field."""
		if self.chat_entry.get() == "Message guests...":
			self.chat_entry.delete(0, "end")

	def _pulse_live(self) -> None:
		"""Cycle the host's live-stream marker through red, blue, and green."""
		self.live_label.configure(fg=self.live_colors[self.live_color_index])
		self.live_color_index = (self.live_color_index + 1) % len(self.live_colors)
		self.root.after(350, self._pulse_live)

	def _append_terminal(self, text: str) -> None:
		"""Write a timestamped line into the host's interactive log."""
		self.terminal.configure(state="normal")
		self.terminal.insert("end", f"[{current_time()}] {text}\n")
		self.terminal.see("end")
		self.terminal.configure(state="disabled")

	def _run_command(self, _event: tk.Event | None = None) -> str:
		"""Run the documented theater commands from the terminal input."""
		command = self.terminal_input.get().strip()
		self.terminal_input.delete(0, "end")
		if not command or command == "type a host command...":
			return "break"
		self.command_history.append(command)
		self.history_index = len(self.command_history)
		self._append_terminal(f"> {command}")
		parts = command.lower().split()
		try:
			if parts[:2] == ["close", "theater"] and len(parts) == 3:
				seconds = int(parts[2])
				if not 1 <= seconds <= 3600:
					raise ValueError("Countdown must be between 1 and 3600 seconds.")
				self.state.close_after(seconds)
			elif parts == ["restart", "theater"]:
				self.state.control("restart")
			elif parts == ["cancel", "movie"]:
				self.state.control("cancel")
			elif parts == ["pause", "movie"]:
				self.state.control("pause")
			elif parts == ["play", "movie"]:
				self.state.control("play")
			else:
				self._append_terminal("Use: close theater <seconds>, restart theater, cancel movie, pause movie, play movie")
		except (ValueError, OSError) as error:
			self._append_terminal(str(error))
		return "break"

	def _history_up(self, _event: tk.Event) -> str:
		"""Recall the previous terminal command with the up arrow."""
		if self.command_history:
			self.history_index = max(0, self.history_index - 1)
			self.terminal_input.delete(0, "end")
			self.terminal_input.insert(0, self.command_history[self.history_index])
		return "break"

	def _history_down(self, _event: tk.Event) -> str:
		"""Move forward through terminal history with the down arrow."""
		if self.command_history:
			self.history_index = min(len(self.command_history), self.history_index + 1)
			self.terminal_input.delete(0, "end")
			if self.history_index < len(self.command_history):
				self.terminal_input.insert(0, self.command_history[self.history_index])
		return "break"

	def _choose_movie(self) -> None:
		"""Select a local movie and make it available to every browser guest."""
		selected = filedialog.askopenfilename(
			title="Choose a movie",
			filetypes=[("Video files", "*.mp4 *.webm *.ogg *.mov *.m4v"), ("All files", "*.*")],
		)
		if not selected:
			return
		movie_path = Path(selected)
		if movie_path.suffix.lower() not in ALLOWED_VIDEO_SUFFIXES:
			messagebox.showerror(APP_NAME, "Choose an MP4, WebM, OGG, MOV, or M4V video.")
			return
		title = self.title_entry.get().strip()
		if not title or title == "Movie title":
			title = movie_path.stem
		self.state.set_movie(movie_path, title)

	def _toggle_playback(self) -> None:
		"""Toggle shared playback from the host console."""
		if not self.state.playing and self.state.movie_path is None:
			self._append_terminal("Choose a movie before starting playback.")
			return
		self.state.control("pause" if self.state.playing else "play")

	def _send_host_message(self, _event: tk.Event | None = None) -> str:
		"""Publish a host-authored message into the guest chat feed."""
		text = self.chat_entry.get().strip()
		self.chat_entry.delete(0, "end")
		self.state.send_message("HOST", text)
		return "break"

	def _render_text(self, widget: tk.Text, lines: list[str]) -> None:
		"""Replace a read-only transcript without moving its view unnecessarily."""
		was_at_end = float(widget.yview()[1]) >= 0.98
		widget.configure(state="normal")
		widget.delete("1.0", "end")
		widget.insert("end", "\n".join(lines) + ("\n" if lines else ""))
		if was_at_end:
			widget.see("end")
		widget.configure(state="disabled")

	def _refresh(self) -> None:
		"""Refresh the host's active-seat counter, logs, chat, and movie title."""
		self._refresh_public_link()
		snapshot = self.state.state_for()
		self.count_label.configure(text=str(snapshot["active"]))
		screen_text = f"THEATER CLOSING IN {snapshot['close_countdown']}" if snapshot["close_countdown"] is not None else (
			f"PAUSED: {snapshot['movie']}" if snapshot["movie"] and not snapshot["playing"]
			else str(snapshot["movie"] or ("THEATER IS CLOSED" if snapshot["closed"] else "READY FOR THE FIRST SCREENING"))
		)
		self.movie_label.configure(text=screen_text)
		movie_path = self.state.movie_path
		should_preview = bool(snapshot["playing"] and movie_path is not None)
		if should_preview and (movie_path != self.preview_movie_path or not self.preview_was_playing):
			self._start_local_preview(movie_path, float(snapshot["position"]))
		elif not should_preview:
			self._stop_local_preview()
		if should_preview:
			self.movie_label.configure(text="" if self.preview_image is not None else screen_text)
		elif movie_path is None:
			self.preview_label.configure(image="")
			self.preview_label.image = None
			self.preview_image = None
		self.preview_was_playing = should_preview
		events = snapshot["events"]
		event_marker = (events[-1]["time"], events[-1]["text"]) if events else None
		if event_marker != self.last_event_marker:
			self._render_text(self.terminal, [f"[{item['time']}] {item['text']}" for item in events])
			self.last_event_marker = event_marker
		messages = snapshot["messages"]
		message_marker = (messages[-1]["time"], messages[-1]["username"], messages[-1]["text"]) if messages else None
		if message_marker != self.last_message_marker:
			self._render_text(self.chat_view, [f"[{item['time']}] {item['username']}: {item['text']}" for item in messages])
			self.last_message_marker = message_marker
		self.root.after(1000, self._refresh)

	def _start_local_preview(self, movie_path: Path, position: float) -> None:
		"""Decode movie frames into the Tkinter screen while shared playback runs."""
		self._stop_local_preview()
		decoder = shutil.which("ffmpeg")
		if decoder is None or Image is None or ImageTk is None:
			self._append_terminal("Install ffmpeg and Pillow to show movie frames inside the desktop screen.")
			self.preview_movie_path = movie_path
			return
		try:
			self.preview_process = subprocess.Popen(
				[
					decoder, "-nostdin", "-hide_banner", "-loglevel", "error", "-re", "-ss",
					str(max(0.0, position)), "-i", str(movie_path), "-an", "-sn", "-dn",
					"-vf", "fps=24,scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2:black",
					"-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
				],
				stdin=subprocess.DEVNULL,
				stdout=subprocess.PIPE,
				stderr=subprocess.DEVNULL,
				bufsize=0,
			)
			self.preview_movie_path = movie_path
			self.preview_frames = queue.Queue(maxsize=2)
			self.preview_eof.clear()
			self.preview_reader = threading.Thread(
				target=self._read_local_frames,
				args=(self.preview_process,),
				daemon=True,
			)
			self.preview_reader.start()
			self.preview_after_id = self.root.after(1, self._render_local_frame)
			self._start_local_audio(movie_path, position)
		except OSError as error:
			self.preview_process = None
			self.preview_movie_path = movie_path
			self._append_terminal(f"Could not decode the movie inside the desktop screen: {error}")

	def _start_local_audio(self, movie_path: Path, position: float) -> None:
		"""Play the movie's audio track without opening a separate video window."""
		player = shutil.which("ffplay")
		if player is None:
			self._append_terminal("Install ffplay to hear the movie audio on this desktop.")
			return
		try:
			self.audio_process = subprocess.Popen(
				[
					player, "-nodisp", "-autoexit", "-hide_banner", "-loglevel", "error",
					"-ss", str(max(0.0, position)), "-i", str(movie_path), "-vn", "-sn",
				],
				stdin=subprocess.DEVNULL,
				stdout=subprocess.DEVNULL,
				stderr=subprocess.DEVNULL,
			)
		except OSError as error:
			self.audio_process = None
			self._append_terminal(f"Could not play the movie audio: {error}")

	def _read_local_frames(self, process: subprocess.Popen[bytes]) -> None:
		"""Read frames off the Tk thread and keep only the newest decoded frames."""
		if process.stdout is None:
			self.preview_eof.set()
			return
		frame_size = 640 * 360 * 3
		try:
			while True:
				frame = read_exactly(process.stdout, frame_size)
				if len(frame) != frame_size:
					self.preview_eof.set()
					return
				if self.preview_frames.full():
					try:
						self.preview_frames.get_nowait()
					except queue.Empty:
						pass
				self.preview_frames.put_nowait(frame)
		except (OSError, ValueError):
			self.preview_eof.set()

	def _render_local_frame(self) -> None:
		"""Display the newest decoded RGB frame in the Tk screen widget."""
		process = self.preview_process
		if process is None:
			return
		frame = None
		while not self.preview_frames.empty():
			try:
				frame = self.preview_frames.get_nowait()
			except queue.Empty:
				break
		if frame is not None:
			image = Image.frombytes("RGB", (640, 360), frame)
			image.thumbnail((max(320, self.screen.winfo_width()), max(180, self.screen.winfo_height())), Image.Resampling.LANCZOS)
			self.preview_image = ImageTk.PhotoImage(image)
			self.preview_label.configure(image=self.preview_image)
			self.preview_label.image = self.preview_image
			self.movie_label.configure(text="")
		if self.preview_eof.is_set() and process.poll() is not None:
			if self.state.playing:
				self.state.control("pause")
			return
		self.preview_after_id = self.root.after(15, self._render_local_frame)

	def _stop_local_preview(self) -> None:
		"""Stop frame decoding and leave shared playback state unchanged."""
		if self.preview_after_id is not None:
			self.root.after_cancel(self.preview_after_id)
			self.preview_after_id = None
		process = self.preview_process
		self.preview_process = None
		self.preview_movie_path = None
		if process is not None and process.poll() is None:
			process.terminate()
		if process is not None and process.stdout is not None:
			process.stdout.close()
		self.preview_reader = None
		audio_process = self.audio_process
		self.audio_process = None
		if audio_process is not None and audio_process.poll() is None:
			audio_process.terminate()
			try:
				audio_process.wait(timeout=1)
			except subprocess.TimeoutExpired:
				audio_process.kill()
				audio_process.wait()

	def _shutdown(self) -> None:
		"""Stop accepting guest requests and close the desktop cleanly."""
		with self.state.lock:
			self.state.sessions.clear()
			self.state.closed = True
		self._stop_local_preview()
		self._stop_public_tunnel()
		self.server.shutdown()
		self.server.server_close()
		self.root.destroy()

	def _stop_public_tunnel(self) -> None:
		"""Stop the optional internet tunnel with a bounded graceful wait."""
		process = self.tunnel_process
		if process is None or process.poll() is not None:
			return
		process.terminate()
		try:
			process.wait(timeout=2)
		except subprocess.TimeoutExpired:
			process.kill()
			process.wait()


def find_available_port() -> int:
	"""Choose the preferred port or the next free local port."""
	for port in range(PORT, PORT + 20):
		probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		try:
			probe.bind(("0.0.0.0", port))
			return port
		except OSError:
			continue
		finally:
			probe.close()
	raise OSError("No free theater port was found between 8765 and 8784.")


def cloudflare_url_from_line(line: str) -> str | None:
	"""Extract only a Cloudflare quick-tunnel URL from one log line."""
	match = CLOUDFLARE_URL_PATTERN.search(line)
	return match.group(0) if match else None


def guest_url_for(port: int, cloud_mode: bool, external_url: str) -> str:
	"""Choose the externally reachable URL or the appropriate local address."""
	if external_url:
		return f"{external_url.rstrip('/')}/"
	if cloud_mode:
		return f"http://0.0.0.0:{port}/"
	return f"http://{local_ip_address()}:{port}/"


def main() -> None:
	"""Start local Tkinter hosting or the headless cloud web service."""
	seat_key = secrets.token_urlsafe(6).replace("-", "A").replace("_", "B").upper()
	state = TheaterState(seat_key)
	cloud_mode = "--cloud" in sys.argv[1:] or os.environ.get("PRJ_MODE", "").lower() == "cloud"
	public_mode = "--public" in sys.argv[1:]
	if cloud_mode:
		if public_mode:
			raise SystemExit("--public is for desktop hosting; cloud services already have a public URL.")
		admin_password = os.environ.get("PRJ_ADMIN_PASSWORD", "")
		if len(admin_password) < 16:
			raise SystemExit("Set PRJ_ADMIN_PASSWORD to a secret of at least 16 characters for cloud hosting.")
		port = int(os.environ.get("PORT", "10000"))
		handler_options = {"state": state, "cloud_mode": True, "admin_password": admin_password}
	else:
		if tk_widgets_unavailable:
			raise SystemExit("Tkinter is unavailable. Run with --cloud for headless web hosting.")
		port = find_available_port()
		handler_options = {"state": state}
	handler = type("BoundTheaterHandler", (TheaterHandler,), handler_options)
	server = TheaterServer(("0.0.0.0", port), handler)
	external_url = (
		os.environ.get("PRJ_PUBLIC_URL", "").rstrip("/")
		or os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
	)
	local_url = guest_url_for(port, cloud_mode, external_url)
	print(f"{APP_NAME} is ready at {local_url}")
	print(f"Seat key: {seat_key}")
	print("Share the seat key and built-in password with invited guests.")
	if cloud_mode:
		print(f"Host console: {local_url.rstrip('/')}/host")
		server.serve_forever()
		return
	print("For internet access, run with --public or configure a trusted HTTPS tunnel.")

	server_thread = threading.Thread(target=server.serve_forever, name="theater-http", daemon=True)
	server_thread.start()
	try:
		root = tk.Tk()
		app = HostApp(root, state, local_url)
		app.server = server
		if public_mode and not external_url:
			app.start_public_tunnel(port)
		elif public_mode:
			app._append_terminal(f"Using configured public URL: {local_url}")
		app._refresh()
		root.mainloop()
	except tk.TclError as error:
		if "app" in locals():
			app._stop_public_tunnel()
		server.shutdown()
		server.server_close()
		raise SystemExit(f"Could not open the Tkinter host window: {error}") from error


if __name__ == "__main__":
	main()