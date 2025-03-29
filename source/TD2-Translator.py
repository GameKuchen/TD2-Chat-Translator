import os
import sys
import re
import tkinter as tk
from tkinter import filedialog, messagebox
import openai
import deepl
import requests
import configparser
from queue import Queue
from threading import Thread, Event
from PIL import Image, ImageTk
import httpcore
setattr(httpcore, 'SyncHTTPTransport', 'AsyncHTTPProxy')
from googletrans import Translator
import csv
from tkinter import ttk
import time
from concurrent.futures import ThreadPoolExecutor


def resource_path(relative_path):
    """ Get absolute path to resource, works for dev und for PyInstaller """
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)

config = configparser.ConfigParser()
config.read(resource_path('config.cfg'))
openai.api_key = config['DEFAULT']['OPENAI_API_KEY']
deepl_api_key = config['DEFAULT']['deepl_api_key']
current_version = "0.2.3"

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

        if not self.stop_event.is_set():
            self.text_widget.after(5000, self.check_new_lines)


    def translate_lines(self, lines):
        translated_lines = []
        tasks = []
        
        with ThreadPoolExecutor(max_workers=3) as executor:  # Bis zu 3 Übersetzungen parallel
            future_to_line = {}

            for line in lines:
                match_fd = re.search(r'^(.*?)\((\d{2}:\d{2}:\d{2})\) ([A-Za-z].*?@[^: ]+)(: | )(.*)$', line)
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
                assistant_id="asst_VQVZRc35HMmcZO7P00VaUeRg",
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


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Train Driver 2 Translation Helper")

        icon_path = resource_path(os.path.join('res', 'Favicon.ico'))
        if os.path.exists(icon_path):
            self.root.iconbitmap(icon_path)

        self.ignore_list = load_ignore_list(resource_path(os.path.join('res', 'ignore_list.csv')))
        self.fixed_translations = load_fixed_translations(resource_path(os.path.join('res', 'fixed_translations.csv')))

        self.language_var = tk.StringVar(self.root, "English")
        self.service_var = tk.StringVar(self.root, "Deepl")
        self.show_original = tk.BooleanVar()
        self.is_dark_mode = tk.BooleanVar(value=True)

        self.handlers = []   # (handler, queue, stop_event, text_area, tab_id)
        self.opened_logs = set()
        self.latest_log_time = None
        self.directory_path = ""
        self.known_logs = {} # Speichert log_file_path -> letzte bekannte mtime

        self.create_widgets()
        self.check_for_updates()

        self.apply_theme()

    def create_widgets(self):
        main_frame = tk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        top_frame = tk.Frame(main_frame)
        top_frame.pack(side=tk.TOP, fill=tk.X)

        img_path = resource_path(os.path.join('res', 'image.png'))
        if os.path.exists(img_path):
            img = Image.open(img_path).resize((80, 40), Image.LANCZOS)
            self.img_tk = ImageTk.PhotoImage(img)
            tk.Label(top_frame, image=self.img_tk).pack(side=tk.RIGHT)

        frame1 = tk.Frame(top_frame)
        frame1.pack(side=tk.LEFT, fill=tk.X, expand=True)

        tk.Label(frame1, text="TD2 Logs Path:").pack(side=tk.LEFT)
        self.file_entry = tk.Entry(frame1, width=50)
        self.file_entry.pack(side=tk.LEFT, padx=5)
        tk.Button(frame1, text="Browse", command=self.browse_directory).pack(side=tk.LEFT)

        frame2 = tk.Frame(main_frame)
        frame2.pack(pady=5, fill=tk.X)

        tk.Label(frame2, text="Target Language:").pack(side=tk.LEFT)
        language_values = ["English", "American English", "German", "Polish", "French", "Spanish", "Italian", "Dutch",
                           "Portuguese", "Brazilian Portuguese", "Greek", "Swedish", "Danish", "Finnish", "Norwegian",
                           "Czech", "Slovak", "Hungarian", "Romanian", "Bulgarian", "Croatian", "Serbian", "Slovenian",
                           "Estonian", "Latvian", "Lithuanian", "Maltese", "Russian"]
        self.language_combobox = ttk.Combobox(frame2, textvariable=self.language_var, state="readonly", values=language_values)
        self.language_combobox.pack(side=tk.LEFT, padx=5)

        tk.Checkbutton(frame2, text="Show Original", variable=self.show_original).pack(side=tk.LEFT, padx=5)

        tk.Label(frame2, text="Translation Service:").pack(side=tk.LEFT, padx=5)
        service_values = ["ChatGPT", "Google Translate", "Deepl"]
        self.service_combobox = ttk.Combobox(frame2, textvariable=self.service_var, state="readonly", values=service_values)
        self.service_combobox.pack(side=tk.LEFT, padx=5)

        tk.Checkbutton(frame2, text="Dark Mode", variable=self.is_dark_mode, command=self.apply_theme).pack(side=tk.LEFT, padx=5)

        frame3 = tk.Frame(main_frame)
        frame3.pack(pady=5, fill=tk.X)
        tk.Button(frame3, text="Close Selected Tab", command=self.close_selected_tab).pack(side=tk.LEFT, padx=5)

        self.notebook = ttk.Notebook(main_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True, pady=10)

    def browse_directory(self):
        directory_path = filedialog.askdirectory(initialdir=os.path.expanduser("~/Documents/TTSK/TrainDriver2/Logs"), title="Select Log Directory")
        if directory_path:
            self.directory_path = directory_path
            self.file_entry.delete(0, tk.END)
            self.file_entry.insert(0, directory_path)

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

        frame = tk.Frame(self.notebook)
        tab_id = self.notebook.add(frame, text=os.path.basename(log_file_path))

        text_area = tk.Text(frame, wrap=tk.WORD, height=20, width=80)
        text_area.pack(fill=tk.BOTH, expand=True)
        text_area.tag_config('fahrdienstleiter', foreground='#DF7676', font=("Helvetica", 10, "bold"))
        text_area.tag_config('translated', foreground='orange', font=("Helvetica", 10, "bold"))
        text_area.tag_config('swdr', foreground='green', font=("Helvetica", 10, "bold"))
        text_area.tag_config('original', font=("Helvetica", 10, "bold"))
        self.apply_theme()

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

        self.handlers.append((handler, queue, stop_event, text_area, tab_id))
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

        self.root.after(10000, self.monitor_new_logs)

    def apply_theme(self):
        if self.is_dark_mode.get():
            bg_color = "#2E2E2E"
            fg_color = "#FFFFFF"
            text_area_bg = "#3E3E3E"
            text_area_fg = "#FFFFFF"
            button_bg = "#4E4E4E"
            button_fg = "#FFFFFF"
        else:
            bg_color = "#FFFFFF"
            fg_color = "#000000"
            text_area_bg = "#FFFFFF"
            text_area_fg = "#000000"
            button_bg = "#F0F0F0"
            button_fg = "#000000"

        self.root.configure(bg=bg_color)

        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Light.TCombobox", fieldbackground="#000000", foreground="#000000")
        style.configure("Dark.TCombobox", fieldbackground="#000000", foreground="#000000")

        if self.is_dark_mode.get():
            self.language_combobox.configure(style="Dark.TCombobox")
            self.service_combobox.configure(style="Dark.TCombobox")
        else:
            self.language_combobox.configure(style="Light.TCombobox")
            self.service_combobox.configure(style="Light.TCombobox")

        self._update_widget_theme(self.root, bg_color, fg_color, text_area_bg, text_area_fg, button_bg, button_fg)
        self.apply_original_tag_colors()

    def apply_original_tag_colors(self):
        # Original-Tag-Farbe je nach Dark-Mode
        original_fg = "#FFFFFF" if self.is_dark_mode.get() else "#000000"
        for handler, queue, stop_event, text_area, tab_id in self.handlers:
            text_area.tag_config("original", foreground=original_fg)

    def _update_widget_theme(self, widget, bg_color, fg_color, text_area_bg, text_area_fg, button_bg, button_fg):
        widget_type = widget.winfo_class()
        if widget_type in ["Frame", "LabelFrame"]:
            widget.configure(bg=bg_color)
        elif widget_type == "Label":
            widget.configure(bg=bg_color, fg=fg_color)
        elif widget_type == "Entry":
            widget.configure(bg=text_area_bg, fg=text_area_fg)
        elif widget_type == "Text":
            widget.configure(bg=text_area_bg, fg=text_area_fg)
        elif widget_type == "Button":
            widget.configure(bg=button_bg, fg=button_fg)
        elif widget_type == "Checkbutton":
            widget.configure(bg=bg_color, fg=fg_color, selectcolor=bg_color)

        for child in widget.winfo_children():
            self._update_widget_theme(child, bg_color, fg_color, text_area_bg, text_area_fg, button_bg, button_fg)

    def process_queue_for_handler(self, handler, queue, text_area, stop_event):
        while True:
            lines = queue.get()
            if stop_event.is_set():
                queue.task_done()
                break
            translated_lines = handler.translate_lines(lines)
            for line, line_type in translated_lines:
                text_area.insert(tk.END, f"{line}\n", line_type)
                text_area.see(tk.END)
            queue.task_done()

    def close_selected_tab(self):
        current_tab = self.notebook.index(self.notebook.select())
        if current_tab == -1:
            return
        handler, queue, stop_event, text_area, tab_id = self.handlers[current_tab]
        stop_event.set()
        if handler.file:
            handler.file.close()
        self.notebook.forget(current_tab)
        del self.handlers[current_tab]

    def check_for_updates(self):
        try:
            response = requests.get("https://api.github.com/repos/bravuralion/TD2-Chat-Translator/releases/latest")
            response.raise_for_status()
            latest_release = response.json()
            latest_version = latest_release['tag_name']
            if latest_version > current_version:
                if messagebox.askyesno("Update Available", f"A new version {latest_version} is available. Download?"):
                    download_url = latest_release['assets'][0]['browser_download_url']
                    os.system(f"start {download_url}")
        except Exception:
            pass

    def on_closing(self):
        for handler, queue, stop_event, text_area, tab_id in self.handlers:
            stop_event.set()
            if handler.file:
                handler.file.close()
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()
