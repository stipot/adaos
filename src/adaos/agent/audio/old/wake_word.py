import json
import queue
import sounddevice as sd
from vosk import Model, KaldiRecognizer

WAKE_WORD = "ада"  # можно "ада ос" или любое слово


class WakeWordVosk:
    def __init__(self, model_path="models/vosk-model-small-ru-0.22", wake_word=WAKE_WORD):
        print(f"[WakeWord] Загружаю модель Vosk из {model_path} ...")
        self.model = Model(model_path)
        self.rec = KaldiRecognizer(self.model, 16000)
        # Ограничиваем распознавание словарём wake-word
        self.rec.SetGrammar([wake_word])
        self.q = queue.Queue()
        self.wake_word = wake_word

        self.stream = sd.RawInputStream(samplerate=16000, blocksize=8000, dtype="int16", channels=1, callback=self.audio_callback)

    def audio_callback(self, indata, frames, time, status):
        if status:
            print(f"[WakeWord] Status: {status}")
        self.q.put(bytes(indata))

    def wait_for_wake_word(self):
        print(f"[WakeWord] Жду команду: '{self.wake_word}' ...")
        with self.stream:
            while True:
                data = self.q.get()
                if self.rec.AcceptWaveform(data):
                    result = json.loads(self.rec.Result())
                    if result.get("text") == self.wake_word:
                        print(f"[WakeWord] Обнаружено слово: {self.wake_word}")
                        return True
