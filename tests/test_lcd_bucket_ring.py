"""Regression tests for the streamed-LCD bucket ring and HID read desyncs.

Hardware-free: a fake stands in for the pinned liquidctl Kraken Z3 driver and
models the two firmware behaviours that broke LCD streaming in the field.

* The bucket currently on screen cannot be deleted (the firmware answers 0x09),
  and a bucket whose delete was refused cannot then be set up (0x04).
* A reply can be crowded out of liquidctl's 12-report read budget by other
  traffic on the shared HID stream, which surfaces as
  ``AssertionError("missing messages ...")``.

Run with:  python3 -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openkraken.backend.device import KrakenDevice

_DESYNC = "missing messages (attempts=12, missing=1)"


class _FakeHidDevice:
    """Stands in for the hidapi handle, counting queue flushes."""

    def __init__(self) -> None:
        self.clears = 0

    def clear_enqueued_reports(self) -> None:
        self.clears += 1


class FakeKrakenDriver:
    """Minimal stand-in for the liquidctl driver's private upload primitives.

    ``undeletable`` holds the buckets the firmware refuses to delete, i.e. the
    one it is displaying (and, in one test, all of them).  ``delete_desyncs``
    counts how many leading ``_delete_bucket`` calls raise a read desync before
    the stream behaves.
    """

    bulk_buffer_size = 512
    orientation = 0

    def __init__(
        self,
        undeletable: set[int] | None = None,
        delete_desyncs: int = 0,
    ) -> None:
        self.undeletable = undeletable or set()
        self.pending_desyncs = delete_desyncs
        self.bulk_device = object()  # presence is all the uploader checks
        self.device = _FakeHidDevice()
        self.deleted: list[int] = []
        self.setups: list[int] = []
        self.switched: list[int] = []
        self._delete_ok: dict[int, bool] = {}
        #: Raised by :meth:`set_screen` when set, to model a failing control call.
        self.screen_error: Exception | None = None

    def set_screen(self, channel: str, mode: str, value) -> None:
        if self.screen_error is not None:
            raise self.screen_error

    def _prepare_static_file(self, path: str, orientation: int) -> bytes:
        return b"\x00" * 4096

    def _delete_bucket(self, index: int) -> bool:
        if self.pending_desyncs > 0:
            self.pending_desyncs -= 1
            raise AssertionError(_DESYNC)
        ok = index not in self.undeletable
        self.deleted.append(index)
        self._delete_ok[index] = ok
        return ok

    def _setup_bucket(self, start: int, end: int, address, size) -> bool:
        self.setups.append(start)
        # The firmware rejects setup on a bucket it just refused to delete.
        return self._delete_ok.get(start, True)

    def _switch_bucket(self, index: int) -> bool:
        self.switched.append(index)
        return True

    def _write_then_read(self, data):
        return [0x00] * 64

    def _write(self, data) -> None:
        pass

    def _bulk_write(self, data) -> None:
        pass


class LcdBucketRingTest(unittest.TestCase):
    def _connected_device(self, **kwargs) -> tuple[KrakenDevice, FakeKrakenDriver]:
        dev = KrakenDevice()
        driver = FakeKrakenDriver(**kwargs)
        dev._dev = driver
        dev._connected = True
        # Panel is already in image/bucket mode, so frames take the ring path
        # instead of the full set_screen("static") re-establish.
        dev._lcd_stream_needs_reinit = False
        return dev, driver

    # ------------------------------------------------------- bucket ring
    def test_ring_advances_past_a_bucket_the_firmware_will_not_delete(self):
        """A refused delete must burn that slot, not pin the ring to it.

        Bucket 0 holds the image the panel is showing (the previous full
        set_screen("static") put it there), so the firmware answers 0x09 to a
        delete and 0x04 to the setup that follows.  The first frame is held, but
        the second must move to bucket 1 and land.
        """
        dev, driver = self._connected_device(undeletable={0})

        first = dev.set_lcd_sensor_frame("frame.png")
        second = dev.set_lcd_sensor_frame("frame.png")

        self.assertFalse(first, "frame on the displayed bucket cannot land")
        self.assertTrue(second, "next frame must use a different bucket and land")
        self.assertEqual([0, 1], driver.deleted, "ring must advance off bucket 0")
        self.assertEqual([1], driver.switched, "only the good frame reaches the panel")

    def test_setup_is_not_attempted_on_a_bucket_that_failed_to_delete(self):
        """Skip the doomed setup instead of provoking the firmware's 0x04."""
        dev, driver = self._connected_device(undeletable={0})

        dev.set_lcd_sensor_frame("frame.png")

        self.assertNotIn(0, driver.setups)

    def test_persistent_bucket_failure_still_forces_a_reconnect(self):
        """Genuinely wedged bucket memory must still escalate.

        When no bucket can be deleted the ring has nowhere to go, and only a
        reconnect clears it.  That escalation predates the ring-advance fix and
        must survive it.
        """
        dev, _ = self._connected_device(undeletable=set(range(6)))

        for _ in range(4):
            dev.set_lcd_sensor_frame("frame.png")

        self.assertFalse(dev.is_connected, "wedged bucket memory must reconnect")

    # ------------------------------------------------------- read desync
    def test_a_read_desync_holds_the_frame_but_keeps_the_connection(self):
        """A crowded-out reply is a stream desync, not a dead cooler.

        liquidctl signals it by raising AssertionError("missing messages").
        Tearing the connection down for that costs a full reconnect and a
        re-apply, and the reconnect's own init traffic provokes more desyncs.
        Hold the frame, flush the stale reports, stay connected.
        """
        dev, driver = self._connected_device(delete_desyncs=1)

        first = dev.set_lcd_sensor_frame("frame.png")

        self.assertFalse(first, "the desynced frame cannot land")
        self.assertTrue(dev.is_connected, "a desync must not drop the connection")
        self.assertGreater(driver.device.clears, 0, "stale reports must be flushed")

    def test_the_frame_after_a_desync_lands(self):
        """Recovery must be immediate, not deferred to a reconnect."""
        dev, _ = self._connected_device(delete_desyncs=1)

        dev.set_lcd_sensor_frame("frame.png")
        second = dev.set_lcd_sensor_frame("frame.png")

        self.assertTrue(second, "the next frame must land once the stream resyncs")

    def test_a_burst_of_desyncs_does_not_trip_the_bucket_wedge_threshold(self):
        """Desyncs must not be counted as wedged bucket memory.

        Sharing one counter meant a burst of desyncs forced a reconnect, and the
        reconnect's init traffic produced the next burst.  A desync burst is
        normal after a device-side error storm and must ride out on flushes.
        """
        dev, _ = self._connected_device(delete_desyncs=6)

        for _ in range(6):
            dev.set_lcd_sensor_frame("frame.png")

        self.assertTrue(dev.is_connected, "a desync burst must not reconnect")

    # ------------------------------------------------------- control path
    def test_set_screen_flushes_the_stream_after_a_read_desync(self):
        """The control path must resync too, not just the frame streamer.

        Brightness, orientation and the liquid/static re-establish all go
        through set_screen.  Leaving the crowded reports queued makes the next
        call fail the same way, which is what stretched startup recovery into
        minutes of repeated rejections.
        """
        dev, driver = self._connected_device()
        driver.screen_error = AssertionError(_DESYNC)

        ok = dev.set_lcd_brightness(50)

        self.assertFalse(ok, "the desynced call cannot be reported as applied")
        self.assertTrue(dev.is_connected, "a desync must not drop the connection")
        self.assertGreater(driver.device.clears, 0, "stale reports must be flushed")

    def test_set_screen_does_not_flush_for_a_content_error(self):
        """An oversized GIF or missing file is not a stream problem."""
        dev, driver = self._connected_device()
        driver.screen_error = AssertionError("Max file size after resize is 24MB")

        ok = dev.set_lcd_brightness(50)

        self.assertFalse(ok)
        self.assertEqual(0, driver.device.clears, "content errors need no flush")


if __name__ == "__main__":
    unittest.main()
