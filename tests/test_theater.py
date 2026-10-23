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

from backend.user import user_login
from watch import (
    MAX_SEATS,
    TheaterHandler,
    TheaterServer,
    TheaterState,
    cloudflare_url_from_line,
    external_media_directory,
    guest_url_for,
    read_exactly,
)
import watch


class TheaterStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TheaterState("TEST-KEY")

    def test_credentials_require_current_key_and_builtin_password(self) -> None:
        self.assertTrue(user_login("Guest", "TEST-KEY", "hello_hello3_whatch_with me", "TEST-KEY"))
        self.assertFalse(user_login("Guest", "OLD-KEY", "hello_hello3_whatch_with me", "TEST-KEY"))
        self.assertFalse(user_login("Guest", "TEST-KEY", "incorrect", "TEST-KEY"))
        self.assertFalse(user_login("  ", "TEST-KEY", "hello_hello3_whatch_with me", "TEST-KEY"))

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
        app.public_link_updates.put(("error", "Cloudflare Tunnel stopped."))

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

    def test_guest_snapshot_does_not_include_host_terminal_events(self) -> None:
        token, _ = self.state.add_guest("Guest")
        self.assertEqual(self.state.state_for(token)["events"], [])
        self.assertTrue(self.state.state_for()["events"])

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
        self.state = TheaterState("CLOUD-KEY")
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
        self.assertEqual(json.loads(response.read())["seat_key"], "CLOUD-KEY")

    def test_cloud_host_commands_require_admin_session(self) -> None:
        status, _ = self.post("/api/host/command", {"command": "restart theater"})
        self.assertEqual(status, 403)
        _, login = self.post("/api/host/login", {"password": "A-secure-test-password"})
        status, result = self.post("/api/host/command", {"command": "close theater 30"}, login["token"])
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertGreater(self.state.state_for()["close_countdown"], 0)

    def test_cloud_guest_page_and_host_page_are_served(self) -> None:
        for route in ("/", "/host"):
            self.connection.request("GET", route)
            response = self.connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b"PRJ-00E1", response.read())

    def test_video_element_cookie_can_access_guest_movie(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.object(watch, "MEDIA_ROOT", Path(folder)):
            self.connection.request(
                "POST", "/api/login",
                json.dumps({
                    "username": "Guest",
                    "seat_key": "CLOUD-KEY",
                    "password": "hello_hello3_whatch_with me",
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


if __name__ == "__main__":
    unittest.main()
