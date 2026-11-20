import os
import sys
import json
import csv
import subprocess
import tempfile
import re
import urllib.request
import hashlib
import time
import email.utils
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor

from PyQt6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QPushButton,
    QScrollArea,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QFileDialog,
    QMessageBox,
    QComboBox,
    QToolButton,
    QDialog,
    QListWidget,
    QListWidgetItem,
    QDialogButtonBox,
    QStyle,
    QStyleOptionComboBox
)
from PyQt6.QtGui import QIcon, QColor, QPalette, QPainter
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QObject

# 网络请求统一超时（秒）。不要用 socket.setdefaulttimeout 改全局超时：
# 那样会波及所有套接字（包括 CSV 下载），慢网下必然失败。
NETWORK_TIMEOUT = 15
USER_AGENT = "BD2pineViewer/1.0 (+https://codeberg.org/kxdekxde/browndust2-spine-viewer)"

# ============================================================
# Codeberg configuration
# ============================================================

GITHUB_FILES = {
    "BD2 Characters - Characters.csv":
        "https://codeberg.org/kxdekxde/browndust2-spine-viewer/raw/branch/main/BD2%20Characters%20-%20Characters.csv"
}


# ============================================================
# Basic file functions
# ============================================================

def get_base_path():
    """Get the base path for the application."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(__file__)


class CenteredComboBox(QComboBox):
    """Custom QComboBox to keep text centered horizontally while closed."""
    def paintEvent(self, event):
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)

        opt.currentText = ""
        painter = QPainter(self)

        self.style().drawComplexControl(
            QStyle.ComplexControl.CC_ComboBox,
            opt,
            painter,
            self
        )

        text_rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            opt,
            QStyle.SubControl.SC_ComboBoxEditField,
            self
        )

        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            self.currentText()
        )


class NoAutoSelectLineEdit(QLineEdit):
    """获得焦点时不自动全选文本的输入框。

    QLineEdit 在通过窗口激活/Tab 获得焦点时会全选内容；Mods 路径框里的
    长路径被整段高亮时很扎眼，所以这里只在鼠标点击时保留 Qt 的默认行为。
    """

    def focusInEvent(self, event):
        super().focusInEvent(event)

        if event.reason() != Qt.FocusReason.MouseFocusReason:
            self.deselect()
            self.setCursorPosition(len(self.text()))


def download_file(url, destination):
    """把 URL 下载到 destination；先写临时文件再原子替换，避免留下半截文件。"""
    tmp_path = destination + ".download"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT) as response:
            with open(tmp_path, "wb") as out_file:
                shutil.copyfileobj(response, out_file)
        os.replace(tmp_path, destination)
        return True
    except Exception as e:
        print(f"Error downloading {url}: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return False


def get_remote_mtime(url):
    """通过 HTTP HEAD 请求获取远程文件的 Last-Modified 时间戳。"""
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", USER_AGENT)
        with urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT) as response:
            last_modified = response.headers.get("Last-Modified")
            if last_modified:
                parsed_date = email.utils.parsedate_to_datetime(last_modified)
                return parsed_date.timestamp()
    except Exception as e:
        print(f"Error checking remote mtime for {url}: {e}")
    return None


def check_for_updates():
    """当本地 CSV 不存在或更新时间早于远程文件时，返回 True 提示更新"""
    for filename, url in GITHUB_FILES.items():
        local_path = os.path.join(get_base_path(), filename)

        if not os.path.exists(local_path):
            return True

        local_mtime = os.path.getmtime(local_path)
        remote_mtime = get_remote_mtime(url)

        if remote_mtime is not None and remote_mtime > local_mtime:
            return True

    return False


def update_files_from_github():
    """下载更新文件。返回 (是否成功, 错误说明)。"""
    failures = []

    for filename, url in GITHUB_FILES.items():
        local_path = os.path.join(get_base_path(), filename)
        tmp_path = local_path + ".download"

        if not download_file(url, tmp_path):
            failures.append(f"{filename}: 下载失败")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            continue

        # CSV 必须能识别表头，否则宁可保留旧文件，也不要用坏文件覆盖。
        if filename.lower().endswith(".csv"):
            try:
                with open(tmp_path, "r", encoding="utf-8-sig", newline="") as csv_file:
                    header = csv_file.readline()
                if "CHARACTER" not in header or "ID" not in header:
                    raise ValueError("CSV 表头缺少 CHARACTER/ID")
            except Exception as e:
                failures.append(f"{filename}: 文件校验失败 ({e})")
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                continue

        try:
            os.replace(tmp_path, local_path)
            remote_mtime = get_remote_mtime(url)
            current_time = remote_mtime if remote_mtime else time.time()
            os.utime(local_path, (current_time, current_time))
        except Exception as e:
            failures.append(f"{filename}: 写入失败 ({e})")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass

    return (not failures, "; ".join(failures))


# ============================================================
# Spine Viewer controller
# ============================================================

class SpineViewerController:

    def __init__(self):
        self.viewer_process = None

        self.viewer_path = os.path.join(
            get_base_path(),
            "SpineViewer-anosu",
            "SpineViewer.exe"
        )

    def launch_viewer(self, skel_path=None):
        """Launch the Spine viewer with optional skeleton file."""
        try:
            if os.path.exists(self.viewer_path):

                if skel_path:
                    self.viewer_process = subprocess.Popen(
                        [self.viewer_path, skel_path]
                    )
                else:
                    self.viewer_process = subprocess.Popen(
                        [self.viewer_path]
                    )

                return True

            print(
                f"Spine viewer not found at: {self.viewer_path}"
            )

            return False

        except Exception as e:
            print(f"Error launching viewer: {e}")
            return False

    def close_viewer(self):
        """Close the viewer."""
        if (
            self.viewer_process
            and self.viewer_process.poll() is None
        ):
            self.viewer_process.terminate()


# ============================================================
# Whitelist dialog
# ============================================================

class WhitelistDialog(QDialog):

    def __init__(self, parent, mods_folder, whitelist):
        super().__init__(parent)

        self.mods_folder = os.path.abspath(
            os.path.normpath(mods_folder)
        )

        self.whitelist = list(whitelist)

        self.setWindowTitle("Switch Whitelist")
        self.setMinimumSize(550, 400)

        layout = QVBoxLayout(self)

        info_label = QLabel(
            "Folders in this list will not be classified by Switch."
        )
        info_label.setWordWrap(True)

        layout.addWidget(info_label)

        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget)

        button_layout = QHBoxLayout()

        add_btn = QPushButton("Add Folder")
        add_btn.clicked.connect(self.add_folder)

        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self.remove_folder)

        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.clear_list)

        button_layout.addWidget(add_btn)
        button_layout.addWidget(remove_btn)
        button_layout.addWidget(clear_btn)
        button_layout.addStretch()

        layout.addLayout(button_layout)

        dialog_buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )

        dialog_buttons.accepted.connect(self.accept)
        dialog_buttons.rejected.connect(self.reject)

        layout.addWidget(dialog_buttons)

        self.refresh_list()

    def normalize_path(self, path):
        return os.path.abspath(
            os.path.normpath(path)
        )

    def is_inside_mods_folder(self, path):
        path = self.normalize_path(path)
        root = self.normalize_path(self.mods_folder)

        try:
            return os.path.commonpath(
                [path, root]
            ) == root
        except ValueError:
            return False

    def refresh_list(self):
        self.list_widget.clear()

        for path in self.whitelist:
            item = QListWidgetItem(path)
            self.list_widget.addItem(item)

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Folder to Add to Whitelist",
            self.mods_folder,
            QFileDialog.Option.ShowDirsOnly
        )

        if not folder:
            return

        folder = self.normalize_path(folder)

        if not self.is_inside_mods_folder(folder):
            QMessageBox.warning(
                self,
                "Invalid Folder",
                "The selected folder must be inside the Mods Folder.",
                QMessageBox.StandardButton.Ok
            )
            return

        if folder not in self.whitelist:
            self.whitelist.append(folder)
            self.refresh_list()

    def remove_folder(self):
        selected_items = self.list_widget.selectedItems()

        if not selected_items:
            return

        for item in selected_items:
            path = item.text()

            if path in self.whitelist:
                self.whitelist.remove(path)

        self.refresh_list()

    def clear_list(self):
        self.whitelist.clear()
        self.refresh_list()


# ============================================================
# Background update worker
# ============================================================

class UpdateCheckWorker(QObject):
    finished = pyqtSignal(bool)

    def run(self):
        try:
            self.finished.emit(check_for_updates())
        except Exception as e:
            print(f"Error checking for updates: {e}")
            self.finished.emit(False)


class UpdateDownloadWorker(QObject):
    """在后台线程下载 CSV，避免下载期间阻塞界面。"""

    finished = pyqtSignal(bool, str)

    def run(self):
        try:
            success, message = update_files_from_github()
        except Exception as e:
            success, message = False, str(e)
        self.finished.emit(success, message)


# ============================================================
# Rename / marker dialogs
# ============================================================

INVALID_FOLDER_CHARS = set('<>:"/\\|?*')
RESERVED_FOLDER_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def validate_folder_name(name, parent_dir, original_path=None):
    """校验文件夹名，返回 (错误信息, 新路径)；错误信息为 None 表示合法。"""
    if not name:
        return "Name cannot be empty.", None

    if name != name.strip():
        return "Name cannot start or end with a space.", None

    if name in (".", ".."):
        return "Name cannot be '.' or '..'.", None

    if any(ch in INVALID_FOLDER_CHARS for ch in name):
        return 'Name cannot contain any of: < > : " / \\ | ? *', None

    if any(ord(ch) < 32 for ch in name):
        return "Name cannot contain control characters.", None

    if name.endswith("."):
        return "Name cannot end with a dot.", None

    if name.split(".")[0].upper() in RESERVED_FOLDER_NAMES:
        return "That name is reserved by Windows.", None

    new_path = os.path.normpath(os.path.join(parent_dir, name))

    if original_path and os.path.normcase(new_path) == os.path.normcase(
        os.path.normpath(original_path)
    ):
        return None, new_path

    if os.path.exists(new_path):
        return "A folder with that name already exists.", None

    return None, new_path


class RenameDialog(QDialog):

    def __init__(self, parent, folder_path):
        super().__init__(parent)

        self.folder_path = folder_path
        self.parent_dir = os.path.dirname(folder_path)
        self.original_name = os.path.basename(folder_path)
        self.new_path = None

        self.setWindowTitle("Rename Mod Folder")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)

        info_label = QLabel(
            "The folder is renamed on disk exactly as entered. "
            "Underscores are kept; the list only displays them as spaces."
        )
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        self.name_edit = QLineEdit(self.original_name)
        self.name_edit.selectAll()
        layout.addWidget(self.name_edit)

        self.hint_label = QLabel()
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet("color: #a6a6a6;")
        layout.addWidget(self.hint_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.name_edit.textChanged.connect(self.validate_name)
        self.validate_name()

    def validate_name(self):
        error, new_path = validate_folder_name(
            self.name_edit.text(),
            self.parent_dir,
            self.folder_path
        )

        self.new_path = new_path
        self.ok_button.setEnabled(error is None)

        if (
            error is None
            and new_path is not None
            and os.path.normcase(new_path)
            == os.path.normcase(os.path.normpath(self.folder_path))
        ):
            error = "Name is unchanged."
            self.ok_button.setEnabled(False)

        if error:
            self.hint_label.setStyleSheet("color: #f48771;")
            self.hint_label.setText(error)
        else:
            self.hint_label.setStyleSheet("color: #a6a6a6;")
            self.hint_label.setText(f"New path: {new_path}")


class MarkerDialog(QDialog):
    """管理一个 mod 文件夹里的多个 .modfile/.mod 标记文件。"""

    def __init__(self, parent, record):
        super().__init__(parent)

        self.record = record
        markers = sorted(
            record.get("markers", []),
            key=lambda marker: marker["path"].lower()
        )

        self.setWindowTitle("Manage Marker Files")
        self.setMinimumSize(720, 480)

        layout = QVBoxLayout(self)

        info_label = QLabel(
            f"This folder contains {len(markers)} marker files. "
            "Checked = active (.modfile), unchecked = inactive (.mod).\n"
            "Select a row and click Preview (or double-click) to open that "
            "sub-mod's .skel/.json in the viewer."
        )
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter marker files...")
        layout.addWidget(self.search_edit)

        self.list_widget = QListWidget()
        for marker in markers:
            relative_path = os.path.relpath(marker["path"], record["path"])
            item = QListWidgetItem(relative_path.replace("\\", "/"))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if marker["active"] else Qt.CheckState.Unchecked
            )
            item.setData(Qt.ItemDataRole.UserRole, marker["path"])
            item.setToolTip(marker["path"])
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget)

        self.count_label = QLabel()
        layout.addWidget(self.count_label)

        action_layout = QHBoxLayout()

        enable_all_btn = QPushButton("Enable All")
        enable_all_btn.clicked.connect(lambda: self.set_all_checked(True))
        action_layout.addWidget(enable_all_btn)

        disable_all_btn = QPushButton("Disable All")
        disable_all_btn.clicked.connect(lambda: self.set_all_checked(False))
        action_layout.addWidget(disable_all_btn)

        self.preview_btn = QPushButton("Preview Selected")
        self.preview_btn.clicked.connect(self.preview_selected)
        action_layout.addWidget(self.preview_btn)

        action_layout.addStretch()
        layout.addLayout(action_layout)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.search_edit.textChanged.connect(self.apply_filter)
        self.list_widget.itemChanged.connect(self.update_count)
        self.list_widget.itemDoubleClicked.connect(self.preview_item)
        self.update_count()

    def set_all_checked(self, checked):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for index in range(self.list_widget.count()):
            self.list_widget.item(index).setCheckState(state)

    def apply_filter(self, text):
        text = text.strip().lower()
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            item.setHidden(bool(text) and text not in item.text().lower())

    def update_count(self, *_args):
        checked = sum(
            1
            for index in range(self.list_widget.count())
            if self.list_widget.item(index).checkState() == Qt.CheckState.Checked
        )
        total = self.list_widget.count()
        self.count_label.setText(f"Checked {checked} / {total} (will be active)")

    def states(self):
        result = {}
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            result[item.data(Qt.ItemDataRole.UserRole)] = (
                item.checkState() == Qt.CheckState.Checked
            )
        return result

    def preview_selected(self):
        self.preview_item(self.list_widget.currentItem())

    def preview_item(self, item):
        if item is None:
            QMessageBox.information(
                self,
                "Preview",
                "Select a marker file first.",
                QMessageBox.StandardButton.Ok
            )
            return

        marker_path = item.data(Qt.ItemDataRole.UserRole)
        preview_path = self.find_preview_path(marker_path)

        if not preview_path:
            QMessageBox.warning(
                self,
                "Preview",
                "No .skel, .json or .png file was found for:\n"
                f"{item.text()}",
                QMessageBox.StandardButton.Ok
            )
            return

        if preview_path.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
            # 剧情插画类 mod 往往只有贴图、没有骨架，直接打开图片。
            parent = self.parent()
            if parent is not None and hasattr(parent, "open_image"):
                parent.open_image(preview_path)
            return

        controller = getattr(self.parent(), "viewer_controller", None)
        if controller is None or not controller.launch_viewer(preview_path):
            QMessageBox.critical(
                self,
                "Error",
                "Failed to launch Spine viewer",
                QMessageBox.StandardButton.Ok
            )

    def find_preview_path(self, marker_path):
        """把 .modfile/.mod 标记文件对应到同名的 .skel/.json 骨架或 .png 图片。"""
        stem = os.path.splitext(os.path.basename(marker_path))[0]
        base_dir = os.path.dirname(marker_path)

        # 1) 整个 mod 目录里同名的骨架/json（标记文件可能和骨架不在同一层）。
        for key in ("skeleton_files", "json_files"):
            for path in self.record.get(key, []):
                if os.path.splitext(os.path.basename(path))[0].lower() == stem.lower():
                    return path

        # 2) 标记文件旁边的同名骨架/json。
        for extension in (".skel", ".json"):
            candidate = os.path.join(base_dir, stem + extension)
            if os.path.isfile(candidate):
                return candidate

        # 3) 有些包用别的名字做标记，退回同一目录下的第一个骨架。
        for key in ("skeleton_files", "json_files"):
            for path in self.record.get(key, []):
                if os.path.dirname(path).lower() == base_dir.lower():
                    return path

        # 4) 纯贴图 mod（剧情插画常见）：没有骨架时退回到同名/同目录图片。
        images = [
            path for path in self.record.get("png_files", [])
            if os.path.dirname(path).lower() == base_dir.lower()
            or os.path.dirname(path).lower().startswith(base_dir.lower() + os.sep)
        ]
        for path in images:
            if os.path.splitext(os.path.basename(path))[0].lower() == stem.lower():
                return path
        if images:
            return sorted(images)[0]

        return None


# ============================================================
# Main application
# ============================================================

class SpineViewer(QWidget):

    def __init__(self):
        super().__init__()

        self.settings_file = os.path.join(
            get_base_path(),
            "spine_viewer_settings.json"
        )

        self.base_window_title = "Brown Dust II Mod Manager"
        self.setWindowTitle(self.base_window_title)

        self.setGeometry(
            100,
            100,
            1000,
            600
        )

        self.viewer_controller = SpineViewerController()

        self.all_mod_items = []
        self.mod_records = []

        self.modfile_index = {}
        self.group_headers = []
        self._record_by_path = {}
        self._whitelist_cache = None

        self.grouped_view = False
        self.duplicate_mode = False
        self.duplicate_paths = set()

        self.update_in_progress = False
        self._update_download_worker = None

        self.collapse_stage = 0

        self.set_windows11_dark_theme()
        self.character_map = self.load_character_map()
        self.settings = self.load_settings()

        self.current_sort_mode = self.settings.get(
            "sort_mode",
            "Name"
        )

        main_layout = QVBoxLayout()

        # Mods folder bar
        folder_layout = QHBoxLayout()

        folder_layout.addWidget(
            QLabel("Mods Folder:")
        )

        self.folder_edit = NoAutoSelectLineEdit()
        self.folder_edit.setPlaceholderText(
            "Path to your mods folder"
        )

        if self.settings.get("mods_folder"):
            self.folder_edit.setText(
                self.settings["mods_folder"]
            )

        folder_layout.addWidget(self.folder_edit)

        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self.browse_mods_folder)
        folder_layout.addWidget(browse_btn)

        self.dup_btn = QPushButton("Duplicates")
        self.dup_btn.clicked.connect(self.run_duplicate_filter)
        folder_layout.addWidget(self.dup_btn)

        refresh_btn = QPushButton("Refresh Mods List")
        refresh_btn.clicked.connect(self.load_mods)
        folder_layout.addWidget(refresh_btn)

        main_layout.addLayout(folder_layout)

        # Search / Sort / Switch bar
        search_layout = QHBoxLayout()

        search_layout.addWidget(
            QLabel("Search:")
        )

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "Filter mods by name or character..."
        )
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.timeout.connect(self.filter_mods)
        self.search_edit.textChanged.connect(
            lambda: self.search_timer.start(120)
        )
        search_layout.addWidget(self.search_edit)

        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.clear_search)
        search_layout.addWidget(clear_btn)

        search_layout.addWidget(
            QLabel("Sort:")
        )

        self.sort_combo = CenteredComboBox()
        self.sort_combo.setFixedWidth(130)
        self.sort_combo.addItems(["Name", "Modified Time"])

        for i in range(self.sort_combo.count()):
            self.sort_combo.setItemData(
                i,
                Qt.AlignmentFlag.AlignHCenter,
                Qt.ItemDataRole.TextAlignmentRole
            )

        self.sort_combo.setCurrentText(self.current_sort_mode)
        self.sort_combo.currentTextChanged.connect(self.sort_changed)
        search_layout.addWidget(self.sort_combo)

        self.switch_btn = QPushButton("Switch")
        self.switch_btn.setFixedWidth(100)
        self.switch_btn.clicked.connect(self.switch_view)
        search_layout.addWidget(self.switch_btn)

        main_layout.addLayout(search_layout)

        # Switch Sub-Bar
        self.switch_tools_widget = QWidget()
        switch_tools_layout = QHBoxLayout(self.switch_tools_widget)
        switch_tools_layout.setContentsMargins(0, 0, 0, 0)

        self.status_label = QLabel("Grouped View: Categorized by Character / Costume")
        self.status_label.setStyleSheet("color: #888888; font-style: italic;")
        switch_tools_layout.addWidget(self.status_label)

        switch_tools_layout.addStretch()

        self.expand_btn = QPushButton("Expand")
        self.expand_btn.setFixedWidth(80)
        self.expand_btn.clicked.connect(self.on_expand_clicked)

        self.collapse_btn = QPushButton("Collapse")
        self.collapse_btn.setFixedWidth(80)
        self.collapse_btn.clicked.connect(self.on_collapse_clicked)

        self.whitelist_btn = QPushButton("Whitelist")
        self.whitelist_btn.setFixedWidth(90)
        self.whitelist_btn.clicked.connect(self.show_whitelist_dialog)

        switch_tools_layout.addWidget(self.expand_btn)
        switch_tools_layout.addWidget(self.collapse_btn)
        switch_tools_layout.addWidget(self.whitelist_btn)

        self.switch_tools_widget.setVisible(False)
        main_layout.addWidget(self.switch_tools_widget)

        # Scroll area
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)

        self.scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.scroll_area.setWidget(self.scroll_content)
        main_layout.addWidget(self.scroll_area)

        self.setLayout(main_layout)

        self.verify_mods_folder()
        self.folder_edit.editingFinished.connect(self.folder_path_changed)

        # 后台联网检查更新
        self.update_worker = UpdateCheckWorker()
        self.update_worker.finished.connect(self.handle_update_check_result)

        bg_thread = threading.Thread(target=self.update_worker.run, daemon=True)
        bg_thread.start()

    def set_windows11_dark_theme(self):
        app = QApplication.instance()

        if sys.platform == "win32":
            try:
                from ctypes import windll, byref, sizeof, c_int
                hwnd = int(self.winId())
                for attribute in [19, 20]:
                    try:
                        value = c_int(1)
                        windll.dwmapi.DwmSetWindowAttribute(
                            hwnd, attribute, byref(value), sizeof(value)
                        )
                    except Exception:
                        pass
            except Exception:
                pass

        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(32, 32, 32))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(240, 240, 240))
        palette.setColor(QPalette.ColorRole.Base, QColor(25, 25, 25))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
        palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(53, 53, 53))
        palette.setColor(QPalette.ColorRole.ToolTipText, QColor(240, 240, 240))
        palette.setColor(QPalette.ColorRole.Text, QColor(240, 240, 240))
        palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(240, 240, 240))
        palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
        palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(240, 240, 240))
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(120, 120, 120))
        palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(127, 127, 127))
        palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(127, 127, 127))
        palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(127, 127, 127))

        app.setPalette(palette)

        self.setStyleSheet(
            """
            QWidget {
                background-color: #202020;
                color: #f0f0f0;
                font-family: "Segoe UI", Arial, sans-serif;
                font-size: 9pt;
            }

            QPushButton {
                background-color: #2d2d2d;
                border: 1px solid #3d3d3d;
                border-radius: 4px;
                padding: 5px 12px;
                min-width: 70px;
            }

            QPushButton:hover {
                background-color: #3d3d3d;
                border: 1px solid #4d4d4d;
            }

            QPushButton:pressed {
                background-color: #1d1d1d;
            }

            QScrollArea {
                border: none;
            }

            QLineEdit {
                background-color: #252525;
                color: #f0f0f0;
                padding: 5px;
                border: 1px solid #3d3d3d;
                border-radius: 4px;
                selection-background-color: #3a6ea5;
                selection-color: #ffffff;
            }

            QLineEdit:disabled {
                background-color: #1a1a1a;
                color: #7f7f7f;
            }

            QComboBox {
                background-color: #2d2d2d;
                border: 1px solid #3d3d3d;
                border-radius: 4px;
                padding: 5px 12px;
                min-width: 120px;
            }

            QComboBox:hover {
                background-color: #3d3d3d;
                border: 1px solid #4d4d4d;
            }

            QComboBox::drop-down {
                border: none;
                width: 20px;
            }

            QComboBox::down-arrow {
                width: 10px;
                height: 10px;
            }

            QComboBox QAbstractItemView {
                background-color: #2d2d2d;
                color: #f0f0f0;
                border: 1px solid #3d3d3d;
                selection-background-color: #3a6ea5;
            }

            QListWidget {
                background-color: #252525;
                color: #f0f0f0;
                border: 1px solid #3d3d3d;
                border-radius: 4px;
            }

            QListWidget::item:selected {
                background-color: #3a6ea5;
            }

            QLabel {
                color: #f0f0f0;
            }

            QScrollBar:vertical {
                border: none;
                background: #252525;
                width: 10px;
                margin: 0px;
            }

            QScrollBar::handle:vertical {
                background: #3d3d3d;
                min-height: 20px;
                border-radius: 5px;
            }

            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                border: none;
                background: none;
                height: 0px;
            }

            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: none;
            }
            """
        )

    def load_settings(self):
        default_settings = {
            "mods_folder": "",
            "sort_mode": "Name",
            "switch_whitelist": []
        }

        try:
            if os.path.exists(self.settings_file):
                with open(self.settings_file, "r", encoding="utf-8") as f:
                    settings = json.load(f)

                if not isinstance(settings, dict):
                    settings = {}

                for key, value in default_settings.items():
                    if key not in settings:
                        settings[key] = value

                return settings
        except Exception as e:
            print(f"Error loading settings: {e}")

        return default_settings

    def save_settings(self):
        try:
            with open(self.settings_file, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving settings: {e}")

    def get_whitelist(self):
        whitelist = self.settings.get("switch_whitelist", [])
        if not isinstance(whitelist, list):
            return []
        return whitelist

    def invalidate_whitelist_cache(self):
        self._whitelist_cache = None

    def _get_normalized_whitelist(self):
        if self._whitelist_cache is None:
            self._whitelist_cache = []
            for path in self.get_whitelist():
                try:
                    self._whitelist_cache.append(self.normalize_path(path))
                except Exception:
                    pass
        return self._whitelist_cache

    def normalize_path(self, path):
        return os.path.abspath(os.path.normpath(path))

    def is_path_whitelisted(self, folder_path):
        folder_path = self.normalize_path(folder_path)

        for white_path in self._get_normalized_whitelist():
            try:
                if os.path.commonpath([folder_path, white_path]) == white_path:
                    return True
            except ValueError:
                continue

        return False

    def show_whitelist_dialog(self):
        mods_folder = self.settings.get("mods_folder", "")

        if not mods_folder or not os.path.isdir(mods_folder):
            QMessageBox.warning(
                self,
                "Mods Folder",
                "Please select a valid Mods Folder first.",
                QMessageBox.StandardButton.Ok
            )
            return

        dialog = WhitelistDialog(self, mods_folder, self.get_whitelist())
        dialog.setStyleSheet(self.styleSheet())

        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.settings["switch_whitelist"] = [
                self.normalize_path(path) for path in dialog.whitelist
            ]
            self.invalidate_whitelist_cache()
            self.save_settings()

            if self.grouped_view:
                self.render_mods()

    def get_duplicate_modfile_records(self):
        duplicate_paths = set()

        for filename_key, paths in self.modfile_index.items():
            if len(paths) >= 2:
                duplicate_paths.update(paths)

        return [
            rec for rec in self.mod_records
            if rec["path"] in duplicate_paths
        ]

    def run_duplicate_filter(self):
        dup_records = self.get_duplicate_modfile_records()
        self.duplicate_paths = {rec["path"] for rec in dup_records}
        self.duplicate_mode = True

        self.grouped_view = True
        self.switch_btn.setText("Normal")
        self.switch_tools_widget.setVisible(True)

        if dup_records:
            self.status_label.setText("Conflict Warning: The following mods contain conflicting .modfile files!")
            self.status_label.setStyleSheet("color: #f48771; font-weight: bold;")
        else:
            self.status_label.setText("No duplicate .modfile conflicts detected.")
            self.status_label.setStyleSheet("color: #888888; font-style: italic;")

        self.render_mods()

    def handle_update_check_result(self, updates_available):
        if not updates_available:
            return

        reply = QMessageBox.question(
            self,
            "Update Available",
            "New updates are available for character mapping. "
            "Would you like to download them now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes
        )

        if reply != QMessageBox.StandardButton.Yes:
            return

        if self.update_in_progress:
            return

        self.update_in_progress = True
        self.setWindowTitle(
            f"{self.base_window_title} - Updating character map..."
        )

        worker = UpdateDownloadWorker()
        worker.finished.connect(self.handle_update_download_finished)
        self._update_download_worker = worker
        threading.Thread(target=worker.run, daemon=True).start()

    def handle_update_download_finished(self, success, message):
        self.update_in_progress = False
        self._update_download_worker = None
        self.setWindowTitle(self.base_window_title)

        if not success:
            QMessageBox.critical(
                self,
                "Update Failed",
                "Could not update the character map.\n\n"
                f"{message}",
                QMessageBox.StandardButton.Ok
            )
            return

        self.character_map = self.load_character_map()

        # scan_mods 对“签名未变化”的记录会直接复用旧对象，
        # 因此先清空签名，确保所有角色名/分组都用新 CSV 重算。
        for record in self.mod_records:
            record["scan_signature"] = None

        self.load_mods()

        QMessageBox.information(
            self,
            "Update Complete",
            f"Character map updated ({len(self.character_map)} entries).",
            QMessageBox.StandardButton.Ok
        )

    def load_character_map(self):
        character_map = {}

        csv_path = os.path.join(
            get_base_path(),
            "BD2 Characters - Characters.csv"
        )

        if not os.path.exists(csv_path):
            # 不在这里同步下载：启动阶段同步联网会卡住窗口，
            # 交给启动后的后台更新检查处理。
            print(f"Character map not found: {csv_path}")
            return character_map

        try:
            # utf-8-sig：万一 CSV 被编辑器保存成带 BOM 的 UTF-8，
            # 第一列名不会变成 "\ufeffCHARACTER" 而导致所有名字为空。
            with open(csv_path, newline="", encoding="utf-8-sig") as csvfile:
                reader = csv.DictReader(csvfile)
                current_character = ""

                for row in reader:
                    if row.get("CHARACTER"):
                        current_character = row["CHARACTER"].strip()

                    character_id = (row.get("ID") or "").strip()
                    if not character_id:
                        continue

                    costume = (row.get("COSTUME") or "").strip()
                    display_name = current_character or "Unknown"
                    if costume:
                        display_name += f" ({costume})"

                    key = character_id.lower()
                    character_map[key] = display_name
        except Exception as e:
            print(f"Error loading character map: {e}")

        return character_map

    def format_display_name(self, name):
        return name.replace("_", " ")

    def clear_search(self):
        self.search_edit.clear()
        self.filter_mods()

    def browse_mods_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Mods Folder",
            os.path.expanduser("~"),
            QFileDialog.Option.ShowDirsOnly
        )

        if folder:
            self.folder_edit.setText(folder)
            self.settings["mods_folder"] = folder
            self.save_settings()
            self.load_mods()

    def folder_path_changed(self):
        path = self.folder_edit.text().strip()
        self.settings["mods_folder"] = path
        self.save_settings()

        if os.path.isdir(path):
            self.load_mods()

    def verify_mods_folder(self):
        if (
            not self.settings.get("mods_folder")
            or not os.path.exists(self.settings["mods_folder"])
        ):
            QMessageBox.information(
                self,
                "Select Mods Folder",
                "Please enter or browse to your mods folder path",
                QMessageBox.StandardButton.Ok
            )
        else:
            self.load_mods()

    def sort_changed(self, mode):
        if mode not in ["Name", "Modified Time"]:
            return

        self.current_sort_mode = mode
        self.settings["sort_mode"] = mode
        self.save_settings()

        self.sort_mod_records()
        if not self.grouped_view and self.all_mod_items:
            self.reorder_normal_mods()
        else:
            self.render_mods()

    def reorder_normal_mods(self):
        self.scroll_content.setUpdatesEnabled(False)
        try:
            while self.scroll_layout.count():
                item = self.scroll_layout.takeAt(0)
                widget = item.widget() if item else None
                if widget:
                    widget.setParent(None)

            self.all_mod_items = []
            for record in self.mod_records:
                widget = record.get("widget")
                if widget is not None:
                    self.scroll_layout.addWidget(widget)
                    self.all_mod_items.append(widget)
        finally:
            self.scroll_content.setUpdatesEnabled(True)
            self.scroll_content.updateGeometry()

        self.filter_mods()

    def calculate_folder_signature(self, item_path):
        digest = hashlib.blake2b(digest_size=16)
        signature_parts = []
        entry_count = 0
        try:
            stack = [item_path]
            while stack:
                current_dir = stack.pop()
                try:
                    with os.scandir(current_dir) as entries:
                        for entry in entries:
                            try:
                                stat = entry.stat(follow_symlinks=False)
                                is_dir = entry.is_dir(follow_symlinks=False)
                            except OSError:
                                continue

                            entry_count += 1
                            relative = os.path.relpath(entry.path, item_path)
                            kind = b"D" if is_dir else b"F"
                            signature_parts.append((
                                kind,
                                relative,
                                stat.st_size,
                                stat.st_mtime_ns
                            ))

                            if is_dir:
                                stack.append(entry.path)
                except OSError:
                    continue
        except OSError:
            return None

        for kind, relative, size, mtime_ns in sorted(signature_parts, key=lambda x: x[1]):
            digest.update(kind)
            digest.update(relative.encode("utf-8", errors="surrogatepass"))
            digest.update(b"\0")
            digest.update(str(size).encode())
            digest.update(b"\0")
            digest.update(str(mtime_ns).encode())
            digest.update(b"\n")
        digest.update(str(entry_count).encode())
        return digest.hexdigest()

    def strip_character_suffix(self, value):
        """去掉 _a / _1 / _2 之类的变体后缀；可处理叠加后缀。"""
        previous = None
        while value != previous:
            previous = value
            value = re.sub(r"(?:_\d+|_a)$", "", value, flags=re.IGNORECASE)
        return value

    def resolve_character_display(self, stem):
        """用骨架/json 文件名解析 CSV 中的角色显示名，解析不到返回 None。"""
        value = stem.lower()

        # cutscene_ 只是同一个角色/服装的技能动画，不再单独显示 "(Cutscene)"，
        # 统一归到 CSV 里的普通角色/服装名，避免同一服装被拆成两组。
        if value.startswith("cutscene_"):
            value = value[len("cutscene_"):]

        candidates = []

        def add_candidate(candidate):
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        add_candidate(value)
        add_candidate(self.strip_character_suffix(value))

        for candidate in candidates:
            display = self.character_map.get(candidate)
            if display:
                return display

        return None

    def build_mod_record(self, item, item_path, scan_signature=None, detected_at=None):
        try:
            latest_mtime = os.path.getmtime(item_path)
        except OSError:
            latest_mtime = 0

        skeleton_files = []
        json_files = []
        png_files = []
        markers = []

        stack = [item_path]
        try:
            while stack:
                current_dir = stack.pop()
                try:
                    with os.scandir(current_dir) as entries:
                        for entry in entries:
                            try:
                                if entry.is_dir(follow_symlinks=False):
                                    stack.append(entry.path)
                                    continue
                            except OSError:
                                continue

                            lower_name = entry.name.lower()
                            file_path = entry.path

                            if lower_name.endswith(".modfile") or lower_name.endswith(".mod"):
                                markers.append({
                                    "name": entry.name,
                                    "path": file_path,
                                    "active": lower_name.endswith(".modfile"),
                                })

                            if lower_name.endswith(".skel"):
                                skeleton_files.append(file_path)
                            elif lower_name.endswith(".json"):
                                json_files.append(file_path)
                            elif lower_name.endswith(".png"):
                                png_files.append(file_path)
                except OSError:
                    continue
        except OSError:
            pass

        stems = []
        seen_stems = set()
        for path in skeleton_files + json_files:
            stem = os.path.splitext(os.path.basename(path))[0]
            key = stem.lower()
            if key not in seen_stems:
                seen_stems.add(key)
                stems.append(stem)

        resolved = [
            (
                stem,
                self.resolve_character_display(stem),
                stem.lower().startswith("cutscene_"),
            )
            for stem in stems
        ]

        # 稳定地选出“主角色”：已知优先、普通立绘优先于 cutscene、短名优先、字典序。
        # 多骨架目录里不再依赖 os.scandir 的返回顺序。
        resolved.sort(
            key=lambda entry: (
                entry[1] is None,
                entry[2],
                len(entry[0]),
                entry[0].lower(),
            )
        )

        if resolved:
            character_id = resolved[0][0].lower()
            character_name = resolved[0][1] or f"Unknown ({resolved[0][0]})"
        else:
            character_id = None
            character_name = "Unknown"

        characters = []
        for stem, display, _is_cutscene in resolved:
            label = display or f"Unknown ({stem})"
            if label not in characters:
                characters.append(label)

        character, costume = self.get_character_and_costume(character_name)

        active_markers = [marker for marker in markers if marker["active"]]
        if active_markers:
            modfile_path = active_markers[0]["path"]
        elif markers:
            modfile_path = markers[0]["path"]
        else:
            modfile_path = None

        is_active = bool(active_markers)

        search_text = " ".join([
            self.format_display_name(item),
            character_name,
            " ".join(characters),
            item_path,
        ]).lower()

        return {
            "item": item,
            "path": item_path,
            "character_id": character_id,
            "character_name": character_name,
            "characters": characters,
            "character": character,
            "costume": costume,
            "latest_mtime": latest_mtime,
            "modfile_path": modfile_path,
            "is_active": is_active,
            "markers": markers,
            "skeleton_files": skeleton_files,
            "json_files": json_files,
            "png_files": png_files,
            "modfile_names": {
                marker["name"].lower() for marker in active_markers
            },
            "scan_signature": scan_signature or self.calculate_folder_signature(item_path),
            "search_text": search_text,
            "widget": None
        }

    def scan_mods(self):
        mods_folder = self.folder_edit.text().strip()
        if not os.path.isdir(mods_folder):
            self.mod_records = []
            self.modfile_index = {}
            self._record_by_path = {}
            return

        discovered_folders = self.discover_mod_folders(mods_folder)
        old_records = self._record_by_path.copy()

        max_workers = min(12, max(2, (os.cpu_count() or 4) * 2), max(1, len(discovered_folders)))
        def signature_pair(pair):
            item, path = pair
            return path, self.calculate_folder_signature(path)

        signatures = {}
        try:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                for path, signature in executor.map(signature_pair, discovered_folders):
                    signatures[path] = signature
        except Exception as e:
            print(f"Parallel mod signature scan failed: {e}")
            for item, path in discovered_folders:
                signatures[path] = self.calculate_folder_signature(path)

        new_records = []
        for item, item_path in discovered_folders:
            old_record = old_records.get(item_path)
            signature = signatures.get(item_path)

            if old_record is not None and old_record.get("scan_signature") == signature:
                old_record["item"] = item
                old_record["path"] = item_path
                try:
                    old_record["latest_mtime"] = os.path.getmtime(item_path)
                except OSError:
                    pass
                new_records.append(old_record)
                continue

            # 记录要重建时，旧 widget 必须销毁，否则会被 render_mods 换下后泄漏。
            if old_record is not None and old_record.get("widget"):
                old_record["widget"].deleteLater()

            record = self.build_mod_record(
                item,
                item_path,
                scan_signature=signature
            )
            new_records.append(record)

        new_paths = {rec["path"] for rec in new_records}
        for old_path, old_rec in old_records.items():
            if old_path not in new_paths and old_rec.get("widget"):
                old_rec["widget"].deleteLater()

        self.mod_records = new_records
        self._record_by_path = {rec["path"]: rec for rec in new_records}
        self._rebuild_modfile_index()

        self.sort_mod_records()

    def _rebuild_modfile_index(self):
        self.modfile_index = {}
        for record in self.mod_records:
            folder_path = record["path"]
            for filename_key in record.get("modfile_names", ()):
                self.modfile_index.setdefault(filename_key, set()).add(folder_path)

    def sort_mod_records(self):
        if self.current_sort_mode == "Modified Time":
            self.mod_records.sort(
                key=lambda record: record.get("latest_mtime", 0),
                reverse=True
            )
        else:
            self.mod_records.sort(
                key=lambda record: record["item"].lower()
            )

    def discover_mod_folders(self, mods_folder):
        folders = []
        try:
            with os.scandir(mods_folder) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            folders.append((entry.name, entry.path))
                    except OSError:
                        continue
        except OSError:
            return folders

        return folders

    def get_character_and_costume(self, character_name):
        if not character_name or character_name == "Unknown":
            return "Unknown", "Unknown"

        match = re.match(r"^(.*?)\s*\((.*?)\)$", character_name)
        if match:
            character = match.group(1).strip()
            costume = match.group(2).strip()
            return (character or "Unknown", costume or "Unknown")

        return character_name.strip(), "Unknown"

    def switch_view(self):
        self.grouped_view = not self.grouped_view

        if self.grouped_view:
            self.switch_btn.setText("Normal")
            self.switch_tools_widget.setVisible(True)
            if not self.duplicate_mode:
                self.status_label.setText("Grouped View: Categorized by Character / Costume")
                self.status_label.setStyleSheet("color: #888888; font-style: italic;")
        else:
            self.switch_btn.setText("Switch")
            self.switch_tools_widget.setVisible(False)

        self.collapse_stage = 0
        self.render_mods()

    def on_expand_clicked(self):
        self.set_all_groups_expanded(True)
        self.collapse_stage = 0

    def on_collapse_clicked(self):
        if self.collapse_stage == 0:
            self.set_level_expanded(1, True)
            self.set_level_expanded(2, False)
            self.collapse_stage = 1
        else:
            self.set_all_groups_expanded(False)
            self.collapse_stage = 2

    def set_level_expanded(self, level, expanded):
        if not self.grouped_view:
            return

        headers = self.get_group_headers()
        for header in headers:
            if header.property("group_level") != level:
                continue

            header.blockSignals(True)
            header.setChecked(expanded)
            
            raw_text = header.property("raw_text") or ""
            prefix = "▾ " if expanded else "▸ "
            header.setText(f"{prefix}{raw_text}")
            header.setProperty("expanded", expanded)
            header.blockSignals(False)

        self.filter_mods()

    def set_all_groups_expanded(self, expanded):
        if not self.grouped_view:
            return

        headers = self.get_group_headers()
        for header in headers:
            header.blockSignals(True)
            header.setChecked(expanded)
            
            raw_text = header.property("raw_text") or ""
            prefix = "▾ " if expanded else "▸ "
            header.setText(f"{prefix}{raw_text}")
            header.setProperty("expanded", expanded)
            header.blockSignals(False)

        self.filter_mods()

    def add_group_header(self, text, level, group_key, expanded=True):
        button = QToolButton()
        prefix = "▾ " if expanded else "▸ "
        button.setText(f"{prefix}{text}")
        button.setCheckable(True)
        button.setChecked(expanded)

        button.setProperty("is_group_header", True)
        button.setProperty("group_level", level)
        button.setProperty("group_key", group_key)
        button.setProperty("expanded", expanded)
        button.setProperty("raw_text", text)

        if level == 1:
            button.setStyleSheet(
                """
                QToolButton {
                    color: #f0f0f0;
                    font-size: 11pt;
                    font-weight: bold;
                    padding: 8px 5px 4px 5px;
                    border: none;
                    text-align: left;
                }
                QToolButton:hover {
                    background-color: #2d2d2d;
                }
                """
            )
        else:
            button.setStyleSheet(
                """
                QToolButton {
                    color: #a6a6a6;
                    font-size: 10pt;
                    font-weight: bold;
                    padding: 4px 5px 2px 20px;
                    border: none;
                    text-align: left;
                }
                QToolButton:hover {
                    background-color: #2d2d2d;
                }
                """
            )

        button.toggled.connect(
            lambda checked, b=button: self.toggle_group(b, checked)
        )

        self.scroll_layout.addWidget(button)
        self.group_headers.append(button)
        return button

    def get_group_headers(self):
        return self.group_headers

    def toggle_group(self, header, expanded):
        raw_text = header.property("raw_text") or ""
        prefix = "▾ " if expanded else "▸ "
        header.setText(f"{prefix}{raw_text}")
        header.setProperty("expanded", expanded)
        self.filter_mods()

    def _matches_search(self, widget):
        search_text = self.search_edit.text().lower().strip()
        if not search_text:
            return True

        cached_text = widget.property("search_text")
        if cached_text is not None:
            return search_text in cached_text

        display_name = (widget.property("display_name") or "").lower()
        character_name = (widget.property("character_name") or "").lower()
        relative_path = (widget.property("relative_path") or "").lower()
        return (
            search_text in display_name
            or search_text in character_name
            or search_text in relative_path
        )

    def load_mods(self):
        self.duplicate_mode = False
        self.duplicate_paths.clear()
        
        if self.grouped_view:
            self.status_label.setText("Grouped View: Categorized by Character / Costume")
            self.status_label.setStyleSheet("color: #888888; font-style: italic;")

        self.scan_mods()
        self.render_mods()

    def render_mods(self):
        self.scroll_content.hide()
        self.scroll_content.setUpdatesEnabled(False)
        try:
            while self.scroll_layout.count():
                item = self.scroll_layout.takeAt(0)
                widget = item.widget() if item else None
                if widget:
                    if widget.property("is_group_header"):
                        widget.deleteLater()
                    else:
                        widget.setParent(None)

            self.all_mod_items = []
            self.group_headers = []

            if self.mod_records:
                records_to_render = (
                    [rec for rec in self.mod_records if rec["path"] in self.duplicate_paths]
                    if self.duplicate_mode
                    else self.mod_records
                )

                if self.grouped_view:
                    self.load_grouped_mods(records_to_render)
                else:
                    for record in records_to_render:
                        self.add_mod_item_from_record(record)

            self.filter_mods()
        finally:
            self.scroll_content.setUpdatesEnabled(True)
            self.scroll_content.show()

    def load_grouped_mods(self, records_to_render):
        grouped_mods = {}
        whitelist_records = []

        for record in records_to_render:
            if self.is_path_whitelisted(record["path"]):
                whitelist_records.append(record)
                continue

            character = record["character"]
            costume = record["costume"]

            grouped_mods.setdefault(character, {}).setdefault(costume, []).append(record)

        sort_mode = self.current_sort_mode

        if sort_mode == "Modified Time":
            character_items = sorted(
                grouped_mods.items(),
                key=lambda x: max(
                    rec["latest_mtime"]
                    for costume_mods in x[1].values()
                    for rec in costume_mods
                ),
                reverse=True
            )
        else:
            character_items = sorted(
                grouped_mods.items(),
                key=lambda x: x[0].lower()
            )

        for character, costumes in character_items:
            character_key = character
            character_header = self.add_group_header(character, 1, character_key, True)
            character_header.setProperty("group_character", character)

            if sort_mode == "Modified Time":
                costume_items = sorted(
                    costumes.items(),
                    key=lambda x: max(rec["latest_mtime"] for rec in x[1]),
                    reverse=True
                )
            else:
                costume_items = sorted(
                    costumes.items(),
                    key=lambda x: x[0].lower()
                )

            for costume, costume_records in costume_items:
                costume_key = f"{character}\0{costume}"
                costume_header = self.add_group_header(costume, 2, costume_key, True)
                costume_header.setProperty("group_character", character)

                if sort_mode == "Modified Time":
                    costume_records = sorted(
                        costume_records,
                        key=lambda rec: rec["latest_mtime"],
                        reverse=True
                    )
                else:
                    costume_records = sorted(
                        costume_records,
                        key=lambda rec: rec["item"].lower()
                    )

                for record in costume_records:
                    self.add_mod_item_from_record(
                        record,
                        group_key=costume_key,
                        group_character=character
                    )

        if whitelist_records:
            if sort_mode == "Modified Time":
                whitelist_records.sort(
                    key=lambda rec: rec["latest_mtime"],
                    reverse=True
                )
            else:
                whitelist_records.sort(
                    key=lambda rec: rec["item"].lower()
                )

            for record in whitelist_records:
                self.add_mod_item_from_record(
                    record,
                    group_key=None,
                    group_character=None,
                    is_whitelisted=True
                )

    def get_or_create_mod_widget(self, record):
        if record.get("widget") is not None:
            return record["widget"]

        folder_name = record["item"]
        folder_path = record["path"]

        item_widget = QWidget()
        item_layout = QHBoxLayout(item_widget)
        item_layout.setContentsMargins(5, 5, 5, 5)
        item_layout.setSpacing(10)

        preview_btn = QPushButton("Preview")
        preview_btn.setFixedWidth(100)
        preview_btn.setProperty("folder_path", folder_path)
        preview_btn.clicked.connect(self.preview_from_button)
        item_layout.addWidget(preview_btn)

        name_edit = QLineEdit(self.format_display_name(folder_name))
        name_edit.setReadOnly(True)
        name_edit.setMinimumWidth(300)
        name_edit.setProperty("original_path", folder_path)
        name_edit.setToolTip(
            "Read-only display. Use Rename to change the folder name on disk; "
            "underscores are shown as spaces here."
        )
        item_layout.addWidget(name_edit)

        character_label = QLabel(record["character_name"])
        character_label.setStyleSheet("color: #a6a6a6;")
        character_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        character_label.setMinimumWidth(150)
        item_layout.addWidget(character_label)

        rename_btn = QPushButton("Rename")
        rename_btn.setFixedWidth(100)
        rename_btn.clicked.connect(
            lambda _checked=False, rec=record: self.rename_folder(rec)
        )
        item_layout.addWidget(rename_btn)

        activate_btn = QPushButton()
        activate_btn.setFixedWidth(100)
        activate_btn.clicked.connect(
            lambda _checked=False, rec=record: self.toggle_mod_activation(rec)
        )
        item_layout.addWidget(activate_btn)

        manage_btn = QPushButton("Markers...")
        manage_btn.setFixedWidth(110)
        manage_btn.clicked.connect(
            lambda _checked=False, rec=record: self.manage_markers(rec)
        )
        item_layout.addWidget(manage_btn)

        record["widget"] = item_widget
        record["ui"] = {
            "name_edit": name_edit,
            "character_label": character_label,
            "preview_btn": preview_btn,
            "rename_btn": rename_btn,
            "activate_btn": activate_btn,
            "manage_btn": manage_btn,
        }
        self._refresh_mod_widget(record)
        return item_widget

    def _activation_state(self, record):
        markers = record.get("markers") or []
        if not markers:
            return "none"

        active_count = sum(1 for marker in markers if marker["active"])
        if active_count == 0:
            return "none"
        if active_count == len(markers):
            return "all"
        return "mixed"

    def _refresh_mod_widget(self, record):
        ui = record.get("ui")
        if not ui:
            return

        markers = record.get("markers") or []
        state = self._activation_state(record)
        colors = {
            "all": "#4ec9b0",
            "none": "#f48771",
            "mixed": "#d7ba7d",
        }
        color = colors[state]

        ui["name_edit"].setText(self.format_display_name(record["item"]))
        ui["name_edit"].setStyleSheet(f"color: {color};")
        ui["name_edit"].setProperty("original_path", record["path"])

        ui["character_label"].setText(record["character_name"])
        characters = record.get("characters") or []
        ui["character_label"].setToolTip("\n".join(characters))

        ui["preview_btn"].setProperty("folder_path", record["path"])

        labels = {"all": "Deactivate", "none": "Activate", "mixed": "Enable All"}
        ui["activate_btn"].setText(labels[state])
        ui["activate_btn"].setStyleSheet(f"color: {color};")

        active_count = sum(1 for marker in markers if marker["active"])
        if markers:
            ui["activate_btn"].setToolTip(
                f"{active_count}/{len(markers)} marker files active. "
                "Click to enable/disable the whole folder."
            )
        else:
            ui["activate_btn"].setToolTip("No .modfile / .mod file found in this folder.")

        ui["manage_btn"].setVisible(len(markers) > 1)
        if len(markers) > 1:
            ui["manage_btn"].setText(f"Markers ({len(markers)})")
            ui["manage_btn"].setToolTip("Enable or disable individual marker files.")

    def add_mod_item_from_record(
        self,
        record,
        group_key=None,
        group_character=None,
        is_whitelisted=False
    ):
        folder_name = record["item"]
        character_name = record["character_name"]

        item_widget = self.get_or_create_mod_widget(record)

        item_widget.setProperty("display_name", self.format_display_name(folder_name))
        item_widget.setProperty("character_name", character_name)
        item_widget.setProperty("group_key", group_key)
        item_widget.setProperty("group_character", group_character)
        item_widget.setProperty("is_whitelisted", is_whitelisted)
        item_widget.setProperty("relative_path", folder_name)
        item_widget.setProperty("search_text", record.get("search_text", ""))

        self.scroll_layout.addWidget(item_widget)
        self.all_mod_items.append(item_widget)

    def filter_mods(self):
        if not self.grouped_view:
            for widget in self.all_mod_items:
                widget.setVisible(self._matches_search(widget))
            return

        visible_costumes = set()
        visible_characters = set()

        for widget in self.all_mod_items:
            matched = self._matches_search(widget)
            widget.setProperty("search_match", matched)

            group_key = widget.property("group_key")
            character = widget.property("group_character")

            if widget.property("is_whitelisted"):
                widget.setVisible(matched)
                continue

            if matched:
                visible_costumes.add(group_key)
                visible_characters.add(character)

        character_expanded = {}
        for header in self.get_group_headers():
            if header.property("group_level") != 1:
                continue
            character = header.property("group_key")
            expanded = bool(header.property("expanded"))
            character_expanded[character] = expanded
            header.setVisible(character in visible_characters)

        costume_expanded = {}
        for header in self.get_group_headers():
            if header.property("group_level") != 2:
                continue
            group_key = header.property("group_key")
            character = header.property("group_character")
            expanded = bool(header.property("expanded"))
            costume_expanded[group_key] = expanded

            parent_expanded = character_expanded.get(character, True)
            has_match = group_key in visible_costumes
            header.setVisible(parent_expanded and has_match)

        for widget in self.all_mod_items:
            if widget.property("is_whitelisted"):
                continue

            matched = bool(widget.property("search_match"))
            if not matched:
                widget.setVisible(False)
                continue

            group_key = widget.property("group_key")
            character = widget.property("group_character")

            character_is_expanded = character_expanded.get(character, True)
            costume_is_expanded = costume_expanded.get(group_key, True)

            widget.setVisible(
                matched and character_is_expanded and costume_is_expanded
            )

    def get_modfile_path(self, folder_path):
        record = self._record_by_path.get(folder_path)
        if record is not None:
            cached = record.get("modfile_path")
            if cached and os.path.isfile(cached):
                return cached

        try:
            with os.scandir(folder_path) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        result = self.get_modfile_path(entry.path)
                        if result:
                            return result
                    elif entry.name.lower().endswith((".modfile", ".mod")):
                        return entry.path
        except OSError:
            pass
        return None

    def _rename_marker(self, marker, target_active):
        """把一个标记文件切到目标状态，返回 (新路径, 新状态)。"""
        old_path = marker["path"]
        lower = old_path.lower()

        if target_active:
            if lower.endswith(".modfile"):
                return old_path, True
            if not lower.endswith(".mod"):
                raise ValueError(f"Unsupported marker: {marker['name']}")
            new_path = old_path[:-4] + ".modfile"
        else:
            if lower.endswith(".mod"):
                return old_path, False
            if not lower.endswith(".modfile"):
                raise ValueError(f"Unsupported marker: {marker['name']}")
            new_path = old_path[:-8] + ".mod"

        if (
            os.path.exists(new_path)
            and os.path.normcase(new_path) != os.path.normcase(old_path)
        ):
            raise FileExistsError(
                f"{os.path.basename(new_path)} already exists in the same folder"
            )

        os.rename(old_path, new_path)
        return new_path, target_active

    def _apply_marker_states(self, record, desired_states):
        """按 {原路径: 是否启用} 批量切换标记文件。"""
        markers = record.get("markers") or []
        errors = []
        changed = 0

        for marker in markers:
            target_active = bool(desired_states.get(marker["path"], marker["active"]))
            if target_active == marker["active"]:
                continue

            try:
                new_path, new_active = self._rename_marker(marker, target_active)
            except Exception as e:
                errors.append(f"{marker['name']}: {e}")
                continue

            marker["path"] = new_path
            marker["name"] = os.path.basename(new_path)
            marker["active"] = new_active
            changed += 1

        if changed:
            active_markers = [marker for marker in markers if marker["active"]]
            record["is_active"] = bool(active_markers)
            record["modfile_path"] = (
                active_markers[0]["path"]
                if active_markers
                else (markers[0]["path"] if markers else None)
            )
            record["modfile_names"] = {
                marker["name"].lower() for marker in active_markers
            }
            record["scan_signature"] = None
            record["latest_mtime"] = time.time()
            self._rebuild_modfile_index()
            self._refresh_mod_widget(record)

        return changed, errors

    def toggle_mod_activation(self, record=None):
        if record is None:
            btn = self.sender()
            if btn:
                record = self._record_by_path.get(btn.property("folder_path"))
        if not record:
            return

        markers = record.get("markers") or []
        if not markers:
            QMessageBox.warning(
                self,
                "No Marker File",
                "No .modfile or .mod file was found in this mod folder.",
                QMessageBox.StandardButton.Ok
            )
            return

        # all -> 全部停用；none/mixed -> 全部启用
        target_active = self._activation_state(record) != "all"

        if len(markers) >= 10:
            action = "Enable" if target_active else "Disable"
            reply = QMessageBox.question(
                self,
                f"{action} All Markers",
                f"This folder contains {len(markers)} marker files. "
                f"{action} all of them?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        desired_states = {marker["path"]: target_active for marker in markers}
        changed, errors = self._apply_marker_states(record, desired_states)

        if errors:
            details = "\n".join(errors[:10])
            if len(errors) > 10:
                details += f"\n... and {len(errors) - 10} more"
            QMessageBox.warning(
                self,
                "Some Markers Were Not Changed",
                f"Changed {changed} marker(s), but some failed:\n\n{details}",
                QMessageBox.StandardButton.Ok
            )

    def manage_markers(self, record):
        dialog = MarkerDialog(self, record)
        dialog.setStyleSheet(self.styleSheet())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        changed, errors = self._apply_marker_states(record, dialog.states())

        if errors:
            details = "\n".join(errors[:10])
            if len(errors) > 10:
                details += f"\n... and {len(errors) - 10} more"
            QMessageBox.warning(
                self,
                "Some Markers Were Not Changed",
                f"Changed {changed} marker(s), but some failed:\n\n{details}",
                QMessageBox.StandardButton.Ok
            )

    def rename_folder(self, record=None):
        if record is None:
            btn = self.sender()
            if btn:
                record = self._record_by_path.get(btn.property("folder_path"))
        if not record:
            return

        old_path = record["path"]
        dialog = RenameDialog(self, old_path)
        dialog.setStyleSheet(self.styleSheet())

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        new_path = dialog.new_path
        if not new_path:
            return

        if os.path.normcase(new_path) == os.path.normcase(old_path):
            return

        try:
            os.rename(old_path, new_path)
        except OSError as e:
            QMessageBox.critical(
                self,
                "Rename Failed",
                f"Failed to rename folder:\n{e}",
                QMessageBox.StandardButton.Ok
            )
            return

        new_name = os.path.basename(new_path)
        self._apply_folder_rename(record, old_path, new_path, new_name)

        QMessageBox.information(
            self,
            "Renamed",
            f"Renamed to:\n{new_name}",
            QMessageBox.StandardButton.Ok
        )

    def _apply_folder_rename(self, record, old_path, new_path, new_name):
        old_prefix = old_path.rstrip(os.sep) + os.sep
        new_prefix = new_path.rstrip(os.sep) + os.sep

        def rebase(path):
            if path == old_path:
                return new_path
            if path.startswith(old_prefix):
                return new_prefix + path[len(old_prefix):]
            return path

        record["path"] = new_path
        record["item"] = new_name
        record["scan_signature"] = None

        try:
            record["latest_mtime"] = os.path.getmtime(new_path)
        except OSError:
            record["latest_mtime"] = time.time()

        for marker in record.get("markers", []):
            marker["path"] = rebase(marker["path"])

        for key in ("skeleton_files", "json_files", "png_files"):
            record[key] = [rebase(path) for path in record.get(key, [])]

        if record.get("modfile_path"):
            record["modfile_path"] = rebase(record["modfile_path"])

        record["search_text"] = " ".join([
            self.format_display_name(new_name),
            record.get("character_name", ""),
            " ".join(record.get("characters", [])),
            new_path,
        ]).lower()

        self._record_by_path.pop(old_path, None)
        self._record_by_path[new_path] = record

        if old_path in self.duplicate_paths:
            self.duplicate_paths.discard(old_path)
            self.duplicate_paths.add(new_path)

        self._rebase_whitelist(old_path, new_path)
        self._rebuild_modfile_index()
        self._refresh_mod_widget(record)

        self.sort_mod_records()
        self.render_mods()

    def _rebase_whitelist(self, old_path, new_path):
        """改名后同步白名单里的路径，避免分组突然失效。"""
        entries = self.settings.get("switch_whitelist", [])
        if not entries:
            return

        updated = []
        changed = False

        for entry in entries:
            try:
                relative = os.path.relpath(entry, old_path)
            except ValueError:
                updated.append(entry)
                continue

            if relative == ".":
                updated.append(new_path)
                changed = True
            elif not relative.startswith(".."):
                updated.append(os.path.join(new_path, relative))
                changed = True
            else:
                updated.append(entry)

        if changed:
            self.settings["switch_whitelist"] = updated
            self.save_settings()
            self.invalidate_whitelist_cache()

    def preview_from_button(self):
        btn = self.sender()
        if not btn:
            return
        folder_path = btn.property("folder_path")
        if folder_path:
            self.preview_folder(folder_path)

    def preview_folder(self, folder_path):
        record = self._record_by_path.get(folder_path)

        if record is not None:
            skeleton_files = record.get("skeleton_files", [])
            json_files = record.get("json_files", [])
            png_files = record.get("png_files", [])
        else:
            skeleton_files = []
            json_files = []
            png_files = []
            try:
                stack = [folder_path]
                while stack:
                    current = stack.pop()
                    try:
                        with os.scandir(current) as current_entries:
                            for entry in current_entries:
                                try:
                                    if entry.is_dir(follow_symlinks=False):
                                        stack.append(entry.path)
                                        continue
                                except OSError:
                                    continue

                                lower = entry.name.lower()
                                if lower.endswith(".skel"):
                                    skeleton_files.append(entry.path)
                                elif lower.endswith(".json"):
                                    json_files.append(entry.path)
                                elif lower.endswith(".png"):
                                    png_files.append(entry.path)
                    except OSError:
                        continue
            except OSError:
                pass

        for skel_file in skeleton_files:
            self.preview_animation(skel_file)
            return

        for json_file in json_files:
            try:
                for encoding in ["utf-8", "utf-16", "latin-1", "cp1252"]:
                    try:
                        with open(json_file, "r", encoding=encoding) as f:
                            data = json.load(f)
                            if "skeleton" in data or "bones" in data:
                                self.preview_animation(json_file)
                                return
                    except UnicodeDecodeError:
                        continue
                    except (json.JSONDecodeError, OSError):
                        break
            except Exception as e:
                print(f"Error processing JSON file: {e}")

        if png_files:
            self.open_image(png_files[0])
            return

        QMessageBox.warning(
            self,
            "No Animation Files",
            "No valid .skel, .json, or .png files "
            f"found in {os.path.basename(folder_path)}",
            QMessageBox.StandardButton.Ok
        )

    def open_image(self, image_path):
        try:
            norm_path = os.path.normpath(image_path)
            if sys.platform == "win32":
                os.startfile(norm_path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", norm_path])
            else:
                subprocess.Popen(["xdg-open", norm_path])
        except Exception as e:
            QMessageBox.critical(
                self,
                "Error",
                f"Failed to open image:\n{str(e)}",
                QMessageBox.StandardButton.Ok
            )

    def preview_animation(self, animation_path):
        if not self.viewer_controller.launch_viewer(animation_path):
            QMessageBox.critical(
                self,
                "Error",
                "Failed to launch Spine viewer",
                QMessageBox.StandardButton.Ok
            )
        else:
            QTimer.singleShot(500, self.bring_to_front)

    def bring_to_front(self):
        self.raise_()
        self.activateWindow()
        self.showNormal()

    def closeEvent(self, event):
        if hasattr(self, "viewer_controller") and self.viewer_controller:
            self.viewer_controller.close_viewer()
        event.accept()


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    app = QApplication(sys.argv)
    icon_path = os.path.join(get_base_path(), "icon.png")

    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    viewer = SpineViewer()
    # 启动时不要把光标放进 Mods 路径框（否则整条路径会被高亮，且点击别处也
    # 像“全选”了一样）。让窗口本身接收初始焦点，用户点哪里再聚焦哪里。
    # blockSignals 避免 clearFocus 触发 editingFinished → 启动时重复扫描。
    viewer.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    viewer.show()
    viewer.folder_edit.blockSignals(True)
    viewer.folder_edit.clearFocus()
    viewer.folder_edit.blockSignals(False)
    viewer.setFocus()
    sys.exit(app.exec())