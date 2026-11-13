"""Copying out of the library: Export (playlists/audiobooks to a device)
and Copy (selected tracks to a folder)."""

from pathlib import Path

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from ..library import db as libdb
from ..library.export import (
    CopyReport,
    CopyTracksWorker,
    ExportItem,
    ExportReport,
    ExportWorker,
    list_audiobook_entries,
)
from ..widgets import (
    LevelGauge,
)
from ..workers import sanitize_filename


class ExportDialog(QDialog):
    """Copies selected playlists / audiobook entries out of the library onto
    an external device (or a staging folder for something like iTunes to
    import from). Opened either from LibraryView (nothing preselected) or
    from PlaylistView (that one playlist preselected)."""

    def __init__(self, parent, library_root: str, preselected_playlist_id: int | None = None):
        super().__init__(parent)
        self.library_root = library_root
        self.settings = QSettings("Thomas", "Splice")

        title = "Export"
        preselect_row = None

        self.resize(560, 560)
        layout = QVBoxLayout(self)

        hint = QLabel(
            "Check the playlists and/or audiobooks you want to copy out. Each one lands "
            "in its own folder at the destination, in the right play order — for devices "
            "like the Shokz that have no playlist support and just play files in folder order."
        )
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        layout.addWidget(hint)

        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget, stretch=1)

        conn = libdb.open_db(library_root)
        playlists = libdb.list_playlists(conn)
        conn.close()
        audiobooks = list_audiobook_entries(library_root)

        if playlists:
            self._add_header("Playlists")
            for p in playlists:
                item = self._add_checkable(p["name"], ExportItem("playlist", p["id"], p["name"]))
                if preselected_playlist_id is not None and p["id"] == preselected_playlist_id:
                    preselect_row = item
                    title = f"Export — {p['name']}"

        if audiobooks:
            self._add_header("Audiobooks")
            for rel_path, name in audiobooks:
                self._add_checkable(name, ExportItem("audiobook", rel_path, name))

        if preselect_row is not None:
            preselect_row.setCheckState(Qt.CheckState.Checked)

        self.setWindowTitle(title)

        dest_row = QHBoxLayout()
        self.dest_edit = QLineEdit()
        self.dest_edit.setReadOnly(True)
        self.dest_edit.setText(str(self.settings.value("export_destination", "") or ""))
        dest_btn = QPushButton("Choose…")
        dest_btn.clicked.connect(self._choose_destination)
        dest_row.addWidget(self.dest_edit, stretch=1)
        dest_row.addWidget(dest_btn)
        layout.addLayout(dest_row)

        self.replace_check = QCheckBox("Replace existing contents of each selected item's destination folder")
        layout.addWidget(self.replace_check)
        replace_hint = QLabel("Only touches that one subfolder per item — never anything else at the destination.")
        replace_hint.setObjectName("muted")
        replace_hint.setWordWrap(True)
        layout.addWidget(replace_hint)

        action_row = QHBoxLayout()
        self.export_btn = QPushButton("Export")
        self.export_btn.setObjectName("primary")
        self.export_btn.clicked.connect(self._run_export)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_export)
        action_row.addWidget(self.export_btn)
        action_row.addStretch(1)
        action_row.addWidget(self.cancel_btn)
        layout.addLayout(action_row)

        self.gauge = LevelGauge()
        layout.addWidget(self.gauge)

        self.report_label = QLabel("")
        self.report_label.setObjectName("muted")
        self.report_label.setWordWrap(True)
        layout.addWidget(self.report_label)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        layout.addWidget(self.log_box, stretch=1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

    def _add_header(self, text: str):
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        self.list_widget.addItem(item)

    def _add_checkable(self, label: str, export_item: ExportItem) -> QListWidgetItem:
        item = QListWidgetItem(f"    {label}")
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Unchecked)
        item.setData(Qt.ItemDataRole.UserRole, export_item)
        self.list_widget.addItem(item)
        return item

    def _choose_destination(self):
        start_dir = self.dest_edit.text() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose export destination", start_dir)
        if chosen:
            self.dest_edit.setText(chosen)

    def _log(self, msg: str):
        self.log_box.appendPlainText(msg)

    def _collect_selected_items(self) -> list[ExportItem]:
        items = []
        for i in range(self.list_widget.count()):
            row = self.list_widget.item(i)
            if row.flags() & Qt.ItemFlag.ItemIsUserCheckable and row.checkState() == Qt.CheckState.Checked:
                items.append(row.data(Qt.ItemDataRole.UserRole))
        return items

    def _run_export(self):
        items = self._collect_selected_items()
        if not items:
            QMessageBox.warning(self, "Nothing selected", "Check at least one playlist or audiobook to export.")
            return

        destination = self.dest_edit.text().strip()
        if not destination or not Path(destination).exists():
            QMessageBox.warning(self, "No destination", "Choose a destination folder first.")
            return

        seen_names = {}
        for item in items:
            sanitized = sanitize_filename(item.name)
            if sanitized in seen_names:
                QMessageBox.warning(
                    self, "Naming collision",
                    f"'{item.name}' and '{seen_names[sanitized]}' would both export to the folder "
                    f"'{sanitized}' — rename one or deselect it.",
                )
                return
            seen_names[sanitized] = item.name

        self.settings.setValue("export_destination", destination)

        self.export_btn.setEnabled(False)
        self.list_widget.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.gauge.set_idle()
        self.report_label.setText("")

        self._worker = ExportWorker(self.library_root, destination, items, self.replace_check.isChecked())
        self._worker.log.connect(self._log)
        self._worker.progress.connect(self.gauge.set_progress)
        self._worker.finished_ok.connect(self._on_export_done)
        self._worker.cancelled.connect(self._on_export_cancelled)
        self._worker.start()

    def _cancel_export(self):
        if hasattr(self, "_worker"):
            self._worker.cancel()

    def _on_export_done(self, report: ExportReport):
        self.gauge.set_done("Export complete")
        self.export_btn.setEnabled(True)
        self.list_widget.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        text = (
            f"{report.items_exported} item(s) exported ({report.items_replaced} replaced), "
            f"{report.copied_count} file(s) copied, {report.skipped_count} missing, "
            f"{report.failed_count} failed."
        )
        if report.skipped_count or report.failed_count:
            text += " See the log below for detail."
        self.report_label.setText(text)

    def _on_export_cancelled(self):
        self.gauge.set_cancelled("Cancelled")
        self.export_btn.setEnabled(True)
        self.list_widget.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.report_label.setText("Export cancelled — items already copied before cancelling are kept.")


class CopyTracksDialog(QDialog):
    """Ad-hoc copy of whatever tracks were multi-selected in the Songs/
    Audiobooks table into a folder the user picks — flat, no per-item
    subfolder (that's what the full Export dialog is for)."""

    def __init__(self, parent, library_root: str, library_paths: list[str]):
        super().__init__(parent)
        self.library_root = library_root
        self.library_paths = library_paths
        self.setWindowTitle(f"Copy {len(library_paths)} Track(s)")
        self.resize(480, 380)

        layout = QVBoxLayout(self)
        hint = QLabel(f"Copying {len(library_paths)} track(s) as flat files into a folder you choose.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        dest_row = QHBoxLayout()
        self.dest_edit = QLineEdit()
        self.dest_edit.setReadOnly(True)
        dest_btn = QPushButton("Choose…")
        dest_btn.clicked.connect(self._choose_destination)
        dest_row.addWidget(self.dest_edit, stretch=1)
        dest_row.addWidget(dest_btn)
        layout.addLayout(dest_row)

        action_row = QHBoxLayout()
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setObjectName("primary")
        self.copy_btn.clicked.connect(self._run_copy)
        action_row.addWidget(self.copy_btn)
        layout.addLayout(action_row)

        self.gauge = LevelGauge()
        layout.addWidget(self.gauge)

        self.report_label = QLabel("")
        self.report_label.setObjectName("muted")
        self.report_label.setWordWrap(True)
        layout.addWidget(self.report_label)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        layout.addWidget(self.log_box, stretch=1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

    def _choose_destination(self):
        chosen = QFileDialog.getExistingDirectory(self, "Choose destination folder", str(Path.home()))
        if chosen:
            self.dest_edit.setText(chosen)

    def _log(self, msg: str):
        self.log_box.appendPlainText(msg)

    def _run_copy(self):
        destination = self.dest_edit.text().strip()
        if not destination or not Path(destination).exists():
            QMessageBox.warning(self, "No destination", "Choose a destination folder first.")
            return
        self.copy_btn.setEnabled(False)
        self.gauge.set_idle()
        self.report_label.setText("")
        self._worker = CopyTracksWorker(self.library_root, self.library_paths, destination)
        self._worker.log.connect(self._log)
        self._worker.progress.connect(self.gauge.set_progress)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.start()

    def _on_done(self, report: CopyReport):
        self.gauge.set_done("Copy complete")
        self.copy_btn.setEnabled(True)
        text = f"{report.copied_count} copied, {report.failed_count} failed."
        if report.failed_count:
            text += " See the log below for detail."
        self.report_label.setText(text)
