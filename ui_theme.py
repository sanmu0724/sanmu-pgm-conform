"""Quiet desktop theme shared by the catalogue and technical pages."""
from PyQt6.QtGui import QColor, QFont, QPalette


def theme_stylesheet():
    return '''
    QWidget { color: #26354a; font-size: 13px; }
    QMainWindow, QDialog { background: #f3f5f8; }
    QLabel { background: transparent; }
    QLabel#pageTitle { font-size: 20px; font-weight: 600; color: #1c2c43; }
    QLabel#mutedLabel { color: #63748a; }
    QGroupBox { background: #ffffff; border: 1px solid #dfe5ed; border-radius: 8px;
                margin-top: 15px; padding: 14px 10px 10px; font-weight: 600; }
    QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; color: #52647c; }
    QGroupBox#workflowStep { margin-top: 16px; padding-top: 9px; }
    QPushButton, QToolButton { background: #ffffff; border: 1px solid #cfd8e5;
        border-radius: 5px; padding: 6px 11px; min-height: 20px; font-weight: 500; }
    QPushButton:hover, QToolButton:hover { border-color: #779bc8; background: #f0f5fb; }
    QPushButton:pressed, QToolButton:pressed { background: #e5eef8; }
    QPushButton#primaryButton { background: #3167a4; border-color: #3167a4; color: #ffffff; }
    QPushButton#primaryButton:hover { background: #285b94; }
    QPushButton:disabled, QToolButton:disabled { color: #9ca8b9; background: #eef1f5; border-color: #dfe5ed; }
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: #ffffff;
        border: 1px solid #cfd8e5; border-radius: 5px; padding: 5px 8px; min-height: 22px; }
    QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus { border-color: #719ac9; }
    QLineEdit:disabled, QComboBox:disabled { color: #8f9bad; background: #edf0f4; }
    QPlainTextEdit, QListWidget, QTreeWidget, QTableWidget { background: #ffffff;
        border: 1px solid #dfe5ed; border-radius: 6px; selection-background-color: #e8f0fa;
        selection-color: #234a77; alternate-background-color: #f8fafc; }
    QListWidget::item { padding: 8px 10px; border: 1px solid #e7ecf2; border-radius: 5px; }
    QListWidget::item:selected { border-color: #8cadd4; background: #eaf2fc; }
    QListWidget::item:hover { background: #f3f7fc; }
    QHeaderView::section { background: #f2f5f9; color: #576c86; border: none;
        border-bottom: 1px solid #dfe5ed; padding: 7px 8px; font-weight: 600; }
    QTabWidget::pane { border: 1px solid #dfe5ed; border-radius: 6px; background: #ffffff; }
    QTabBar::tab { padding: 8px 16px; margin-right: 3px; background: #e9edf3;
        color: #67788e; border-top-left-radius: 5px; border-top-right-radius: 5px; }
    QTabBar::tab:selected { background: #ffffff; color: #285b94; font-weight: 600; }
    QProgressBar { border: 1px solid #dfe5ed; background: #e9eef5; border-radius: 4px;
        min-height: 16px; text-align: center; }
    QProgressBar::chunk { background: #729bc9; border-radius: 3px; }
    QMenu { background: #ffffff; border: 1px solid #dfe5ed; padding: 5px; }
    QMenu::item { padding: 7px 24px 7px 12px; }
    QMenu::item:selected { background: #e8f0fa; }
    QSplitter::handle { background: #e4e9f0; }
    QScrollBar:vertical { background: #f4f6f9; width: 10px; margin: 0; }
    QScrollBar::handle:vertical { background: #c5d0df; border-radius: 5px; min-height: 24px; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
    '''


def apply_theme(app):
    """Apply once to QApplication. Source playback widgets keep native rendering."""
    if app is None or app.property('pgmThemeApplied'):
        return
    app.setStyle('Fusion')
    app.setFont(QFont('Helvetica Neue', 12))
    palette = QPalette()
    for role, color in ((QPalette.ColorRole.Window, '#f3f5f8'),
                        (QPalette.ColorRole.WindowText, '#26354a'),
                        (QPalette.ColorRole.Base, '#ffffff'),
                        (QPalette.ColorRole.AlternateBase, '#f8fafc'),
                        (QPalette.ColorRole.Text, '#26354a'),
                        (QPalette.ColorRole.Button, '#ffffff'),
                        (QPalette.ColorRole.ButtonText, '#26354a'),
                        (QPalette.ColorRole.Highlight, '#dceafa'),
                        (QPalette.ColorRole.HighlightedText, '#234a77')):
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    app.setStyleSheet(theme_stylesheet())
    app.setProperty('pgmThemeApplied', True)
