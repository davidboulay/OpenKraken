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
    the stream behaves, and ``screen_desyncs`` does the same for
    :meth:`set_screen` -- a *transient* crowded stream, as opposed to
    ``screen_error``, which keeps failing for as long as it is set.
    """

    bulk_buffer_size = 512
    orientation = 0

    def __init__(
        self,
        undeletable: set[int] | None = None,
        delete_desyncs: int = 0,
        screen_desyncs: int = 0,
    ) -> None:
        self.undeletable = undeletable or set()
        self.pending_desyncs = delete_desyncs
        self.pending_screen_desyncs = screen_desyncs
        self.screen_calls = 0
        self.bulk_device = object()  # presence is all the uploader checks
        self.device = _FakeHidDevice()
        self.deleted: list[int] = []
        self.setups: list[int] = []
        self.switched: list[int] = []
        self._delete_ok: dict[int, bool] = {}
        #: Raised by :meth:`set_screen` when set, to model a failing control call.
        self.screen_error: Exception | None = None

    def set_screen(self, channel: str, mode: str, value) -> None:
        self.screen_calls += 1
        if self.pending_screen_desyncs > 0:
            self.pending_screen_desyncs -= 1
            raise AssertionError(_DESYNC)
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
    def _connected_device(
        self, product_id: int | None = 0x3012, stream: str = "auto", **kwargs
    ) -> tuple[KrakenDevice, FakeKrakenDriver]:
        dev = KrakenDevice()
        driver = FakeKrakenDriver(**kwargs)
        dev._dev = driver
        dev._connected = True
        # The ring streamer is only used on the Kraken 2024 Elite in "auto".
        dev.product_id = product_id
        dev.lcd_stream_mode = stream
        # Panel is already in image/bucket mode, so frames take the ring path
        # instead of the full set_screen("static") re-establish.
        dev._lcd_stream_needs_reinit = False
        return dev, driver

    @staticmethod
    def _reconnect(dev: KrakenDevice, driver: FakeKrakenDriver) -> None:
        """Model the engine re-opening the cooler after a disconnect."""
        dev._dev = driver
        dev._connected = True
        dev._lcd_stream_needs_reinit = False

    # ------------------------------------------------------- streaming path
    def test_auto_uses_the_ring_only_on_the_2024_elite(self):
        """Issue #8: the ring wedges a Z73's bucket memory; static is stable."""
        for product, path in ((0x3012, "ring"), (0x3008, "static"), (0x300E, "static"), (None, "static")):
            dev, driver = self._connected_device(product_id=product)
            self.assertEqual(path, dev.lcd_stream_path(), hex(product or 0))
            self.assertTrue(dev.set_lcd_sensor_frame("frame.png"))
            if path == "ring":
                self.assertEqual([0], driver.switched)
                self.assertEqual(0, driver.screen_calls)
            else:
                self.assertEqual([], driver.deleted, "static path must not touch the ring")
                self.assertEqual(1, driver.screen_calls, "frame goes through set_screen")

    def test_a_forced_mode_overrides_the_product_choice(self):
        dev, driver = self._connected_device(product_id=0x3008, stream="ring")
        self.assertTrue(dev.set_lcd_sensor_frame("frame.png"))
        self.assertEqual([0], driver.switched)

        dev, driver = self._connected_device(product_id=0x3012, stream="static")
        self.assertTrue(dev.set_lcd_sensor_frame("frame.png"))
        self.assertEqual(1, driver.screen_calls)
        self.assertEqual([], driver.deleted)

    def test_auto_falls_back_to_static_after_a_bucket_wedge(self):
        """Issue #8's loop: reconnect un-wedges the memory, then the ring wedges it again.

        After the wedge-triggered reconnect, "auto" must stop using the ring on
        this cooler and send frames through the full static upload instead.
        """
        dev, driver = self._connected_device(undeletable=set(range(6)))

        for _ in range(4):
            dev.set_lcd_sensor_frame("frame.png")
        self.assertFalse(dev.is_connected, "wedged bucket memory still reconnects")

        self._reconnect(dev, driver)
        self.assertEqual("static", dev.lcd_stream_path())
        self.assertTrue(dev.set_lcd_sensor_frame("frame.png"))
        self.assertEqual(1, driver.screen_calls)
        self.assertEqual(8, len(driver.deleted), "no further ring attempts (2 slots per frame)")

    def test_auto_falls_back_to_static_after_a_desync_streak(self):
        """Issue #9: a desync on every ring frame, while static uploads are fine."""
        dev, driver = self._connected_device(delete_desyncs=5)

        results = [dev.set_lcd_sensor_frame("frame.png") for _ in range(5)]

        self.assertEqual([False] * 5, results)
        self.assertTrue(dev.is_connected, "a desync streak is not a dead cooler")
        self.assertEqual("static", dev.lcd_stream_path())
        self.assertTrue(dev.set_lcd_sensor_frame("frame.png"))
        self.assertEqual(1, driver.screen_calls)

    def test_auto_skips_the_ring_while_the_kernel_driver_is_bound(self):
        """Issue #11: Linux 7.3's nzxt_kraken3 binds the 2024 Elite and garbles the ring."""
        dev, driver = self._connected_device(product_id=0x3012)
        dev.kernel_driver = "nzxt_kraken3"

        self.assertEqual("static", dev.lcd_stream_path())
        self.assertTrue(dev.set_lcd_sensor_frame("frame.png"))
        self.assertEqual(1, driver.screen_calls)
        self.assertEqual([], driver.deleted, "static path must not touch the ring")

        dev.lcd_stream_mode = "ring"
        self.assertEqual("ring", dev.lcd_stream_path(), "a forced ring still wins")

    def test_auto_falls_back_when_held_frames_interleave_with_good_ones(self):
        """Issue #11's log: desync, landed frame, refused setup, landed frame...

        No streak ever reaches the fallback, so it must count over a window.
        """
        dev, driver = self._connected_device()

        for i in range(10):
            if i % 2 == 0:
                driver.pending_desyncs = 1
            dev.set_lcd_sensor_frame("frame.png")

        self.assertTrue(dev.is_connected)
        self.assertEqual("static", dev.lcd_stream_path())

    def test_an_occasional_held_frame_keeps_the_ring(self):
        dev, driver = self._connected_device()

        for i in range(40):
            if i % 10 == 0:
                driver.pending_desyncs = 1
            dev.set_lcd_sensor_frame("frame.png")

        self.assertEqual("ring", dev.lcd_stream_path())

    def test_refused_deletes_alone_keep_the_ring(self):
        """A refused delete is the ring stepping past the displayed slot, not trouble."""
        dev, driver = self._connected_device(undeletable={1})

        for _ in range(30):
            dev.set_lcd_sensor_frame("frame.png")

        self.assertTrue(dev.is_connected)
        self.assertEqual("ring", dev.lcd_stream_path())

    def test_a_forced_ring_never_falls_back(self):
        dev, driver = self._connected_device(stream="ring", delete_desyncs=6)

        for _ in range(6):
            dev.set_lcd_sensor_frame("frame.png")

        self.assertEqual("ring", dev.lcd_stream_path())
        self.assertTrue(dev.set_lcd_sensor_frame("frame.png"))
        self.assertEqual(0, driver.screen_calls)

    # ------------------------------------------------------- bucket ring
    def test_ring_advances_past_a_bucket_the_firmware_will_not_delete(self):
        """A refused delete must burn that slot, not pin the ring to it.

        Bucket 0 holds the image the panel is showing (the previous full
        set_screen("static") put it there), so the firmware answers 0x09 to a
        delete and 0x04 to the setup that follows.  The frame steps to bucket 1
        in the same call and lands there (issue #11: abandoning it blanked the
        panel until the next frame).
        """
        dev, driver = self._connected_device(undeletable={0})

        first = dev.set_lcd_sensor_frame("frame.png")
        second = dev.set_lcd_sensor_frame("frame.png")

        self.assertTrue(first, "frame steps past the displayed bucket and lands")
        self.assertTrue(second)
        self.assertEqual([0, 1, 2], driver.deleted, "ring must advance off bucket 0")
        self.assertEqual([1, 2], driver.switched)

    def test_a_frame_is_held_when_the_next_bucket_is_refused_too(self):
        dev, driver = self._connected_device(undeletable={0, 1})

        self.assertFalse(dev.set_lcd_sensor_frame("frame.png"))
        self.assertTrue(dev.set_lcd_sensor_frame("frame.png"))
        self.assertEqual([2], driver.switched, "next frame advances past both")

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

    def test_a_transient_desync_is_retried_in_place(self):
        """A one-shot setting must survive a momentary crowded stream.

        Unlike a streamed frame, which the next tick re-sends anyway, brightness
        and orientation are applied once -- so losing the call loses the setting
        until something applies it again.  The flush is what makes the retry
        work: re-issuing without draining re-reads the same stale reports.
        """
        dev, driver = self._connected_device(screen_desyncs=1)

        ok = dev.set_lcd_brightness(50)

        self.assertTrue(ok, "the retried call must be reported as applied")
        self.assertEqual(driver.screen_calls, 2, "exactly one retry was needed")
        self.assertGreater(driver.device.clears, 0, "the retry must flush first")
        self.assertTrue(dev.is_connected)

    def test_a_content_error_is_not_retried(self):
        """The retry budget is for desyncs only, not for invalid input."""
        dev, driver = self._connected_device()
        driver.screen_error = AssertionError("Max file size after resize is 24MB")

        ok = dev.set_lcd_brightness(50)

        self.assertFalse(ok)
        self.assertEqual(driver.screen_calls, 1, "a validation error must fail at once")

    def test_set_screen_does_not_flush_for_a_content_error(self):
        """An oversized GIF or missing file is not a stream problem."""
        dev, driver = self._connected_device()
        driver.screen_error = AssertionError("Max file size after resize is 24MB")

        ok = dev.set_lcd_brightness(50)

        self.assertFalse(ok)
        self.assertEqual(0, driver.device.clears, "content errors need no flush")


if __name__ == "__main__":
    unittest.main()
