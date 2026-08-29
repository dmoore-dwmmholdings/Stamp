"""The feature tree - spec §7, left panel.

Base part pinned at the top, features in application order, drag to reorder, a
checkbox to suppress, an icon per operation kind, and a warning badge on anything
that failed.
Double-click renames.  Right-click gives duplicate, mirror, and delete.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QMenu,
    QTreeWidget,
    QTreeWidgetItem,
)

from stamp.core.document import Document, Feature, ModifierKind, OperationKind

ADD_ICON = "↗"  # north east arrow - material added
CUT_ICON = "↘"  # south east arrow - material removed
STAMP_ICON = "◈"  # a filled shape inside an outline - a colour inlay, flush
WARN_ICON = "⚠"
BROKEN_ICON = "✖"

ROLE_FEATURE_ID = Qt.ItemDataRole.UserRole + 1
ROLE_MODIFIER_ID = Qt.ItemDataRole.UserRole + 2
ROLE_KIND = Qt.ItemDataRole.UserRole + 3

WARN_COLOR = QColor("#c58a2a")
BROKEN_COLOR = QColor("#c0453a")
MUTED_COLOR = QColor("#8a8f98")


class FeatureTree(QTreeWidget):
    """Signals carry ids, never rows - the model is the document, not the widget."""

    feature_selected = Signal(str)          # feature id, or "" for the base part
    modifier_selected = Signal(str, str)    # feature id, modifier id
    enabled_toggled = Signal(str, bool)
    renamed = Signal(str, str)
    reordered = Signal(str, int)            # feature id, new index
    duplicate_requested = Signal(str)
    delete_requested = Signal(str)
    mirror_requested = Signal(str)
    delete_modifier_requested = Signal(str, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setColumnCount(1)
        self.setRootIsDecorated(True)
        self.setIndentation(14)
        self.setIconSize(QSize(14, 14))
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.header().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

        self._document: Document | None = None
        self._results = {}
        self._updating = False

        self.itemChanged.connect(self._on_item_changed)
        self.itemSelectionChanged.connect(self._on_selection_changed)
        self.customContextMenuRequested.connect(self._on_context_menu)
        self.model().rowsMoved.connect(self._on_rows_moved)

    # ------------------------------------------------------------------ filling

    def set_document(self, document: Document | None, results: dict | None = None) -> None:
        """Rebuild the tree.  Keeps the selection when the same feature still exists."""
        self._document = document
        self._results = results or {}
        selected = self.selected_feature_id()

        self._updating = True
        self.clear()
        if document is not None:
            self._add_base_item(document)
            for feature in document.features:
                self._add_feature_item(feature)
        self._updating = False

        self.expandAll()
        if selected:
            self.select_feature(selected)

    def _add_base_item(self, document: Document) -> QTreeWidgetItem:
        base = document.base
        item = QTreeWidgetItem(self)
        if base is None:
            item.setText(0, "No part loaded")
        else:
            from pathlib import Path

            name = Path(base.source_path).stem or "part"
            item.setText(0, f"{name}  ({base.mode})")
        font = QFont(self.font())
        font.setBold(True)
        item.setFont(0, font)
        item.setData(0, ROLE_KIND, "base")
        item.setData(0, ROLE_FEATURE_ID, "")
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        return item

    def _add_feature_item(self, feature: Feature) -> QTreeWidgetItem:
        item = QTreeWidgetItem(self)
        icon = {
            OperationKind.ADD: ADD_ICON,
            OperationKind.COLOR: STAMP_ICON,
        }.get(feature.operation.kind, CUT_ICON)
        item.setText(0, f"{icon}  {feature.name}")
        item.setData(0, ROLE_KIND, "feature")
        item.setData(0, ROLE_FEATURE_ID, feature.id)
        item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsEditable
            | Qt.ItemFlag.ItemIsDragEnabled
        )
        item.setCheckState(
            0, Qt.CheckState.Checked if feature.enabled else Qt.CheckState.Unchecked
        )

        result = self._results.get(feature.id)
        if result is not None and getattr(result, "broken", False):
            item.setForeground(0, QBrush(BROKEN_COLOR))
            item.setText(0, f"{BROKEN_ICON} {item.text(0)}")
            item.setToolTip(0, "\n".join(result.errors))
        elif result is not None and result.warnings:
            item.setForeground(0, QBrush(WARN_COLOR))
            item.setToolTip(0, "\n".join(result.warnings))
        elif not feature.enabled:
            item.setForeground(0, QBrush(MUTED_COLOR))

        for modifier in feature.modifiers:
            child = QTreeWidgetItem(item)
            mark = "R" if modifier.kind is ModifierKind.FILLET else ""
            child.setText(
                0,
                f"↳ {modifier.kind.value} {mark}{modifier.value:g} "
                f"({modifier.target.role.value})",
            )
            child.setData(0, ROLE_KIND, "modifier")
            child.setData(0, ROLE_FEATURE_ID, feature.id)
            child.setData(0, ROLE_MODIFIER_ID, modifier.id)
            child.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            if result is not None and modifier.id in getattr(result, "suggested_values", {}):
                child.setForeground(0, QBrush(WARN_COLOR))
                child.setText(0, f"{WARN_ICON} {child.text(0)}")
                suggested = result.suggested_values[modifier.id]
                child.setToolTip(0, f"A value of {suggested:.3f} mm works on every edge.")
        return item

    # ---------------------------------------------------------------- selection

    def selected_feature_id(self) -> str:
        items = self.selectedItems()
        if not items:
            return ""
        return items[0].data(0, ROLE_FEATURE_ID) or ""

    def select_feature(self, feature_id: str) -> None:
        for i in range(self.topLevelItemCount()):
            item = self.topLevelItem(i)
            if item.data(0, ROLE_FEATURE_ID) == feature_id:
                self.setCurrentItem(item)
                return

    def _on_selection_changed(self) -> None:
        if self._updating:
            return
        items = self.selectedItems()
        if not items:
            return
        item = items[0]
        kind = item.data(0, ROLE_KIND)
        if kind == "modifier":
            self.modifier_selected.emit(
                item.data(0, ROLE_FEATURE_ID), item.data(0, ROLE_MODIFIER_ID)
            )
        else:
            self.feature_selected.emit(item.data(0, ROLE_FEATURE_ID) or "")

    # ------------------------------------------------------------------- edits

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._updating or item.data(0, ROLE_KIND) != "feature":
            return
        feature_id = item.data(0, ROLE_FEATURE_ID)

        enabled = item.checkState(0) == Qt.CheckState.Checked
        feature = self._document.feature_by_id(feature_id) if self._document else None
        if feature is not None and feature.enabled != enabled:
            self.enabled_toggled.emit(feature_id, enabled)
            return

        # A rename arrives as a text change; strip the icon prefix the view adds.
        text = item.text(0)
        for prefix in (f"{BROKEN_ICON} ", ADD_ICON, CUT_ICON):
            text = text.replace(prefix, "")
        text = text.strip()
        if feature is not None and text and text != feature.name:
            self.renamed.emit(feature_id, text)

    def _on_rows_moved(self, parent, start, end, destination, row) -> None:
        if self._updating or self._document is None:
            return
        item = self.topLevelItem(min(row, self.topLevelItemCount() - 1))
        if item is None:
            return
        feature_id = item.data(0, ROLE_FEATURE_ID)
        if not feature_id:
            return
        # The base part is pinned at index 0, so feature indices are one less.
        new_index = max(0, self.indexOfTopLevelItem(item) - 1)
        self.reordered.emit(feature_id, new_index)

    # ------------------------------------------------------------ context menu

    def _on_context_menu(self, position) -> None:
        item = self.itemAt(position)
        if item is None:
            return
        kind = item.data(0, ROLE_KIND)
        feature_id = item.data(0, ROLE_FEATURE_ID)
        if not feature_id:
            return

        menu = QMenu(self)
        if kind == "modifier":
            modifier_id = item.data(0, ROLE_MODIFIER_ID)
            action = QAction("Delete", menu)
            action.triggered.connect(
                lambda: self.delete_modifier_requested.emit(feature_id, modifier_id)
            )
            menu.addAction(action)
        else:
            duplicate = QAction("Duplicate", menu)
            duplicate.triggered.connect(lambda: self.duplicate_requested.emit(feature_id))
            menu.addAction(duplicate)

            mirror = QAction("Mirror across the sketch plane", menu)
            mirror.triggered.connect(lambda: self.mirror_requested.emit(feature_id))
            menu.addAction(mirror)

            rename = QAction("Rename", menu)
            rename.triggered.connect(lambda: self.editItem(item, 0))
            menu.addAction(rename)

            menu.addSeparator()
            delete = QAction("Delete", menu)
            delete.triggered.connect(lambda: self.delete_requested.emit(feature_id))
            menu.addAction(delete)

        menu.exec(self.viewport().mapToGlobal(position))


__all__ = ["FeatureTree"]
