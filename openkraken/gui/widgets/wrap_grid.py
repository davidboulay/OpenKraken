"""A container that reflows its children into as many columns as fit.

Qt ships no flow layout, and a ``QHBoxLayout`` cannot wrap: five gauges with a
150 px minimum each pinned the Dashboard to an 830 px floor, and a tiling
compositor hands out whatever width it likes regardless of size hints. Below
that floor the row overflowed the window edge — labels sheared off mid-word —
instead of adapting.

:class:`WrapGrid` fills a ``QGridLayout`` and recomputes the column count from
the width it is actually given, so five gauges become 5x1, then 4x2, then 3x2
as the window narrows — and no further, see ``max_rows``.

**Why the height is pinned rather than left to ``heightForWidth``.** The
textbook answer is to implement ``heightForWidth`` and set
``QSizePolicy.setHeightForWidth(True)``, so the parent learns that a narrower
widget needs more height. ``QBoxLayout`` does not honour that for child
widgets, though — it allocated the single-row height, and the wrapped rows were
not merely clipped, the children vanished outright. Since the row count follows
deterministically from the column count, this sets an explicit fixed height on
every re-flow instead, which any layout respects.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QGridLayout, QSizePolicy, QWidget


class WrapGrid(QWidget):
    """Lay children out in a grid whose column count follows the width.

    Parameters
    ----------
    min_item_width:
        Narrowest width an item may be given. The column count is the largest
        number of columns that keeps every item at or above this width.
    spacing:
        Gap between items, horizontally and vertically.
    max_rows:
        Ceiling on how far wrapping may go. Without one, a very narrow window
        wraps five gauges into five rows and needs ~900 px of height — trading
        the horizontal overflow for a vertical one, which on a short tile is no
        better. At the floor the items compress instead of wrapping further.
    """

    def __init__(
        self,
        min_item_width: int,
        spacing: int = 12,
        max_rows: int = 2,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._min_item_width = max(1, int(min_item_width))
        self._max_rows = max(1, int(max_rows))
        self._items: list[QWidget] = []
        self._cols = 0

        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(spacing)

        # Horizontally elastic, vertically exactly as tall as the current
        # arrangement needs (set in _apply).
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    # ------------------------------------------------------------------ API
    def add_widget(self, widget: QWidget) -> None:
        """Append *widget* and re-flow."""
        self._items.append(widget)
        self._apply(self._column_count(self.width()), force=True)

    # -------------------------------------------------------------- layout
    def _column_count(self, width: int) -> int:
        """Columns that fit in *width*, clamped by ``max_rows`` and item count."""
        count = len(self._items)
        if not count:
            return 1
        spacing = self._grid.spacing()
        # n columns occupy n*min + (n-1)*spacing; solve for the largest n.
        fitting = (max(width, 0) + spacing) // (self._min_item_width + spacing)
        # Never wrap past max_rows: that is the column count below which the
        # items must compress rather than form another row.
        floor = -(-count // self._max_rows)  # ceiling division
        return max(floor, min(int(fitting), count))

    def _rows_for(self, cols: int) -> int:
        if not self._items:
            return 0
        return -(-len(self._items) // cols)  # ceiling division

    @staticmethod
    def _wanted_height(widget: QWidget) -> int:
        """Height *widget* needs, from whichever source actually knows it.

        ``minimumSizeHint()`` is not ``minimumSize()``: a painted widget with no
        layout of its own (the gauges) reports an empty hint even after an
        explicit ``setMinimumSize``, while a laid-out one (a checkbox) reports a
        useful hint but no explicit minimum. Taking the largest of the three
        covers both without special-casing either.
        """
        return max(
            widget.minimumSize().height(),
            widget.minimumSizeHint().height(),
            widget.sizeHint().height(),
        )

    def _row_height(self) -> int:
        """Height one row needs: the tallest item in it."""
        return max((self._wanted_height(w) for w in self._items), default=0)

    def _apply(self, cols: int, *, force: bool = False) -> None:
        """Re-place every item for a *cols*-wide grid, if that changed."""
        if cols == self._cols and not force:
            return
        self._cols = cols
        for widget in self._items:
            self._grid.removeWidget(widget)
        for index, widget in enumerate(self._items):
            self._grid.addWidget(widget, index // cols, index % cols)
        # Share the width evenly between live columns, and retire stale ones so
        # a former 5-column layout leaves no stretch behind at 2 columns.
        for column in range(max(self._grid.columnCount(), cols)):
            self._grid.setColumnStretch(column, 1 if column < cols else 0)

        rows = self._rows_for(cols)
        height = rows * self._row_height() + max(rows - 1, 0) * self._grid.spacing()
        self.setFixedHeight(height)

    # ------------------------------------------------------- Qt overrides
    def minimumSizeHint(self):  # noqa: N802 - Qt naming
        """One item wide, so a tiler can shrink us without clipping."""
        hint = super().minimumSizeHint()
        hint.setWidth(self._min_item_width)
        return hint

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._apply(self._column_count(event.size().width()))
