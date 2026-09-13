from __future__ import annotations

import io
import subprocess
import sys
import unittest
from unittest import mock

try:
    import gusb_receiver as receiver_module

    from gusb_receiver import (
        AOA_DATA_PRODUCT_IDS,
        AOA_AUDIO_ONLY_PRODUCT_IDS,
        AccessoryConnection,
        BulkInterface,
        FfplaySink,
        LIBUSB_ERROR_TIMEOUT,
        LibUSB,
        StreamSession,
        LatestFrameQueue,
        AccessUnit,
        StreamStopped,
        USBError,
        USBIdentity,
        identity_matches,
        parse_device_selector,
        parse_stream_info,
    )
    from protocol import (
        AOA_IDENTIFIER_STRINGS,
        HEADER,
        HELLO_PAYLOAD,
        MAGIC,
        MAX_PAYLOAD,
        TYPE_ERROR,
        TYPE_HELLO,
        TYPE_INFO,
        TYPE_STOP,
        TYPE_VIDEO,
        FLAG_CODEC_CONFIG,
        FLAG_END_OF_ACCESS_UNIT,
        FLAG_KEY_FRAME,
        Frame,
        FrameError,
        FrameParser,
        encode_frame,
    )
except ImportError:  # ``python -m unittest discover -s receiver -t .``.
    from . import gusb_receiver as receiver_module

    from .gusb_receiver import (
        AOA_DATA_PRODUCT_IDS,
        AOA_AUDIO_ONLY_PRODUCT_IDS,
        AccessoryConnection,
        BulkInterface,
        FfplaySink,
        LIBUSB_ERROR_TIMEOUT,
        LibUSB,
        StreamSession,
        LatestFrameQueue,
        AccessUnit,
        StreamStopped,
        USBError,
        USBIdentity,
        identity_matches,
        parse_device_selector,
        parse_stream_info,
    )
    from .protocol import (
        AOA_IDENTIFIER_STRINGS,
        HEADER,
        HELLO_PAYLOAD,
        MAGIC,
        MAX_PAYLOAD,
        TYPE_ERROR,
        TYPE_HELLO,
        TYPE_INFO,
        TYPE_STOP,
        TYPE_VIDEO,
        FLAG_CODEC_CONFIG,
        FLAG_END_OF_ACCESS_UNIT,
        FLAG_KEY_FRAME,
        Frame,
        FrameError,
        FrameParser,
        encode_frame,
    )


class FrameProtocolTests(unittest.TestCase):
    def test_parser_accepts_fragmented_and_coalesced_frames(self) -> None:
        info = encode_frame(TYPE_INFO, b'{"width":1280,"height":720,"fps":30,"codec":"h264"}')
        video = encode_frame(TYPE_VIDEO, b"\x00\x00\x01\x65frame")
        parser = FrameParser()

        self.assertEqual(parser.feed(info[:3]), [])
        self.assertEqual(parser.feed(info[3:8]), [])
        frames = parser.feed(info[8:] + video)
        self.assertEqual([frame.type for frame in frames], [TYPE_INFO, TYPE_VIDEO])
        self.assertEqual(frames[1].payload, b"\x00\x00\x01\x65frame")

    def test_parser_decodes_frames_without_test_side_effects(self) -> None:
        info_payload = b'{"width":1280,"height":720,"fps":30,"codec":"h264"}'
        wire = encode_frame(TYPE_INFO, info_payload) + encode_frame(TYPE_STOP)
        parser = FrameParser()
        frames = []
        for chunk in (wire[:1], wire[1:6], wire[6:17], wire[17:]):
            frames.extend(parser.feed(chunk))
        self.assertEqual([frame.type for frame in frames], [TYPE_INFO, TYPE_STOP])
        self.assertEqual(frames[0].payload, info_payload)
        self.assertEqual(parser.buffered_bytes, 0)

    def test_parser_rejects_bad_header_and_oversize(self) -> None:
        parser = FrameParser()
        with self.assertRaises(FrameError):
            parser.feed(b"BAD!" + b"\0" * (HEADER.size - 4))
        with self.assertRaises(FrameError):
            FrameParser().feed(HEADER.pack(MAGIC, 2, TYPE_VIDEO, 0, 0))
        with self.assertRaises(FrameError):
            FrameParser().feed(HEADER.pack(MAGIC, 1, 99, 0, 0))
        oversized = HEADER.pack(MAGIC, 1, TYPE_VIDEO, 0, MAX_PAYLOAD + 1)
        with self.assertRaises(FrameError):
            FrameParser().feed(oversized)

    def test_encode_enforces_types_and_size(self) -> None:
        self.assertEqual(len(encode_frame(TYPE_HELLO, HELLO_PAYLOAD)), HEADER.size + len(HELLO_PAYLOAD))
        with self.assertRaises(FrameError):
            encode_frame(99)
        with self.assertRaises(FrameError):
            encode_frame(TYPE_VIDEO, b"x" * (MAX_PAYLOAD + 1))


class InfoAndSelectionTests(unittest.TestCase):
    def test_info_validation(self) -> None:
        info = parse_stream_info(
            b'{"width":1920,"height":1080,"fps":60,"codec":"h264"}'
        )
        self.assertEqual((info.width, info.height, info.fps, info.codec), (1920, 1080, 60.0, "h264"))
        for payload in (
            b"[]",
            b'{"width":0,"height":1080,"fps":60,"codec":"h264"}',
            b'{"width":1920,"height":1080,"fps":60,"codec":"hevc"}',
            b'{"width":1920,"height":1080,"fps":0,"codec":"h264"}',
        ):
            with self.subTest(payload=payload), self.assertRaises(FrameError):
                parse_stream_info(payload)

    def test_device_selector_and_stable_port_matching(self) -> None:
        selector = parse_device_selector("2:17")
        self.assertEqual((selector.bus, selector.address), (2, 17))
        with self.assertRaises(Exception):
            parse_device_selector("not-a-device")
        expected = USBIdentity(2, 17, (4, 2))
        reenumerated = USBIdentity(2, 31, (4, 2))
        self.assertTrue(identity_matches(expected, reenumerated))
        other_port = USBIdentity(2, 31, (4, 3))
        self.assertFalse(identity_matches(expected, other_port))

    def test_accessory_product_policy(self) -> None:
        self.assertIn(0x2D00, AOA_DATA_PRODUCT_IDS)
        self.assertNotIn(0x2D02, AOA_DATA_PRODUCT_IDS)
        self.assertIn(0x2D02, AOA_AUDIO_ONLY_PRODUCT_IDS)

    def test_aoa_identifier_strings_match_android_contract(self) -> None:
        self.assertEqual(
            AOA_IDENTIFIER_STRINGS,
            (
                "Omarchy",
                "Galaxy USB MediaProjection",
                "USB screen projection",
                "1",
                "https://github.com/yu1sh/linux-galaxy-dex",
                "omarchy-galaxy-usb",
            ),
        )


class _BinaryStdout:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def flush(self) -> None:
        self.buffer.flush()


class _FakeUSB:
    def __init__(self, incoming: list[bytes]) -> None:
        self.incoming = iter(incoming)
        self.outgoing: list[bytes] = []

    def bulk_out(self, _connection: object, data: bytes, **_kwargs: object) -> None:
        self.outgoing.append(data)

    def bulk_in(self, _connection: object) -> bytes:
        return next(self.incoming)


class StreamSessionTests(unittest.TestCase):
    def _connection(self) -> object:
        return object()

    def test_stdout_contains_video_payload_only(self) -> None:
        info = encode_frame(TYPE_INFO, b'{"width":640,"height":480,"fps":30,"codec":"h264"}')
        video = b"\x00\x00\x00\x01\x65sample-h264"
        wire = info + encode_frame(TYPE_VIDEO, video) + encode_frame(TYPE_STOP)
        fake_usb = _FakeUSB([wire])
        stdout = _BinaryStdout()
        session = StreamSession(fake_usb, self._connection(), raw_stdout=True, ffplay_path=None)
        with mock.patch.object(sys, "stdout", stdout):
            session.run()
        self.assertEqual(stdout.buffer.getvalue(), video)
        self.assertEqual(FrameParser().feed(fake_usb.outgoing[0])[0].type, TYPE_HELLO)

    def test_malformed_frame_sends_error_and_stops(self) -> None:
        fake_usb = _FakeUSB([b"BAD!" + b"\0" * (HEADER.size - 4)])
        session = StreamSession(fake_usb, self._connection(), raw_stdout=True, ffplay_path=None)
        with self.assertRaises(FrameError):
            session.run()
        outgoing_types = [frame.type for frame in FrameParser().feed(b"".join(fake_usb.outgoing))]
        self.assertEqual(outgoing_types, [TYPE_HELLO, TYPE_ERROR])

    def test_player_breakage_sends_stop(self) -> None:
        info_payload = b'{"width":640,"height":480,"fps":30,"codec":"h264"}'
        fake_usb = _FakeUSB([])
        session = StreamSession(fake_usb, self._connection(), raw_stdout=False, ffplay_path="ffplay")
        fake_sink = mock.Mock()
        fake_sink.write.side_effect = StreamStopped("player exited")
        with mock.patch.object(receiver_module, "FfplaySink", return_value=fake_sink):
            session.handle(Frame(TYPE_INFO, 0, info_payload))
            with self.assertRaises(StreamStopped):
                session.handle(Frame(TYPE_VIDEO, 0, b"video"))
        output_frames = FrameParser().feed(b"".join(fake_usb.outgoing))
        self.assertEqual([frame.type for frame in output_frames], [TYPE_STOP])

    def test_rotation_info_is_accepted(self) -> None:
        fake_usb = _FakeUSB([])
        session = StreamSession(fake_usb, self._connection(), raw_stdout=True, ffplay_path=None)
        session.handle(Frame(TYPE_INFO, 0, b'{"width":640,"height":480,"fps":30,"codec":"h264"}'))
        session.handle(Frame(TYPE_INFO, 0, b'{"width":480,"height":640,"fps":30,"codec":"h264"}'))
        self.assertEqual((session.info.width, session.info.height), (480, 640))

    def test_codec_configs_are_concatenated_for_idr_recovery(self) -> None:
        fake_usb = _FakeUSB([])
        session = StreamSession(fake_usb, self._connection(), raw_stdout=True, ffplay_path=None)
        info = Frame(TYPE_INFO, 0, b'{"width":640,"height":480,"fps":30,"codec":"h264"}')
        session.handle(info)
        sps, pps, idr = b"SPS", b"PPS", b"IDR"
        session.handle(Frame(TYPE_VIDEO, FLAG_CODEC_CONFIG | FLAG_END_OF_ACCESS_UNIT, sps))
        session.handle(Frame(TYPE_VIDEO, FLAG_CODEC_CONFIG | FLAG_END_OF_ACCESS_UNIT, pps))
        session.handle(Frame(TYPE_VIDEO, FLAG_KEY_FRAME | FLAG_END_OF_ACCESS_UNIT, idr))
        unit = session._video_queue.get_latest()
        self.assertIsNotNone(unit)
        self.assertEqual(unit.payload, sps + pps + idr)

    def test_rotation_drops_old_codec_config(self) -> None:
        fake_usb = _FakeUSB([])
        session = StreamSession(fake_usb, self._connection(), raw_stdout=True, ffplay_path=None)
        session.handle(Frame(TYPE_INFO, 0, b'{"width":640,"height":480,"fps":30,"codec":"h264"}'))
        session.handle(Frame(TYPE_VIDEO, FLAG_CODEC_CONFIG | FLAG_END_OF_ACCESS_UNIT, b"OLD"))
        session.handle(Frame(TYPE_INFO, 0, b'{"width":480,"height":640,"fps":30,"codec":"h264"}'))
        session.handle(Frame(TYPE_VIDEO, FLAG_KEY_FRAME | FLAG_END_OF_ACCESS_UNIT, b"NEW"))
        self.assertEqual(session._video_queue.get_latest().payload, b"NEW")

    def test_latest_queue_drops_old_units(self) -> None:
        q = LatestFrameQueue(max_items=2, max_bytes=100)
        for value in (b"a", b"b", b"c"):
            q.put(AccessUnit(value, 0, receiver_module.time.monotonic()))
        self.assertEqual(q.get_latest().payload, b"c")

    def test_run_burst_outputs_keyframe_before_predictive_frame(self) -> None:
        info = encode_frame(TYPE_INFO, b'{"width":640,"height":480,"fps":30,"codec":"h264"}')
        wire = info
        wire += encode_frame(TYPE_VIDEO, b"SPS", FLAG_CODEC_CONFIG | FLAG_END_OF_ACCESS_UNIT)
        wire += encode_frame(TYPE_VIDEO, b"PPS", FLAG_CODEC_CONFIG | FLAG_END_OF_ACCESS_UNIT)
        wire += encode_frame(TYPE_VIDEO, b"IDR", FLAG_KEY_FRAME | FLAG_END_OF_ACCESS_UNIT)
        wire += encode_frame(TYPE_VIDEO, b"P", FLAG_END_OF_ACCESS_UNIT)
        wire += encode_frame(TYPE_STOP)
        stdout = _BinaryStdout()
        session = StreamSession(_FakeUSB([wire]), self._connection(), raw_stdout=True, ffplay_path=None)
        with mock.patch.object(sys, "stdout", stdout):
            session.run()
        output = stdout.buffer.getvalue()
        self.assertLess(output.index(b"SPS"), output.index(b"P"))
        self.assertIn(b"SPSPPSIDR", output)

    def test_queue_drop_suppresses_predictive_frames_until_keyframe(self) -> None:
        q = LatestFrameQueue(max_items=1, max_bytes=100)
        q.put(AccessUnit(b"IDR", FLAG_KEY_FRAME, receiver_module.time.monotonic()))
        q.put(AccessUnit(b"P", 0, receiver_module.time.monotonic()))
        self.assertIsNone(q.get_for_output())
        q.put(AccessUnit(b"NEXT", FLAG_KEY_FRAME, receiver_module.time.monotonic()))
        self.assertEqual(q.get_for_output().payload, b"NEXT")

    @mock.patch.object(receiver_module.subprocess, "Popen")
    def test_ffplay_gets_info_rate_and_low_latency_options(self, popen: mock.Mock) -> None:
        process = mock.Mock()
        process.stdin = mock.Mock()
        popen.return_value = process
        sink = FfplaySink("ffplay")
        sink.start(60.0)
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("-framerate") + 1], "60")
        self.assertEqual(command[command.index("-probesize") + 1], "32")
        self.assertEqual(command[command.index("-analyzeduration") + 1], "0")
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.PIPE)


class _FakeBulkLib:
    def __init__(self, responses: list[tuple[int, bytes]]) -> None:
        self.responses = iter(responses)
        self.calls: list[tuple[bytes, int, int]] = []

    def libusb_bulk_transfer(
        self,
        _handle: object,
        _endpoint: int,
        buffer: object,
        size: int,
        transferred: object,
        _timeout_ms: int,
    ) -> int:
        code, payload = next(self.responses)
        moved = min(len(payload), size)
        for index, value in enumerate(payload[:moved]):
            buffer[index] = value
        transferred._obj.value = moved
        self.calls.append((bytes(buffer[:size]), moved, code))
        return code


class BulkTransferTests(unittest.TestCase):
    def _usb(self, fake_lib: _FakeBulkLib) -> tuple[LibUSB, AccessoryConnection]:
        usb = LibUSB.__new__(LibUSB)
        usb.lib = fake_lib
        connection = AccessoryConnection(
            handle=object(),
            interface=BulkInterface(0, 0, 0x81, 0x02),
        )
        return usb, connection

    def test_partial_timeout_bulk_in_is_returned_to_parser(self) -> None:
        fake_lib = _FakeBulkLib([(LIBUSB_ERROR_TIMEOUT, b"GUSB")])
        usb, connection = self._usb(fake_lib)
        self.assertEqual(usb.bulk_in(connection), b"GUSB")

    def test_partial_timeout_bulk_out_retries_only_unsent_suffix(self) -> None:
        fake_lib = _FakeBulkLib(
            [(LIBUSB_ERROR_TIMEOUT, b"abc"), (0, b"def")]
        )
        usb, connection = self._usb(fake_lib)
        usb.bulk_out(connection, b"abcdef")
        self.assertEqual([call[0] for call in fake_lib.calls], [b"abcdef", b"def"])

    def test_empty_timeout_bulk_out_fails_without_replaying_frame(self) -> None:
        fake_lib = _FakeBulkLib([(LIBUSB_ERROR_TIMEOUT, b"")])
        usb, connection = self._usb(fake_lib)
        with self.assertRaises(USBError):
            usb.bulk_out(connection, b"frame")
        self.assertEqual(len(fake_lib.calls), 1)


if __name__ == "__main__":
    unittest.main()
