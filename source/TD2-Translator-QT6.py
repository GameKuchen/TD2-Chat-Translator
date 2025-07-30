import os
import sys
import re
from PyQt6 import QtWidgets, QtGui, QtCore
from PyQt6.QtWidgets import (QMainWindow, QApplication, QWidget, QVBoxLayout, 
                           QHBoxLayout, QLabel, QLineEdit, QPushButton, QComboBox,
                           QCheckBox, QTabWidget, QTextEdit, QFileDialog)
from PyQt6.QtCore import Qt, QTimer
import openai
import deepl
import requests
import configparser
from queue import Queue
from threading import Thread, Event
from PIL import Image
import httpcore
setattr(httpcore, 'SyncHTTPTransport', 'AsyncHTTPProxy')
from googletrans import Translator
import csv
from concurrent.futures import ThreadPoolExecutor

current_version = "0.2.8"

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev und for PyInstaller """
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)

config = configparser.ConfigParser()
config.read(resource_path('config.cfg'))
openai.api_key = config['DEFAULT']['OPENAI_API_KEY']
deepl_api_key = config['DEFAULT']['deepl_api_key']
current_version = "0.2.8"

def load_ignore_list(filepath):
    with open(filepath, 'r', encoding='utf-8') as file:
        return {line.strip() for line in file}

def load_fixed_translations(filepath):
    fixed_translations = {}
    with open(filepath, 'r', encoding='utf-8') as file:
        reader = csv.DictReader(file)
        for row in reader:
            text = row['text']
            language = row['language']
            translation = row['translation']
            if text not in fixed_translations:
                fixed_translations[text] = {}
            fixed_translations[text][language] = translation
    return fixed_translations

class LogHandler:
    def __init__(self, log_file_path, text_widget, language_var, service_var, queue, stop_event, show_original, ignore_list, fixed_translations):
        self.log_file_path = log_file_path
        self.file = open(log_file_path, 'r', encoding='utf-8')
        self.text_widget = text_widget
        self.language_var = language_var
        self.service_var = service_var
        self.queue = queue
        self.stop_event = stop_event
        self.show_original = show_original
        self.ignore_list = ignore_list
        self.fixed_translations = fixed_translations
        self.translator = Translator()
        self.deepl_translator = deepl.Translator(deepl_api_key)
        self.last_position = self.file.tell()

        # Timer für regelmäßige Überprüfung neuer Zeilen
        self.timer = QTimer()
        self.timer.timeout.connect(self.check_new_lines)
        self.timer.start(5000)

    @staticmethod
    def contains_time(line):
        return re.search(r'\(\d{2}:\d{2}:\d{2}\)', line) is not None

    @staticmethod
    def clean_chat_message(line):
        chat_message = re.search(r'ChatMessage: (.*)', line)
        if chat_message:
            return re.sub(r'<.*?>', '', chat_message.group(1))
        return ""

    def check_new_lines(self):
        if self.stop_event.is_set() or not self.file:
            return

        self.file.seek(self.last_position)
        lines = []
        while (line := self.file.readline()):
            if "ChatMessage:" in line and self.contains_time(line):
                clean_line = self.clean_chat_message(line)
                if clean_line:
                    lines.append(clean_line)

        if lines:
            self.last_position = self.file.tell()
            self.queue.put(lines)

    def translate_lines(self, lines):
        translated_lines = []
        tasks = []
        
        with ThreadPoolExecutor(max_workers=3) as executor:  # Bis zu 3 Übersetzungen parallel
            future_to_line = {}

            for line in lines:
                match_fd = re.search(r'^(.*?)\((\d{2}:\d{2}:\d{2})\) ([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż].*?@[^: ]+)(: | )(.*)$', line)
                match_player = re.search(r'^(.*?)\((\d{2}:\d{2}:\d{2})\) (\d+@[^: ]+)(: | )(.*)$', line)
                match_swdr = re.search(r'^(.*?)\((\d{2}:\d{2}:\d{2})\) \[(.*? \((.*?)\))\] (.*)$', line)

                if match_fd:
                    timestamp_user, message = match_fd.group(1) + "(" + match_fd.group(2) + ") " + match_fd.group(3), match_fd.group(5).strip()
                    tag = "fahrdienstleiter"
                elif match_player:
                    timestamp_user, message = match_player.group(1) + "(" + match_player.group(2) + ") " + match_player.group(3), match_player.group(5).strip()
                    tag = "translated"
                elif match_swdr:
                    timestamp_user, message = match_swdr.group(1) + "(" + match_swdr.group(2) + ") [" + match_swdr.group(3) + "]", match_swdr.group(5).strip()
                    tag = "swdr"
                else:
                    continue

                if message in self.ignore_list:
                    continue

                current_target_language = self.language_var.get()
                translation_service = self.service_var.get()
                self.target_language = current_target_language

                # Starte die Übersetzung parallel
                future = executor.submit(self.translate_message, message, translation_service)
                future_to_line[future] = (timestamp_user, message, tag)

            # Ergebnisse einsammeln, sobald sie fertig sind
            for future in future_to_line:
                timestamp_user, message, tag = future_to_line[future]
                translation = future.result()  # Wartet auf die Übersetzung

                translation = re.sub(r'【[^】]*】', '', translation).strip()

                if self.show_original.get():
                    translated_lines.append((f"Original: {timestamp_user}: {message}", "original"))
                translated_lines.append((f"Translated: {timestamp_user}: {translation}", tag))

        return translated_lines

    def translate_message(self, text, translation_service):
        if translation_service == "ChatGPT":
            return self.translate_with_chatgpt(text)
        elif translation_service == "Google Translate":
            return self.translate_with_google(text)
        elif translation_service == "Deepl":
            return self.translate_with_deepl(text)

    def translate_with_chatgpt(self, text):
        try:
            client = openai.OpenAI(api_key=openai.api_key)
            thread = client.beta.threads.create()
            client.beta.threads.messages.create(
                thread_id=thread.id,
                role="user",
                content=f"Translate the following Sentence to {self.target_language}, Only provide the translation without any explanations or additional text. If there are parts that cannot be translated (e.g., names, emojis), leave those unchanged, and translate the rest. if necessary translate word by word: {text}"
            )

            run = client.beta.threads.runs.create_and_poll(
                thread_id=thread.id,
                assistant_id="asst_dxWUY2bN5TSwZXi09Q7HKITj",
                instructions="You are a translator. Translate the text provided to you to the requested languages without any additional explanations. The Source can be in multiple languages. Refer to the uploaded translations PDF first for predefined translations. Only reply with the requested target language."
            )

            if run.status == 'completed':
                messages = client.beta.threads.messages.list(thread_id=thread.id)
                message_data = messages.data
                if message_data:
                    last_message = message_data[0]
                    if last_message.content:
                        text_content_block = last_message.content[0]
                        return text_content_block.text.value.strip()
                    else:
                        return "No content found in the last message"
                else:
                    return "No messages found"
            else:
                return run.status
        except Exception as e:
            return str(e)

    def translate_with_google(self, text):
        try:
            translation = self.translator.translate(text, dest=self.target_language)
            return translation.text
        except Exception as e:
            return str(e)

    def translate_with_deepl(self, text):
        target_lang_code = self.get_deepl_language_code(self.target_language)
        if not target_lang_code:
            return f"Target language '{self.target_language}' not supported by Deepl"
        try:
            result = self.deepl_translator.translate_text(text, target_lang=target_lang_code)
            return result.text
        except Exception as e:
            return str(e)

    @staticmethod
    def get_deepl_language_code(language):
        language_codes = {
            "Bulgarian": "BG",
            "Czech": "CS",
            "Danish": "DA",
            "German": "DE",
            "Greek": "EL",
            "English": "EN-GB",
            "American English": "EN-US",
            "Spanish": "ES",
            "Estonian": "ET",
            "Finnish": "FI",
            "French": "FR",
            "Hungarian": "HU",
            "Italian": "IT",
            "Japanese": "JA",
            "Lithuanian": "LT",
            "Latvian": "LV",
            "Dutch": "NL",
            "Polish": "PL",
            "Portuguese": "PT-PT",
            "Brazilian Portuguese": "PT-BR",
            "Romanian": "RO",
            "Russian": "RU",
            "Slovak": "SK",
            "Slovenian": "SL",
            "Swedish": "SV",
            "Chinese": "ZH"
        }
        return language_codes.get(language, None)


class App(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Train Driver 2 Translation Helper")
        
        # Setup central widget
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QVBoxLayout(self.central_widget)
        
        # Initialize variables
        self.overlay_window = None
        self.overlay_font_size = 10
        self.language_var = "English"
        self.service_var = "Deepl"
        self.show_original = False
        self.is_dark_mode = True
        self.handlers = []
        self.opened_logs = set()
        self.latest_log_time = None
        self.directory_path = ""
        self.known_logs = {}

        # Load resources
        icon_path = resource_path(os.path.join('res', 'Favicon.ico'))
        if os.path.exists(icon_path):
            self.setWindowIcon(QtGui.QIcon(icon_path))
            
        self.ignore_list = load_ignore_list(resource_path(os.path.join('res', 'ignore_list.csv')))
        self.fixed_translations = load_fixed_translations(resource_path(os.path.join('res', 'fixed_translations.csv')))

        # Create UI
        self.create_widgets()
        self.check_for_updates()
        self.apply_theme()

        # Setup monitoring timer
        self.monitor_timer = QTimer(self)
        self.monitor_timer.timeout.connect(self.monitor_new_logs)
        self.monitor_timer.start(10000)

    def create_widgets(self):
        # Top section
        top_layout = QHBoxLayout()
        self.main_layout.addLayout(top_layout)

        # Logo
        img_path = resource_path(os.path.join('res', 'image.png'))
        if os.path.exists(img_path):
            pixmap = QtGui.QPixmap(img_path).scaled(80, 40, Qt.AspectRatioMode.KeepAspectRatio)
            img_label = QLabel()
            img_label.setPixmap(pixmap)
            top_layout.addWidget(img_label)

        # Path section
        path_layout = QHBoxLayout()
        self.main_layout.addLayout(path_layout)
        
        path_layout.addWidget(QLabel("TD2 Logs Path:"))
        self.file_entry = QLineEdit()
        path_layout.addWidget(self.file_entry)
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self.browse_directory)
        path_layout.addWidget(browse_btn)

        # Controls section
        controls_layout = QHBoxLayout()
        self.main_layout.addLayout(controls_layout)

        # Language selection
        controls_layout.addWidget(QLabel("Target Language:"))
        self.language_combobox = QComboBox()
        self.language_combobox.addItems([
            "English", "American English", "German", "Polish", "French", "Spanish", "Italian", "Dutch",
            "Portuguese", "Brazilian Portuguese", "Greek", "Swedish", "Danish", "Finnish", "Norwegian",
            "Czech", "Slovak", "Hungarian", "Romanian", "Bulgarian", "Croatian", "Serbian", "Slovenian",
            "Estonian", "Latvian", "Lithuanian", "Maltese", "Russian"
        ])
        controls_layout.addWidget(self.language_combobox)

        # Show original checkbox
        self.show_original_checkbox = QCheckBox("Show Original")
        controls_layout.addWidget(self.show_original_checkbox)

        # Translation service
        controls_layout.addWidget(QLabel("Translation Service:"))
        self.service_combobox = QComboBox()
        self.service_combobox.addItems(["ChatGPT", "Google Translate", "Deepl"])
        controls_layout.addWidget(self.service_combobox)

        # Buttons section
        buttons_layout = QHBoxLayout()
        self.main_layout.addLayout(buttons_layout)

        close_tab_btn = QPushButton("Close Selected Tab")
        close_tab_btn.clicked.connect(self.close_selected_tab)
        buttons_layout.addWidget(close_tab_btn)

        overlay_btn = QPushButton("Toggle Overlay")
        overlay_btn.clicked.connect(self.toggle_overlay)
        buttons_layout.addWidget(overlay_btn)

        font_size_layout = QHBoxLayout()
        buttons_layout.addLayout(font_size_layout)
        
        aplus_btn = QPushButton("A+")
        aplus_btn.clicked.connect(lambda: self.change_overlay_font_size(1))
        font_size_layout.addWidget(aplus_btn)
        
        aminus_btn = QPushButton("A−")
        aminus_btn.clicked.connect(lambda: self.change_overlay_font_size(-1))
        font_size_layout.addWidget(aminus_btn)

        self.dark_mode_checkbox = QCheckBox("Dark Mode")
        self.dark_mode_checkbox.setChecked(self.is_dark_mode)
        self.dark_mode_checkbox.stateChanged.connect(self.apply_theme)
        buttons_layout.addWidget(self.dark_mode_checkbox)

        # Tab widget
        self.notebook = QTabWidget()
        self.main_layout.addWidget(self.notebook)

    def apply_theme(self):
        self.is_dark_mode = self.dark_mode_checkbox.isChecked()
        if self.is_dark_mode:
            self.setStyleSheet("""
                QMainWindow, QWidget {
                    background-color: #2E2E2E;
                    color: #FFFFFF;
                }
                QTextEdit {
                    background-color: #3E3E3E;
                    color: #FFFFFF;
                }
                QPushButton {
                    background-color: #4E4E4E;
                    color: #FFFFFF;
                    border: none;
                    padding: 5px;
                }
                QComboBox {
                    background-color: #3E3E3E;
                    color: #FFFFFF;
                }
                QLineEdit {
                    background-color: #3E3E3E;
                    color: #FFFFFF;
                }
            """)
        else:
            self.setStyleSheet("""
                QMainWindow, QWidget {
                    background-color: #FFFFFF;
                    color: #000000;
                }
                QTextEdit {
                    background-color: #FFFFFF;
                    color: #000000;
                }
                QPushButton {
                    background-color: #F0F0F0;
                    color: #000000;
                    border: 1px solid #CCCCCC;
                    padding: 5px;
                }
                QComboBox {
                    background-color: #FFFFFF;
                    color: #000000;
                }
                QLineEdit {
                    background-color: #FFFFFF;
                    color: #000000;
                }
            """)

    def browse_directory(self):
        directory_path = QFileDialog.getExistingDirectory(self, "Select Log Directory", os.path.expanduser("~/Documents/TTSK/TrainDriver2/Logs"))
        if directory_path:
            self.directory_path = directory_path
            self.file_entry.setText(directory_path)

            newest = self.find_newest_log_file(directory_path)
            if newest:
                self.open_log_in_new_tab(newest)
                self.latest_log_time = os.path.getctime(newest)

            # Alle Logs merken (mtime)
            self.record_all_logs()

            self.monitor_new_logs()

    def record_all_logs(self):
        # Speichere mtime aller Logs, damit wir Veränderungen feststellen können
        if not self.directory_path:
            return
        log_files = [os.path.join(self.directory_path, f) for f in os.listdir(self.directory_path)
                     if os.path.isfile(os.path.join(self.directory_path, f)) and "Log" in f]
        for lf in log_files:
            self.known_logs[lf] = os.path.getmtime(lf)

    def find_newest_log_file(self, directory_path):
        log_files = [os.path.join(directory_path, f) for f in os.listdir(directory_path)
                     if os.path.isfile(os.path.join(directory_path, f)) and "Log" in f]
        if not log_files:
            return None
        return max(log_files, key=os.path.getctime)

    def open_log_in_new_tab(self, log_file_path):
        if log_file_path in self.opened_logs:
            return
        self.opened_logs.add(log_file_path)

        frame = QWidget()
        self.notebook.addTab(frame, os.path.basename(log_file_path))

        text_area = QTextEdit()
        text_area.setAcceptRichText(True)
        text_area.setReadOnly(True)
        frame_layout = QVBoxLayout(frame)
        frame_layout.addWidget(text_area)

        text_area.setStyleSheet("QTextEdit { font-family: 'Helvetica'; font-size: 10pt; }")

        text_area.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)

        text_area.setText(f"Loading log file: {log_file_path}")

        queue = Queue()
        stop_event = Event()
        handler = LogHandler(
            log_file_path=log_file_path,
            text_widget=text_area,
            language_var=self.language_var,
            service_var=self.service_var,
            queue=queue,
            stop_event=stop_event,
            show_original=self.show_original,
            ignore_list=self.ignore_list,
            fixed_translations=self.fixed_translations
        )

        handler.file.seek(0, os.SEEK_END)
        latest_message = None
        while (line := handler.file.readline()):
            if "ChatMessage:" in line and handler.contains_time(line):
                clean_line = handler.clean_chat_message(line)
                if clean_line:
                    latest_message = clean_line

        if latest_message:
            queue.put([latest_message])

        handler.last_position = handler.file.tell()
        handler.check_new_lines()

        t = Thread(target=self.process_queue_for_handler, args=(handler, queue, text_area, stop_event), daemon=True)
        t.start()

        self.handlers.append((handler, queue, stop_event, text_area))
        self.apply_original_tag_colors()

    def monitor_new_logs(self):
        if self.directory_path:
            # Prüfe alle Logs erneut
            log_files = [os.path.join(self.directory_path, f) for f in os.listdir(self.directory_path)
                         if os.path.isfile(os.path.join(self.directory_path, f)) and "Log" in f]
            for lf in log_files:
                mtime = os.path.getmtime(lf)
                if lf not in self.opened_logs:
                    # Kann dieses ältere Log aktiv sein?
                    # Wir vergleichen mtime mit previously known mtime
                    old_mtime = self.known_logs.get(lf, None)
                    if old_mtime is not None and mtime > old_mtime:
                        # Log hat sich geändert, also aktiv geworden
                        self.open_log_in_new_tab(lf)
                # Aktualisiere known_logs mit neuem mtime
                self.known_logs[lf] = mtime

    def apply_theme(self):
        self.is_dark_mode = self.dark_mode_checkbox.isChecked()
        if self.is_dark_mode:
            self.setStyleSheet("""
                QMainWindow, QWidget {
                    background-color: #2E2E2E;
                    color: #FFFFFF;
                }
                QTextEdit {
                    background-color: #3E3E3E;
                    color: #FFFFFF;
                }
                QPushButton {
                    background-color: #4E4E4E;
                    color: #FFFFFF;
                    border: none;
                    padding: 5px;
                }
                QComboBox {
                    background-color: #3E3E3E;
                    color: #FFFFFF;
                }
                QLineEdit {
                    background-color: #3E3E3E;
                    color: #FFFFFF;
                }
            """)
        else:
            self.setStyleSheet("""
                QMainWindow, QWidget {
                    background-color: #FFFFFF;
                    color: #000000;
                }
                QTextEdit {
                    background-color: #FFFFFF;
                    color: #000000;
                }
                QPushButton {
                    background-color: #F0F0F0;
                    color: #000000;
                    border: 1px solid #CCCCCC;
                    padding: 5px;
                }
                QComboBox {
                    background-color: #FFFFFF;
                    color: #000000;
                }
                QLineEdit {
                    background-color: #FFFFFF;
                    color: #000000;
                }
            """)

    def process_queue_for_handler(self, handler, queue, text_area, stop_event):
        while True:
            lines = queue.get()
            if stop_event.is_set():
                queue.task_done()
                break
            translated_lines = handler.translate_lines(lines)
            for line, line_type in translated_lines:
                text_area.append(f"{line}")
                text_area.moveCursor(Qt.TextCursor.MoveOperation.End)
            queue.task_done()

    def close_selected_tab(self):
        current_tab = self.notebook.currentIndex()
        if current_tab == -1:
            return
        handler, queue, stop_event, text_area = self.handlers[current_tab]
        stop_event.set()
        if handler.file:
            handler.file.close()
        self.notebook.removeTab(current_tab)
        del self.handlers[current_tab]

    def check_for_updates(self):
        try:
            response = requests.get("https://api.github.com/repos/bravuralion/TD2-Chat-Translator/releases/latest")
            response.raise_for_status()
            latest_release = response.json()
            latest_version = latest_release['tag_name']
            if latest_version > current_version:
                if QtWidgets.QMessageBox.question(self, "Update Available", f"A new version {latest_version} is available. Download?") == QtWidgets.QMessageBox.Yes:
                    download_url = latest_release['assets'][0]['browser_download_url']
                    os.system(f"start {download_url}")
        except Exception:
            pass

    def on_closing(self):
        for handler, queue, stop_event, text_area in self.handlers:
            stop_event.set()
            if handler.file:
                handler.file.close()
        if self.overlay_window and self.overlay_window.isVisible():
            self.overlay_window.close()
        self.close()

    def toggle_overlay(self):
        if self.overlay_window and self.overlay_window.isVisible():
            self.overlay_window.close()
            self.overlay_window = None
        else:
            self.overlay_window = QWidget()
            self.overlay_window.setWindowTitle("Overlay")
            self.overlay_window.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
            self.overlay_window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.overlay_window.resize(400, 200)
            self.overlay_window.move(100, 100)

            layout = QVBoxLayout(self.overlay_window)
            self.overlay_text = QTextEdit()
            self.overlay_text.setReadOnly(True)
            self.overlay_text.setStyleSheet("QTextEdit { font-family: 'Helvetica'; font-size: 10pt; }")
            layout.addWidget(self.overlay_text)

            # Draggable
            self.overlay_window.setMouseTracking(True)
            self.overlay_window.mousePressEvent = self.start_move
            self.overlay_window.mouseMoveEvent = self.do_move

            if self.handlers:
                handler, queue, stop_event, main_text_area = self.handlers[-1]
                self.start_overlay_sync(main_text_area)
            self.overlay_window.show()

    def start_overlay_sync(self, source_text_widget):
        def sync():
            if not self.overlay_window or not self.overlay_window.isVisible():
                return

            self.overlay_text.setPlainText(source_text_widget.toPlainText())

            # Alle Tags im Quell-Widget durchgehen
            for tag in source_text_widget.document().findChildren(QtGui.QTextBlock):
                if tag.userData():
                    self.overlay_text.document().findBlockByNumber(tag.blockNumber()).setUserData(tag.userData())

            self.overlay_text.moveCursor(Qt.TextCursor.MoveOperation.End)
            self.root.after(1000, sync)
        sync()

    def start_move(self, event):
        self._x = event.globalX()
        self._y = event.globalY()

    def do_move(self, event):
        deltax = event.globalX() - self._x
        deltay = event.globalY() - self._y
        x = self.overlay_window.x() + deltax
        y = self.overlay_window.y() + deltay
        self.overlay_window.move(x, y)

        self._x = event.globalX()
        self._y = event.globalY()

    def change_overlay_font_size(self, delta):
        if not self.overlay_window or not self.overlay_window.isVisible():
            return
        self.overlay_font_size = max(6, self.overlay_font_size + delta)
        self.overlay_text.setStyleSheet(f"QTextEdit {{ font-family: 'Helvetica'; font-size: {self.overlay_font_size}pt; }}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = App()
    window.show()
    sys.exit(app.exec())
