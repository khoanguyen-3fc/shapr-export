import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSettings, QSize, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


@dataclass
class Project:
    project_id: str
    title: str
    slot_id: Optional[str]
    remote_id: Optional[str]
    team_id: Optional[str]
    thumbnail_light: Optional[str]
    thumbnail_dark: Optional[str]


class ProjectBrowser(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Shapr Toolbox")
        self.resize(980, 680)

        self.app_data = (
            Path.home() / "Library" / "Containers" / "com.shapr3d.shapr" / "Data"
        )
        self.storage_db = (
            self.app_data
            / "Library"
            / "Application Support"
            / "com.shapr3d.shapr"
            / "storage"
            / "projectStorage.db"
        )
        self.resources_dir = (
            self.app_data
            / "Library"
            / "Application Support"
            / "com.shapr3d.shapr"
            / "storage"
            / "resources"
        )
        self.projects_root = self.app_data / "Documents" / "projects"
        self.tessellation_cache = self.app_data / "Library" / "Caches" / "Tessellation"
        self.settings = QSettings("shapr-toolbox", "shapr-toolbox")
        last_dir_raw = self.settings.value("last_export_dir", str(Path.home()))
        self.last_export_dir = Path(str(last_dir_raw)).expanduser()
        if not self.last_export_dir.exists():
            self.last_export_dir = Path.home()

        self.projects: list[Project] = []

        root = QWidget(self)
        self.setCentralWidget(root)

        layout = QVBoxLayout(root)
        # layout.setContentsMargins(12, 12, 12, 12)
        # layout.setSpacing(10)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.SingleSelection)
        self.list_widget.setViewMode(QListWidget.IconMode)
        self.list_widget.setResizeMode(QListWidget.Adjust)
        self.list_widget.setMovement(QListWidget.Static)
        # self.list_widget.setSpacing(14)
        self.list_widget.setIconSize(QSize(240, 180))
        # self.list_widget.setGridSize(QSize(240, 180 + 30))
        layout.addWidget(self.list_widget, stretch=1)

        button_row = QHBoxLayout()

        self.status_label = QLabel("Loading projects...")
        button_row.addWidget(self.status_label)

        button_row.addStretch(1)

        self.export_project_button = QPushButton("Export Project")
        self.export_project_button.clicked.connect(self.export_project)
        button_row.addWidget(self.export_project_button)

        self.export_tess_button = QPushButton("Export Tessellation (Experimental)")
        self.export_tess_button.clicked.connect(self.export_tessellation)
        button_row.addWidget(self.export_tess_button)

        self.export_parasolid_button = QPushButton("Export Parasolid")
        self.export_parasolid_button.clicked.connect(self.export_parasolid)
        button_row.addWidget(self.export_parasolid_button)

        layout.addLayout(button_row)

        self.load_projects()

    def load_projects(self) -> None:
        if not self.storage_db.exists():
            self.status_label.setText("Project database not found.")
            return

        query = """
            SELECT projectID, title, slotID, remoteID, teamID, thumbnailLight, thumbnailDark
            FROM Projects p JOIN Spaces s ON p.spaceID = s.spaceID AND p.userID = s.userID
            WHERE isDeleted = 0 AND isTemporary = 0
            ORDER BY lastTouchedAtMsec DESC
        """

        try:
            with sqlite3.connect(self.storage_db) as conn:
                rows = conn.execute(query).fetchall()
        except sqlite3.Error as exc:
            self.status_label.setText(f"Failed to read database: {exc}")
            return

        self.projects = [
            Project(
                project_id=row[0],
                title=row[1] or row[0],
                slot_id=row[2],
                remote_id=row[3],
                team_id=row[4],
                thumbnail_light=row[5],
                thumbnail_dark=row[6],
            )
            for row in rows
        ]

        self.render_projects()

    def render_projects(self) -> None:
        self.list_widget.clear()

        for project in self.projects:
            item = QListWidgetItem(project.title)
            item.setData(Qt.UserRole, project)
            pixmap = self.load_thumbnail(project)
            item.setIcon(QIcon(pixmap))
            item.setTextAlignment(Qt.AlignHCenter)
            item.setFlags(item.flags() | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            self.list_widget.addItem(item)

        self.status_label.setText(f"Loaded {len(self.projects)} projects")

    def load_thumbnail(self, project: Project) -> QPixmap:
        thumb_path = self.resolve_thumbnail_path(
            project.thumbnail_light
        ) or self.resolve_thumbnail_path(project.thumbnail_dark)

        if thumb_path and thumb_path.exists():
            pixmap = QPixmap(str(thumb_path))
            if not pixmap.isNull():
                return pixmap.scaled(
                    self.list_widget.iconSize(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )

        return self.placeholder_thumbnail(project.title)

    def resolve_thumbnail_path(self, value: Optional[str]) -> Optional[Path]:
        if not value:
            return None

        raw = value.strip()
        if not raw:
            return None

        direct = Path(raw).expanduser()
        if direct.exists():
            return direct

        candidate = self.resources_dir / raw
        if candidate.exists():
            return candidate

        candidate_name = self.resources_dir / Path(raw).name
        if candidate_name.exists():
            return candidate_name

        return None

    def placeholder_thumbnail(self, title: str) -> QPixmap:
        pixmap = QPixmap(self.list_widget.iconSize())
        pixmap.fill(QColor("#27374D"))

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QColor("#DDE6ED"))
        painter.drawText(pixmap.rect(), Qt.AlignCenter, title[:2].upper() or "PR")
        painter.end()

        return pixmap

    def selected_project(self) -> Optional[Project]:
        items = self.list_widget.selectedItems()
        if len(items) == 0:
            return None
        return items[0].data(Qt.UserRole)

    def export_project(self) -> None:
        project = self.selected_project()
        if project is None:
            QMessageBox.warning(self, "No selection", "Please select a project first.")
            return

        if not project.slot_id:
            QMessageBox.warning(self, "Missing slot", "Selected project has no slotID.")
            return

        source = self.projects_root / project.project_id / project.slot_id
        if not source.exists():
            QMessageBox.warning(
                self, "Not found", f"Project workspace not found:\n{source}"
            )
            return

        project_file_name = self.safe_name(project.title, project.project_id)
        default_path = str(self.last_export_dir / f"{project_file_name}.shapr")
        selected_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Project",
            default_path,
            "Shapr3D Projects (*.shapr);;All Files (*)",
        )
        if not selected_path:
            return

        destination = Path(selected_path)
        if destination.suffix.lower() != ".shapr":
            destination = destination.with_suffix(".shapr")
        self.last_export_dir = destination.parent
        self.settings.setValue("last_export_dir", str(self.last_export_dir))

        archive_base = destination.with_suffix("")

        try:
            if destination.exists():
                destination.unlink()

            zip_path = Path(
                shutil.make_archive(str(archive_base), "zip", root_dir=source)
            )

            if destination.exists():
                destination.unlink()
            zip_path.rename(destination)
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return

        QMessageBox.information(
            self, "Export complete", f"Project exported to:\n{destination}"
        )

    def export_tessellation(self) -> None:
        project = self.selected_project()
        if project is None:
            QMessageBox.warning(self, "No selection", "Please select a project first.")
            return

        source = self.tessellation_cache / f"{project.project_id}.db"
        if source.exists():
            try:
                source.unlink()
            except OSError as exc:
                QMessageBox.critical(
                    self, "Export failed", f"Failed to delete old cache: {exc}"
                )
                return

        project_deeplink = (
            f"shapr3d://project/{project.remote_id}?team={project.team_id}"
            if project.remote_id and project.team_id
            else None
        )

        if project_deeplink:
            QMessageBox.information(
                self,
                "Generating tessellation",
                f'The project "{project.title}" will be opened in Shapr3D to trigger tessellation cache generation.\n\n'
                + "Click OK to open the project in Shapr3D.\n\n"
                + "Once the app is open, please wait a few moments for the tessellation cache to be generated, then return here to complete the export.",
            )

            QDesktopServices.openUrl(QUrl(project_deeplink))

            QMessageBox.information(
                self,
                "Continue export",
                "Please wait a few moments for the tessellation cache to be generated, then press OK to continue the export process.",
            )
        else:
            QMessageBox.information(
                self,
                "Manual action required",
                f'Please manually open the project "{project.title}" in Shapr3D to trigger tessellation cache generation.\n\n'
                + "Once the project is open, please wait a few moments for the tessellation cache to be generated, then return here to complete the export.",
            )

        if not source.exists():
            QMessageBox.warning(
                self,
                "Cache not found",
                f"Tessellation cache not regenerated:\n{source}\n\n"
                + "Please try again, ensuring that you wait a few moments after Shapr3D opens the project to allow the cache to be generated.",
            )
            return

        project_file_name = self.safe_name(project.title, project.project_id)
        default_path = str(self.last_export_dir / f"{project_file_name}.stl")

        selected_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Tessellation",
            default_path,
            "Standard Tessellation Language Files (*.stl);;All Files (*)",
        )
        if not selected_path:
            return
        self.last_export_dir = Path(selected_path).parent
        self.settings.setValue("last_export_dir", str(self.last_export_dir))

        import tessellation_export

        try:
            result = tessellation_export.export_stl(source, Path(selected_path))
        except tessellation_export.TessellationError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - surface any conversion error
            QMessageBox.critical(
                self, "Export failed", f"Failed to convert tessellation cache:\n{exc}"
            )
            return

        QMessageBox.information(
            self,
            "Export complete",
            f"Tessellation exported to:\n{selected_path}\n\n"
            f"{result['triangles']} triangles from {result['faces']} faces",
        )

    def export_parasolid(self) -> None:
        project = self.selected_project()
        if project is None:
            QMessageBox.warning(self, "No selection", "Please select a project first.")
            return

        if not project.slot_id:
            QMessageBox.warning(self, "Missing slot", "Selected project has no slotID.")
            return

        source = self.projects_root / project.project_id / project.slot_id
        workspace_db = source / "workspace"
        if not workspace_db.exists():
            QMessageBox.warning(
                self, "Not found", f"Project workspace not found:\n{workspace_db}"
            )
            return

        # Imported lazily so the app still launches if the ps-parser submodule
        # has not been initialized (git submodule update --init).
        try:
            import parasolid_export
        except ImportError as exc:
            QMessageBox.critical(
                self,
                "Parasolid support unavailable",
                "The ps-parser library could not be loaded. Run:\n\n"
                "    git submodule update --init\n\n"
                f"Details: {exc}",
            )
            return

        project_file_name = self.safe_name(project.title, project.project_id)
        default_path = str(self.last_export_dir / f"{project_file_name}.x_b")
        selected_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Parasolid",
            default_path,
            "Parasolid Transmit Files (*.x_b);;All Files (*)",
        )
        if not selected_path:
            return

        destination = Path(selected_path)
        if destination.suffix.lower() != ".x_b":
            destination = destination.with_suffix(".x_b")
        self.last_export_dir = destination.parent
        self.settings.setValue("last_export_dir", str(self.last_export_dir))

        try:
            result = parasolid_export.export_workspace(workspace_db, destination)
        except parasolid_export.ExportError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - surface any parser/merge error
            QMessageBox.critical(
                self, "Export failed", f"Unexpected error during export:\n{exc}"
            )
            return

        bodies = result["bodies"]
        body_list = ", ".join(bodies) if bodies else "(none)"
        QMessageBox.information(
            self,
            "Export complete",
            f"Parasolid exported to:\n{destination}\n\n"
            f"{len(bodies)} body/bodies: {body_list}",
        )

    @staticmethod
    def safe_name(title: str, fallback: str) -> str:
        source = title.strip() or fallback
        allowed = []
        for ch in source:
            if ch.isalnum() or ch in ("-", "_"):
                allowed.append(ch)
            elif ch in (" ", "."):
                allowed.append("_")
        cleaned = "".join(allowed).strip("_")
        return cleaned or fallback


def main() -> int:
    app = QApplication(sys.argv)
    window = ProjectBrowser()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
