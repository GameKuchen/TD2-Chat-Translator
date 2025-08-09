"""
log_simulator.py
==================

This utility provides a small graphical test harness for exercising a game
translation tool.  Given a log file produced by the game, it writes a new
"demo" log in a user‑chosen directory containing only the chat messages from
the original.  The program reads chat lines (those beginning with
``ChatMessage:``) from the source log, optionally capturing multiline
messages, and every five seconds appends the next chat message to the demo
log file.  Each time a message is written the application displays the
message in the window so that the tester can see what has been sent.

Usage
-----

Run the script directly with Python.  A simple GUI will open that lets
you pick the original log file and the directory in which to create the
demo log.  Once both selections have been made, press **Start** and the
simulator begins streaming chat messages into the demo log at five second
intervals.

The demo log is named after the source log: the original file name is
prefixed with ``demo_`` and saved to the chosen output directory.  If the
file already exists it will be overwritten when you start a new session.
You can stop the simulator at any time by closing the window.

"""

import os
import threading
import time
from pathlib import Path
from typing import List

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext
except Exception as e:
    # If tkinter is not available, inform the user clearly.  The GUI cannot
    # function without tkinter, so provide guidance on how to install it.
    raise ImportError(
        "This script requires Tkinter, which could not be imported. "
        "Make sure that your Python installation includes tkinter."
    ) from e


def extract_chat_messages(log_path: Path) -> List[str]:
    """
    Parse the provided log file and return a list of chat messages.

    A chat message is defined as any line containing the literal string
    "ChatMessage:".  The function also captures subsequent lines that do
    *not* start with ``[``, treating them as continuations of the
    previous chat message.  This accommodates multi‑line chat messages
    where the following lines are not prefaced with a timestamp.

    Parameters
    ----------
    log_path: Path
        Path to the original log file.

    Returns
    -------
    List[str]
        A list of chat messages in the order they appear in the log.
    """
    messages: List[str] = []
    try:
        with log_path.open('r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
    except FileNotFoundError:
        raise

    i = 0
    while i < len(lines):
        line = lines[i]
        # Determine if this line begins a chat message
        if "ChatMessage:" in line:
            message = line.rstrip('\n')
            i += 1
            # Collect continuation lines until a line starting with '[' or
            # until end of file
            while i < len(lines) and not lines[i].startswith('['):
                message += '\n' + lines[i].rstrip('\n')
                i += 1
            messages.append(message)
        else:
            i += 1
    return messages


class LogSimulatorApp:
    """
    Graphical application for streaming chat messages from a game log.

    After selecting an input log file and an output directory, pressing
    **Start** will cause the application to write one chat message every
    five seconds to the demo log file.  Messages are read from top to
    bottom; once the end is reached the simulator stops.
    """

    POLL_INTERVAL = 15.0  # seconds between messages

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Log Chat Message Simulator")
        self.root.resizable(False, False)

        # Paths
        self.input_path: Path | None = None
        self.output_dir: Path | None = None
        self.messages: List[str] = []
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()

        # UI Elements
        self.file_label = tk.Label(root, text="Kein Log ausgewählt", anchor='w')
        self.file_button = tk.Button(root, text="Log wählen", command=self.select_file)
        self.dir_label = tk.Label(root, text="Kein Ausgabeordner ausgewählt", anchor='w')
        self.dir_button = tk.Button(root, text="Ordner wählen", command=self.select_dir)
        self.start_button = tk.Button(root, text="Start", command=self.start_simulation, state='disabled')
        self.status_text = scrolledtext.ScrolledText(root, height=10, width=80, state='disabled', wrap=tk.WORD)

        # Layout
        pad_options = {'padx': 5, 'pady': 5, 'sticky': 'w'}
        self.file_button.grid(row=0, column=0, **pad_options)
        self.file_label.grid(row=0, column=1, **pad_options)
        self.dir_button.grid(row=1, column=0, **pad_options)
        self.dir_label.grid(row=1, column=1, **pad_options)
        self.start_button.grid(row=2, column=0, columnspan=2, pady=(5, 10))
        self.status_text.grid(row=3, column=0, columnspan=2, padx=5, pady=(0, 5))

    def select_file(self) -> None:
        """Prompt the user to select the original log file."""
        file_path = filedialog.askopenfilename(
            title="Original Log Datei auswählen",
            filetypes=[("Log Dateien", "*.log"), ("Alle Dateien", "*.*")]
        )
        if file_path:
            self.input_path = Path(file_path)
            self.file_label.config(text=self.input_path.name)
        else:
            self.input_path = None
            self.file_label.config(text="Kein Log ausgewählt")
        self.update_start_state()

    def select_dir(self) -> None:
        """Prompt the user to select the directory for the demo log file."""
        dir_path = filedialog.askdirectory(title="Ausgabeordner auswählen")
        if dir_path:
            self.output_dir = Path(dir_path)
            self.dir_label.config(text=str(self.output_dir))
        else:
            self.output_dir = None
            self.dir_label.config(text="Kein Ausgabeordner ausgewählt")
        self.update_start_state()

    def update_start_state(self) -> None:
        """Enable the start button when both input and output paths are valid."""
        if self.input_path and self.output_dir:
            self.start_button.config(state='normal')
        else:
            self.start_button.config(state='disabled')

    def start_simulation(self) -> None:
        """
        Start reading chat messages and writing them to the demo log.

        This method extracts the chat messages, prepares the output file,
        and spins up a background thread to append messages at regular
        intervals.  If a simulation is already running it first stops
        the existing thread.
        """
        # Stop any existing simulation
        if self.thread and self.thread.is_alive():
            self.stop_event.set()
            self.thread.join(timeout=1.0)

        # Extract messages
        try:
            self.messages = extract_chat_messages(self.input_path)
        except FileNotFoundError:
            messagebox.showerror("Datei nicht gefunden", f"Die Datei {self.input_path} konnte nicht geöffnet werden.")
            return
        except Exception as err:
            messagebox.showerror("Fehler", f"Beim Lesen der Logdatei ist ein Fehler aufgetreten: {err}")
            return

        if not self.messages:
            messagebox.showinfo("Keine Nachrichten", "Es wurden keine ChatMessage-Einträge im Log gefunden.")
            return

        # Determine output file path.  Prefix the base name with 'demo_'.
        base_name = self.input_path.name
        output_name = f"demo_{base_name}"
        self.output_path = self.output_dir / output_name

        # Ensure the file is empty before starting
        try:
            with self.output_path.open('w', encoding='utf-8') as f:
                pass
        except Exception as err:
            messagebox.showerror("Fehler", f"Konnte Ausgabedatei {self.output_path} nicht anlegen: {err}")
            return

        # Clear previous status output
        self.status_text.config(state='normal')
        self.status_text.delete('1.0', tk.END)
        self.status_text.config(state='disabled')

        # Start background simulation
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run_simulation, daemon=True)
        self.thread.start()

    def _run_simulation(self) -> None:
        """
        Background worker that writes chat messages at fixed intervals.

        This method runs on a separate thread to avoid blocking the GUI.
        It iterates over the extracted messages, writes each one to the
        demo log, and schedules the UI update on the main thread.
        """
        for idx, message in enumerate(self.messages, start=1):
            if self.stop_event.is_set():
                break

            try:
                with self.output_path.open('a', encoding='utf-8') as f:
                    f.write(message + "\n")
            except Exception as err:
                self._append_status(f"Fehler beim Schreiben in die Ausgabedatei: {err}\n")
                break

            display_text = f"({idx}/{len(self.messages)}) Folgende Nachricht an Demo Log geschickt:\n{message}\n\n"
            self._append_status(display_text)

            # Sleep for the defined interval or until stop
            for _ in range(int(self.POLL_INTERVAL * 10)):
                if self.stop_event.is_set():
                    break
                time.sleep(0.1)
            if self.stop_event.is_set():
                break

        self._append_status("Simulation beendet.\n")

    def _append_status(self, text: str) -> None:
        """
        Append text to the status display area.

        This helper schedules the update on the Tkinter main loop thread
        because UI modifications must occur on the main thread.
        """
        def update():
            self.status_text.config(state='normal')
            self.status_text.insert(tk.END, text)
            self.status_text.see(tk.END)
            self.status_text.config(state='disabled')
        self.root.after(0, update)


def main() -> None:
    """Entry point for running the simulator standalone."""
    root = tk.Tk()
    app = LogSimulatorApp(root)
    root.protocol("WM_DELETE_WINDOW", root.quit)
    root.mainloop()


if __name__ == '__main__':
    main()