"""Behavior checks for the lightweight theater room and authentication."""

import http.client
import io
import json
import queue
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.user import DEFAULT_SEAT_KEY, user_login
from watch import (
    MAX_SEATS,
    TheaterHandler,
    TheaterServer,
    TheaterState,
    cloudflare_url_from_line,
    external_media_directory,
    guest_url_for,
    read_exactly,
    write_ppm_frame,
)
import watch


class TheaterStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TheaterState("TEST-KEY")

    def test_credentials_require_current_key_and_builtin_password(self) -> None:
        self.assertTrue(user_login("Guest", DEFAULT_SEAT_KEY, "FANTASY000", DEFAULT_SEAT_KEY))
        self.assertFalse(user_login("Guest", "OLD-KEY", "FANTASY000", DEFAULT_SEAT_KEY))
        self.assertFalse(user_login("Guest", DEFAULT_SEAT_KEY, "incorrect", DEFAULT_SEAT_KEY))
        self.assertFalse(user_login("  ", DEFAULT_SEAT_KEY, "FANTASY000", DEFAULT_SEAT_KEY))

    def test_cloudflare_tunnel_url_is_extracted_from_logs(self) -> None:
        self.assertEqual(
            cloudflare_url_from_line(
                "Quick Tunnel is ready at https://example-name.trycloudflare.com"
            ),
            "https://example-name.trycloudflare.com",
        )
        self.assertIsNone(cloudflare_url_from_line("Tunnel connection established"))
        self.assertIsNone(cloudflare_url_from_line("https://example.com is not a quick tunnel"))

    def test_public_tunnel_failure_keeps_the_local_guest_link_available(self) -> None:
        app = watch.HostApp.__new__(watch.HostApp)
        app.local_guest_url = "http://192.0.2.10:8765/"
        app.guest_url = "https://theater.trycloudflare.com/"
        app.public_link_updates = queue.Queue()
        app.link_label = Mock()
        app._append_terminal = Mock()
        app.tunnel_generation = 0
        app.public_link_updates.put((0, "error", "Cloudflare Tunnel stopped."))

        app._refresh_public_link()

        self.assertEqual(app.guest_url, app.local_guest_url)
        app.link_label.configure.assert_called_once_with(
            text=f"LOCAL GUEST LINK  {app.local_guest_url}"
        )

    def test_guest_url_prefers_configured_public_url_in_desktop_mode(self) -> None:
        self.assertEqual(
            guest_url_for(8765, False, "https://theater.example/"),
            "https://theater.example/",
        )
        self.assertEqual(
            guest_url_for(8765, True, "https://theater.example/"),
            "https://theater.example/",
        )
        self.assertEqual(guest_url_for(8765, True, ""), "http://0.0.0.0:8765/")
        with patch.object(watch, "local_ip_address", return_value="192.0.2.10"):
            self.assertEqual(
                guest_url_for(8765, False, ""),
                "http://192.0.2.10:8765/",
            )

    def test_external_media_directory_detects_mounted_linux_volumes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            volume = Path(folder) / "USB_DISK"
            volume.mkdir()

            def mounted_volumes(directory: Path) -> object:
                if directory == Path("/media/guest"):
                    return iter([volume])
                raise OSError("No mounted volumes here")

            with (
                patch.object(Path, "home", return_value=Path("/home/guest")),
                patch.object(Path, "iterdir", autospec=True, side_effect=mounted_volumes),
                patch.object(Path, "is_mount", autospec=True, side_effect=lambda path: path == volume),
            ):
                self.assertEqual(external_media_directory("linux"), str(volume))

    def test_movie_picker_opens_at_detected_external_volume(self) -> None:
        app = watch.HostApp.__new__(watch.HostApp)
        app.title_entry = Mock()
        app.title_entry.get.return_value = "Movie title"
        app.state = Mock()
        movie_path = "/media/guest/USB_DISK/movie.mp4"

        with (
            patch.object(watch, "external_media_directory", return_value="/media/guest/USB_DISK"),
            patch.object(watch.filedialog, "askopenfilename", return_value=movie_path) as picker,
        ):
            app._choose_movie()

        self.assertEqual(picker.call_args.kwargs["initialdir"], "/media/guest/USB_DISK")
        app.state.set_movie.assert_called_once_with(Path(movie_path), "movie")

    def test_waiting_guest_is_promoted_when_a_seat_opens(self) -> None:
        guests = [self.state.add_guest(f"Guest {index}") for index in range(MAX_SEATS + 1)]
        self.assertEqual(sum(admitted for _, admitted in guests), MAX_SEATS)
        waiting_token = guests[-1][0]
        self.state.sessions.pop(guests[0][0])
        self.state._promote_waiting()
        self.assertTrue(self.state.sessions[waiting_token]["active"])

    def test_remove_guest_disconnects_matching_sessions_and_promotes_waiting_guest(self) -> None:
        guests = [self.state.add_guest(f"Guest {index}") for index in range(MAX_SEATS - 1)]
        duplicate_token, _ = self.state.add_guest(" Alex ")
        waiting_token, admitted = self.state.add_guest("Waiting")
        self.assertFalse(admitted)
        second_alex_token, _ = self.state.add_guest("alex")

        removed_count = self.state.remove_guest("  ALEX ")

        self.assertEqual(removed_count, 2)
        self.assertNotIn(duplicate_token, self.state.sessions)
        self.assertNotIn(second_alex_token, self.state.sessions)
        self.assertTrue(self.state.sessions[waiting_token]["active"])
        self.assertEqual(self.state.active_count(), MAX_SEATS)
        self.assertEqual(len(guests), MAX_SEATS - 1)

    def test_remove_guest_returns_zero_for_unknown_username(self) -> None:
        self.assertEqual(self.state.remove_guest("missing"), 0)

    def test_desktop_reinit_restarts_server_and_starts_new_tunnel(self) -> None:
        guest_token, _ = self.state.add_guest("Guest")
        handler = type("RestartTestHandler", (TheaterHandler,), {"state": self.state})
        old_server = TheaterServer(("127.0.0.1", 0), handler)
        old_port = old_server.server_port
        old_thread = threading.Thread(target=old_server.serve_forever, daemon=True)
        old_thread.start()

        app = watch.HostApp.__new__(watch.HostApp)
        app.server = old_server
        app.server_thread = old_thread
        app.state = self.state
        app.tunnel_process = None
        app.tunnel_generation = 0
        app.public_link_updates = queue.Queue()
        app.public_mode = True
        app.configured_external_url = ""
        app.link_label = Mock()
        app._append_terminal = Mock()
        app.start_public_tunnel = Mock()

        try:
            app._restart_server_with_new_tunnel()

            self.assertIsNot(app.server, old_server)
            self.assertEqual(app.server.server_port, old_port)
            self.assertTrue(app.server_thread.is_alive())
            self.assertNotIn(guest_token, self.state.sessions)
            app.start_public_tunnel.assert_called_once_with(old_port)

            connection = http.client.HTTPConnection("127.0.0.1", old_port, timeout=2)
            connection.request("GET", "/")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
        finally:
            app.server.shutdown()
            app.server.server_close()
            app.server_thread.join()

    def test_desktop_command_reinit_dispatches_restart(self) -> None:
        app = watch.HostApp.__new__(watch.HostApp)
        app.terminal_input = Mock()
        app.terminal_input.get.return_value = "reinit -m --ser"
        app.command_history = []
        app.history_index = 0
        app._append_terminal = Mock()
        app._restart_server_with_new_tunnel = Mock()

        app._run_command()

        app._restart_server_with_new_tunnel.assert_called_once_with()

    def test_guest_snapshot_does_not_include_host_terminal_events(self) -> None:
        token, _ = self.state.add_guest("Guest")
        self.assertEqual(self.state.state_for(token)["events"], [])
        self.assertTrue(self.state.state_for()["events"])

    def test_guest_session_survives_mobile_background_pause(self) -> None:
        token, _ = self.state.add_guest("Guest")
        self.state.sessions[token]["last_seen"] = (
            time.monotonic() - watch.GUEST_IDLE_TIMEOUT_SECONDS + 1
        )

        self.assertTrue(self.state.state_for(token)["admitted"])
        self.assertIn(token, self.state.sessions)

    def test_inactive_guest_session_expires_after_idle_timeout(self) -> None:
        token, _ = self.state.add_guest("Guest")
        self.state.sessions[token]["last_seen"] = (
            time.monotonic() - watch.GUEST_IDLE_TIMEOUT_SECONDS - 1
        )

        snapshot = self.state.state_for(token)

        self.assertFalse(snapshot["admitted"])
        self.assertNotIn(token, self.state.sessions)

    def test_expired_close_clears_room_and_signs_out_guests(self) -> None:
        self.state.add_guest("Guest")
        self.state.close_after(5)
        self.state.close_deadline = time.monotonic() - 1
        snapshot = self.state.state_for()
        self.assertTrue(snapshot["closed"])
        self.assertEqual(snapshot["active"], 0)
        self.assertEqual(snapshot["movie"], "")

    def test_setting_movie_starts_shared_playback(self) -> None:
        self.state.set_movie(Path("sample.mp4"), "Sample")
        snapshot = self.state.state_for()
        self.assertTrue(snapshot["playing"])
        self.assertEqual(snapshot["movie"], "Sample")
        self.assertLess(snapshot["position"], 0.01)

    def test_read_exactly_accumulates_short_video_pipe_reads(self) -> None:
        class ShortReader:
            def __init__(self, payload: bytes) -> None:
                self.payload = payload

            def read(self, byte_count: int) -> bytes:
                chunk = self.payload[:min(7001, byte_count)]
                self.payload = self.payload[len(chunk):]
                return chunk

        payload = bytes(range(256)) * 2700
        frame = read_exactly(ShortReader(payload), 640 * 360 * 3)
        self.assertEqual(len(frame), 640 * 360 * 3)
        self.assertEqual(frame, payload[:len(frame)])

    def test_write_ppm_frame_uses_tk_builtin_image_format(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            frame_path = Path(folder) / "frame.ppm"
            pixels = b"\xff\x00\x00\x00\xff\x00"
            write_ppm_frame(frame_path, pixels, width=2, height=1)
            self.assertEqual(frame_path.read_bytes(), b"P6\n2 1\n255\n" + pixels)

    def test_write_ppm_frame_rejects_incomplete_rgb_data(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "Expected an RGB frame"):
                write_ppm_frame(Path(folder) / "frame.ppm", b"\x00", width=2, height=1)

    def test_waiting_guest_cannot_send_messages_or_control_playback(self) -> None:
        guests = [self.state.add_guest(f"Guest {index}") for index in range(MAX_SEATS + 1)]
        waiting_token = guests[-1][0]
        with self.assertRaises(PermissionError):
            self.state.require_active(waiting_token)

    def test_video_stream_quietly_stops_when_browser_disconnects(self) -> None:
        class DisconnectedWriter:
            def write(self, _chunk: bytes) -> None:
                raise ConnectionResetError("browser closed the video request")

        class VideoHandler:
            headers: dict[str, str] = {}
            wfile = DisconnectedWriter()

            def send_response(self, _status: int) -> None:
                pass

            def send_header(self, _name: str, _value: str) -> None:
                pass

            def end_headers(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as folder:
            video_path = Path(folder) / "sample.mp4"
            video_path.write_bytes(b"test video")
            TheaterHandler._serve_video(VideoHandler(), video_path)


class CloudHostApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TheaterState(DEFAULT_SEAT_KEY)
        handler = type(
            "CloudTestHandler",
            (TheaterHandler,),
            {"state": self.state, "cloud_mode": True, "admin_password": "A-secure-test-password"},
        )
        self.server = TheaterServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)

    def tearDown(self) -> None:
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def post(self, route: str, body: dict[str, str], token: str = "") -> tuple[int, dict[str, object]]:
        self.connection.request(
            "POST", route, json.dumps(body),
            {"Content-Type": "application/json", "X-Host-Token": token},
        )
        response = self.connection.getresponse()
        return response.status, json.loads(response.read())

    def test_cloud_host_password_creates_admin_session(self) -> None:
        status, _ = self.post("/api/host/login", {"password": "incorrect"})
        self.assertEqual(status, 401)
        status, result = self.post("/api/host/login", {"password": "A-secure-test-password"})
        self.assertEqual(status, 200)
        self.assertIn("token", result)

        self.connection.request("GET", "/api/host/state", headers={"X-Host-Token": result["token"]})
        response = self.connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())["seat_key"], DEFAULT_SEAT_KEY)

    def test_cloud_host_commands_require_admin_session(self) -> None:
        status, _ = self.post("/api/host/command", {"command": "restart theater"})
        self.assertEqual(status, 403)
        _, login = self.post("/api/host/login", {"password": "A-secure-test-password"})
        status, result = self.post("/api/host/command", {"command": "close theater 30"}, login["token"])
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertGreater(self.state.state_for()["close_countdown"], 0)

    def test_cloud_host_command_removes_guest_by_username(self) -> None:
        guest_token, _ = self.state.add_guest("Alex")
        _, login = self.post("/api/host/login", {"password": "A-secure-test-password"})

        status, result = self.post(
            "/api/host/command", {"command": "remove -user alex"}, login["token"]
        )

        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertNotIn(guest_token, self.state.sessions)

    def test_cloud_host_cannot_reinitialize_desktop_server(self) -> None:
        _, login = self.post("/api/host/login", {"password": "A-secure-test-password"})

        status, result = self.post(
            "/api/host/command", {"command": "reinit -m --ser"}, login["token"]
        )

        self.assertEqual(status, 403)
        self.assertIn("desktop host console", result["error"])

    def test_cloud_guest_page_and_host_page_are_served(self) -> None:
        for route in ("/", "/host"):
            self.connection.request("GET", route)
            response = self.connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b"PRJ-00E1", response.read())

    def test_guest_leave_endpoint_releases_seat(self) -> None:
        status, login = self.post(
            "/api/login",
            {
                "username": "Guest",
                "seat_key": DEFAULT_SEAT_KEY,
                "password": "FANTASY000",
            },
        )
        self.assertEqual(status, 200)
        token = login["token"]
        self.assertEqual(self.state.state_for()["active"], 1)

        self.connection.request(
            "POST", "/api/leave", json.dumps({"token": token}),
            {"Content-Type": "application/json"},
        )
        self.assertEqual(self.connection.getresponse().status, 200)
        self.assertEqual(self.state.state_for()["active"], 0)
        self.assertNotIn(token, self.state.sessions)

    def test_video_element_cookie_can_access_guest_movie(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.object(watch, "MEDIA_ROOT", Path(folder)):
            self.connection.request(
                "POST", "/api/login",
                json.dumps({
                    "username": "Guest",
                    "seat_key": DEFAULT_SEAT_KEY,
                    "password": "FANTASY000",
                }),
                {"Content-Type": "application/json"},
            )
            login = self.connection.getresponse()
            self.assertEqual(login.status, 200)
            login_result = json.loads(login.read())
            cookie = login.getheader("Set-Cookie")
            self.assertIn("HttpOnly", cookie)
            self.connection.request(
                "POST", "/api/upload", b"video bytes",
                {
                    "X-Session-Token": login_result["token"],
                    "X-Filename": "sample.mp4",
                    "X-Movie-Title": "Sample",
                },
            )
            upload = self.connection.getresponse()
            self.assertEqual(upload.status, 200)
            upload.read()
            self.connection.request(
                "POST", "/api/control",
                json.dumps({"token": login_result["token"], "action": "play"}),
                {"Content-Type": "application/json"},
            )
            self.assertEqual(self.connection.getresponse().status, 200)
            self.connection.request(
                "GET", "/media", headers={"Cookie": cookie.split(";", 1)[0], "Range": "bytes=0-4"},
            )
            response = self.connection.getresponse()
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"video")
            self.connection.request(
                "GET", "/media", headers={"Cookie": cookie.split(";", 1)[0], "Range": "bytes=-4"},
            )
            response = self.connection.getresponse()
            self.assertEqual(response.status, 206)
            self.assertEqual(response.getheader("Content-Range"), "bytes 7-10/11")
            self.assertEqual(response.read(), b"ytes")
            self.connection.request(
                "HEAD", "/media", headers={"Cookie": cookie.split(";", 1)[0]},
            )
            response = self.connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "video/mp4")
            self.assertEqual(response.read(), b"")

    def test_cloud_host_preview_requires_host_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            movie = Path(folder) / "sample.mp4"
            movie.write_bytes(b"host video")
            self.state.set_movie(movie, "Host Sample")
            _, login = self.post("/api/host/login", {"password": "A-secure-test-password"})
            self.connection.request("GET", "/host-media", headers={"Range": "bytes=0-3"})
            self.assertEqual(self.connection.getresponse().status, 403)
            self.connection.request(
                "GET", "/host-media",
                headers={"Cookie": f"PRJ_HOST_SESSION={login['token']}", "Range": "bytes=0-3"},
            )
            response = self.connection.getresponse()
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"host")
            self.connection.request(
                "HEAD", "/host-media",
                headers={
                    "Cookie": f"PRJ_HOST_SESSION={login['token']}",
                    "Range": "bytes=0-3",
                },
            )
            response = self.connection.getresponse()
            self.assertEqual(response.status, 206)
            self.assertEqual(response.getheader("Content-Range"), "bytes 0-3/10")
            self.assertEqual(response.getheader("Content-Type"), "video/mp4")
            self.assertEqual(response.read(), b"")


if __name__ == "__main__":
    unittest.main()
