"""Regression tests for WrapGrid, the Dashboard's re-flowing container.

Hardware-free, but it does need a Qt platform: the column count is a function
of the width a real layout hands the widget. The offscreen platform plugin is
enough, and the whole module skips if Qt cannot start at all (a headless box
without qt6-base, say).

Run with:  python3 -m unittest discover -s tests
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication, QWidget

    from openkraken.gui.widgets.wrap_grid import WrapGrid
except Exception as exc:  # pragma: no cover - no Qt available
    raise unittest.SkipTest(f"PyQt6 unavailable: {exc}") from exc


class _Item(QWidget):
    """An item with an explicit minimum, like the gauges have."""

    def __init__(self, w: int = 120, h: int = 170) -> None:
        super().__init__()
        self.setMinimumSize(w, h)


class WrapGridTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _grid(self, count: int = 5, **kwargs) -> WrapGrid:
        grid = WrapGrid(kwargs.pop("min_item_width", 120), **kwargs)
        for _ in range(count):
            grid.add_widget(_Item())
        return grid

    def test_columns_follow_the_width(self):
        """Five 120px items: five across when wide, fewer as it narrows."""
        grid = self._grid(5, max_rows=5)
        # n columns need n*120 + (n-1)*12
        self.assertEqual(grid._column_count(5 * 120 + 4 * 12), 5)
        self.assertEqual(grid._column_count(3 * 120 + 2 * 12), 3)
        self.assertEqual(grid._column_count(120), 1)

    def test_never_more_columns_than_items(self):
        grid = self._grid(5, max_rows=5)
        self.assertEqual(grid._column_count(10_000), 5)

    def test_max_rows_stops_the_wrapping(self):
        """Past the floor the items must compress, not form another row.

        Unbounded wrapping turned a narrow window into a ~900px-tall gauge
        stack, i.e. it traded a horizontal overflow for a vertical one.
        """
        grid = self._grid(5, max_rows=2)
        # Even at an absurdly narrow width, 5 items over 2 rows needs 3 columns.
        self.assertEqual(grid._column_count(1), 3)
        self.assertEqual(grid._rows_for(grid._column_count(1)), 2)

    def test_zero_width_does_not_divide_by_zero(self):
        grid = self._grid(5)
        self.assertGreaterEqual(grid._column_count(0), 1)

    def test_empty_grid_is_harmless(self):
        grid = WrapGrid(120)
        self.assertEqual(grid._column_count(500), 1)
        self.assertEqual(grid._rows_for(1), 0)

    def test_height_is_pinned_to_the_arrangement(self):
        """QBoxLayout ignores heightForWidth, so the height must be explicit.

        Without this the wrapped rows were not merely clipped — the children
        vanished, because the parent allocated a single row's height.
        """
        grid = self._grid(5, max_rows=2)
        grid._apply(5, force=True)
        one_row = grid.height()
        grid._apply(3, force=True)
        two_rows = grid.height()
        self.assertEqual(one_row, 170)
        self.assertEqual(two_rows, 2 * 170 + 12)
        self.assertGreater(two_rows, one_row)

    def test_row_height_sees_an_explicit_minimum(self):
        """minimumSizeHint() is not minimumSize(); the gauges only set the latter."""
        grid = self._grid(1)
        self.assertEqual(grid._row_height(), 170)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
