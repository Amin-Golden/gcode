import sys
import time
from dataclasses import dataclass

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:
    import serial
except ImportError:
    serial = None


@dataclass
class TextItem:
    key: str
    label: str
    text: str
    x: float
    y: float


class LaserPreview(QWidget):
    itemMoved = pyqtSignal(int, float, float)
    itemSelected = pyqtSignal(int)

    def __init__(self, items, font_factory, parent=None):
        super().__init__(parent)
        self.items = items
        self.font_factory = font_factory
        self.work_width = 86.0
        self.work_height = 54.0
        self.padding = 28
        self.selected_index = 0
        self.drag_index = None
        self.drag_offset = QPointF(0, 0)
        self.setMinimumSize(620, 440)
        self.setMouseTracking(True)

    def set_work_area(self, width, height):
        self.work_width = max(1.0, float(width))
        self.work_height = max(1.0, float(height))
        self.update()

    def update_preview(self):
        self.update()

    def set_selected_index(self, index):
        self.selected_index = index
        self.update()

    def scale_factor(self):
        available_w = max(1, self.width() - self.padding * 2)
        available_h = max(1, self.height() - self.padding * 2)
        return min(available_w / self.work_width, available_h / self.work_height)

    def origin(self):
        scale = self.scale_factor()
        used_w = self.work_width * scale
        used_h = self.work_height * scale
        return QPointF((self.width() - used_w) / 2, (self.height() + used_h) / 2)

    def machine_to_screen(self, point):
        scale = self.scale_factor()
        origin = self.origin()
        return QPointF(origin.x() + point.x() * scale, origin.y() - point.y() * scale)

    def screen_to_machine(self, point):
        scale = self.scale_factor()
        origin = self.origin()
        return QPointF((point.x() - origin.x()) / scale, (origin.y() - point.y()) / scale)

    def text_path(self, item):
        path = QPainterPath()
        path.addText(item.x, item.y, self.font_factory(), item.text)
        return path

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(245, 246, 248))

        scale = self.scale_factor()
        origin = self.origin()

        painter.translate(origin)
        painter.scale(scale, -scale)

        self.draw_work_area(painter)
        self.draw_text_items(painter)
        painter.end()

    def draw_work_area(self, painter):
        grid_pen = QPen(QColor(214, 219, 226), 0)
        painter.setPen(grid_pen)
        for x in range(0, int(self.work_width) + 1, 5):
            painter.drawLine(QPointF(float(x), 0.0), QPointF(float(x), self.work_height))
        for y in range(0, int(self.work_height) + 1, 5):
            painter.drawLine(QPointF(0.0, float(y)), QPointF(self.work_width, float(y)))

        border_pen = QPen(QColor(46, 52, 64), 0)
        border_pen.setWidthF(0)
        painter.setPen(border_pen)
        painter.drawRect(QRectF(0, 0, self.work_width, self.work_height))

        axis_pen = QPen(QColor(35, 130, 95), 0)
        axis_pen.setDashPattern([2, 2])
        painter.setPen(axis_pen)
        painter.drawLine(QPointF(0.0, 0.0), QPointF(min(20.0, self.work_width), 0.0))
        painter.drawLine(QPointF(0.0, 0.0), QPointF(0.0, min(20.0, self.work_height)))

    def draw_text_items(self, painter):
        for index, item in enumerate(self.items):
            if not item.text:
                marker = QRectF(item.x - 0.8, item.y - 0.8, 1.6, 1.6)
                painter.setPen(QPen(QColor(120, 126, 135), 0))
                painter.drawEllipse(marker)
                continue

            path = self.text_path(item)
            is_selected = index == self.selected_index
            painter.setPen(QPen(QColor(25, 85, 150) if is_selected else QColor(42, 48, 58), 0))
            painter.drawPath(path)

            bounds = path.boundingRect()
            box_pen = QPen(QColor(225, 80, 68) if is_selected else QColor(137, 147, 161), 0)
            box_pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(box_pen)
            painter.drawRect(bounds)

    def hit_test(self, screen_pos):
        machine_pos = self.screen_to_machine(screen_pos)
        point_rect = QRectF(machine_pos.x() - 1.5, machine_pos.y() - 1.5, 3.0, 3.0)
        for index in reversed(range(len(self.items))):
            item = self.items[index]
            if item.text:
                if self.text_path(item).boundingRect().adjusted(-1, -1, 1, 1).contains(machine_pos):
                    return index
            elif point_rect.contains(QPointF(item.x, item.y)):
                return index
        return None

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        index = self.hit_test(event.position())
        if index is None:
            return
        self.selected_index = index
        self.drag_index = index
        item = self.items[index]
        self.drag_offset = self.screen_to_machine(event.position()) - QPointF(item.x, item.y)
        self.itemSelected.emit(index)
        self.update()

    def mouseMoveEvent(self, event):
        if self.drag_index is None:
            return
        machine_pos = self.screen_to_machine(event.position()) - self.drag_offset
        x = max(0.0, min(self.work_width, machine_pos.x()))
        y = max(0.0, min(self.work_height, machine_pos.y()))
        self.itemMoved.emit(self.drag_index, x, y)

    def mouseReleaseEvent(self, event):
        self.drag_index = None


class LaserTextGCodeApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("تولید G-code حکاکی لیزر فارسی")
        self.items = [
            TextItem("first_name", "نام", "علی", 8.0, 42.0),
            TextItem("last_name", "نام خانوادگی", "رضایی", 8.0, 32.0),
            TextItem("card_number", "شماره کارت", "۶۰۳۷ ۹۹۷۵ ۱۲۳۴ ۵۶۷۸", 8.0, 18.0),
            TextItem("extra", "توضیح", "", 8.0, 8.0),
        ]
        self.text_fields = []
        self.x_fields = []
        self.y_fields = []
        self.init_ui()
        self.refresh_data()

    def init_ui(self):
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        main_layout = QHBoxLayout(self)

        controls = QVBoxLayout()
        controls.addWidget(self.build_text_group())
        controls.addWidget(self.build_font_group())
        controls.addWidget(self.build_device_group())
        controls.addLayout(self.build_buttons())

        self.gcode_text = QTextEdit()
        self.gcode_text.setReadOnly(True)
        self.gcode_text.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.gcode_text.setPlaceholderText("G-code تولید شده در این بخش نمایش داده می‌شود.")
        self.gcode_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        controls.addWidget(self.gcode_text, stretch=1)

        preview_column = QVBoxLayout()
        preview_label = QLabel("پیش‌نمایش خروجی لیزر - برای تغییر محل، متن را بکشید.")
        preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview = LaserPreview(self.items, self.current_font)
        self.preview.itemMoved.connect(self.on_preview_item_moved)
        self.preview.itemSelected.connect(self.on_preview_item_selected)
        preview_column.addWidget(preview_label)
        preview_column.addWidget(self.preview, stretch=1)

        main_layout.addLayout(controls, stretch=3)
        main_layout.addLayout(preview_column, stretch=4)

    def build_text_group(self):
        group = QGroupBox("متن و موقعیت هر بخش")
        layout = QGridLayout()
        headers = ["بخش", "متن", "X mm", "Y mm"]
        for col, title in enumerate(headers):
            layout.addWidget(QLabel(title), 0, col)

        for row, item in enumerate(self.items, start=1):
            label = QLabel(item.label)
            text_edit = QLineEdit(item.text)
            text_edit.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
            text_edit.textChanged.connect(self.refresh_data)

            x_spin = self.position_spin(item.x)
            y_spin = self.position_spin(item.y)

            self.text_fields.append(text_edit)
            self.x_fields.append(x_spin)
            self.y_fields.append(y_spin)

            layout.addWidget(label, row, 0)
            layout.addWidget(text_edit, row, 1)
            layout.addWidget(x_spin, row, 2)
            layout.addWidget(y_spin, row, 3)

        group.setLayout(layout)
        return group

    def position_spin(self, value):
        spin = QDoubleSpinBox()
        spin.setRange(-500.0, 500.0)
        spin.setDecimals(2)
        spin.setSingleStep(0.5)
        spin.setValue(value)
        spin.valueChanged.connect(self.refresh_data)
        return spin

    def build_font_group(self):
        group = QGroupBox("تنظیمات فونت و خروجی")
        layout = QFormLayout()

        self.font_selector = QComboBox()
        families = self.preferred_font_families()
        self.font_selector.addItems(families)
        self.font_selector.currentTextChanged.connect(self.refresh_data)

        self.font_size_spin = QDoubleSpinBox()
        self.font_size_spin.setRange(4.0, 120.0)
        self.font_size_spin.setValue(6.0)
        self.font_size_spin.setSingleStep(0.5)
        self.font_size_spin.valueChanged.connect(self.refresh_data)

        self.work_width_spin = QDoubleSpinBox()
        self.work_width_spin.setRange(10.0, 1000.0)
        self.work_width_spin.setValue(86.0)
        self.work_width_spin.setSingleStep(1.0)
        self.work_width_spin.valueChanged.connect(self.refresh_data)

        self.work_height_spin = QDoubleSpinBox()
        self.work_height_spin.setRange(10.0, 1000.0)
        self.work_height_spin.setValue(54.0)
        self.work_height_spin.setSingleStep(1.0)
        self.work_height_spin.valueChanged.connect(self.refresh_data)

        self.feed_rate_spin = QDoubleSpinBox()
        self.feed_rate_spin.setRange(10.0, 10000.0)
        self.feed_rate_spin.setValue(900.0)
        self.feed_rate_spin.setSingleStep(50.0)

        self.travel_rate_spin = QDoubleSpinBox()
        self.travel_rate_spin.setRange(10.0, 20000.0)
        self.travel_rate_spin.setValue(2500.0)
        self.travel_rate_spin.setSingleStep(100.0)

        self.laser_power_spin = QDoubleSpinBox()
        self.laser_power_spin.setRange(0.0, 1000.0)
        self.laser_power_spin.setValue(450.0)
        self.laser_power_spin.setSingleStep(25.0)

        layout.addRow("فونت:", self.font_selector)
        layout.addRow("اندازه فونت:", self.font_size_spin)
        layout.addRow("عرض ناحیه کار mm:", self.work_width_spin)
        layout.addRow("ارتفاع ناحیه کار mm:", self.work_height_spin)
        layout.addRow("سرعت حکاکی:", self.feed_rate_spin)
        layout.addRow("سرعت حرکت آزاد:", self.travel_rate_spin)
        layout.addRow("قدرت لیزر S:", self.laser_power_spin)
        group.setLayout(layout)
        return group

    def build_device_group(self):
        group = QGroupBox("ارسال به دستگاه")
        layout = QFormLayout()

        self.com_port_edit = QLineEdit("COM3")
        self.com_port_edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.baud_spin = QSpinBox()
        self.baud_spin.setRange(300, 250000)
        self.baud_spin.setValue(115200)

        self.send_button = QPushButton("ارسال به دستگاه")
        self.send_button.clicked.connect(self.on_send)
        self.send_button.setEnabled(serial is not None)

        status = "pySerial آماده است." if serial is not None else "pySerial نصب نیست؛ فقط ذخیره فایل فعال است."
        self.device_status = QLabel(status)
        self.device_status.setWordWrap(True)

        layout.addRow("پورت:", self.com_port_edit)
        layout.addRow("Baud:", self.baud_spin)
        layout.addRow(self.send_button)
        layout.addRow(self.device_status)
        group.setLayout(layout)
        return group

    def build_buttons(self):
        layout = QHBoxLayout()
        self.generate_button = QPushButton("تولید G-code")
        self.generate_button.clicked.connect(self.on_generate)
        self.save_button = QPushButton("ذخیره فایل")
        self.save_button.clicked.connect(self.on_save)
        layout.addWidget(self.generate_button)
        layout.addWidget(self.save_button)
        return layout

    def preferred_font_families(self):
        families = QFontDatabase.families()
        keywords = ["B ", "Nazanin", "Mitra", "Yekan", "Iran", "Vazir", "Sahel", "Shabnam", "Tahoma", "Arial"]
        filtered = [family for family in families if any(k.lower() in family.lower() for k in keywords)]
        return filtered or families

    def current_font(self):
        font_name = self.font_selector.currentText() if hasattr(self, "font_selector") else "Tahoma"
        font = QFont(font_name)
        font.setPixelSize(max(1, int(self.font_size_spin.value())))
        font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        return font

    def refresh_data(self):
        for index, item in enumerate(self.items):
            item.text = self.text_fields[index].text().strip()
            item.x = self.x_fields[index].value()
            item.y = self.y_fields[index].value()

        if hasattr(self, "preview"):
            self.preview.set_work_area(self.work_width_spin.value(), self.work_height_spin.value())
            self.preview.update_preview()

    def on_preview_item_selected(self, index):
        self.preview.set_selected_index(index)
        self.text_fields[index].setFocus()

    def on_preview_item_moved(self, index, x, y):
        self.x_fields[index].blockSignals(True)
        self.y_fields[index].blockSignals(True)
        self.x_fields[index].setValue(x)
        self.y_fields[index].setValue(y)
        self.x_fields[index].blockSignals(False)
        self.y_fields[index].blockSignals(False)
        self.items[index].x = x
        self.items[index].y = y
        self.preview.update_preview()

    def build_text_paths(self):
        shapes = []
        font = self.current_font()
        for item in self.items:
            if not item.text:
                continue
            path = QPainterPath()
            path.addText(item.x, item.y, font, item.text)
            polygons = path.toSubpathPolygons()
            shapes.append((item.key, polygons))
        return shapes

    def generate_gcode(self):
        feed_rate = self.feed_rate_spin.value()
        travel_rate = self.travel_rate_spin.value()
        power = self.laser_power_spin.value()

        lines = [
            "(Generated by laser_text_gcode.py)",
            "G21 ; units in millimeters",
            "G90 ; absolute positioning",
            "M05 ; laser off",
            f"G0 F{travel_rate:.0f}",
        ]

        for label, polygons in self.build_text_paths():
            lines.append(f"(Text: {label})")
            for polygon in polygons:
                if polygon.isEmpty():
                    continue
                start = polygon[0]
                lines.append(f"G0 X{start.x():.3f} Y{start.y():.3f}")
                lines.append(f"M03 S{power:.0f}")
                lines.append(f"G1 F{feed_rate:.0f}")
                for point in polygon[1:]:
                    lines.append(f"G1 X{point.x():.3f} Y{point.y():.3f}")
                lines.append(f"G1 X{start.x():.3f} Y{start.y():.3f}")
                lines.append("M05")
            lines.append("")

        lines.extend(["M05", "G0 X0 Y0", "M30"])
        return "\n".join(lines)

    def on_generate(self):
        self.refresh_data()
        self.gcode_text.setPlainText(self.generate_gcode())

    def on_save(self):
        gcode = self.gcode_text.toPlainText().strip() or self.generate_gcode()
        path, _ = QFileDialog.getSaveFileName(
            self,
            "ذخیره G-code",
            "laser_output.gcode",
            "G-code files (*.gcode *.nc *.tap);;All Files (*)",
        )
        if path:
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(gcode)
            self.device_status.setText(f"فایل ذخیره شد: {path}")

    def on_send(self):
        if serial is None:
            QMessageBox.warning(self, "ارسال ممکن نیست", "برای ارسال مستقیم، pyserial را نصب کنید.")
            return

        gcode = self.gcode_text.toPlainText().strip() or self.generate_gcode()
        self.gcode_text.setPlainText(gcode)
        port = self.com_port_edit.text().strip()
        baud = self.baud_spin.value()

        try:
            with serial.Serial(port, baudrate=baud, timeout=2) as ser:
                time.sleep(2.0)
                ser.reset_input_buffer()
                for line in gcode.splitlines():
                    command = line.strip()
                    if not command:
                        continue
                    ser.write((command + "\n").encode("ascii", errors="ignore"))
                    ser.readline()
                ser.flush()
            self.device_status.setText(f"G-code با موفقیت به {port} ارسال شد.")
        except Exception as exc:
            self.device_status.setText(f"خطا در ارسال: {exc}")


def main():
    app = QApplication(sys.argv)
    window = LaserTextGCodeApp()
    window.resize(1280, 760)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
