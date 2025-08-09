import queue
import sounddevice as sd
import vosk
import json

class VoiceRecognizer:
    def __init__(self, model_path="model"):
        self.model = vosk.Model(model_path)
        self.q = queue.Queue()

    def _callback(self, indata, frames, time, status):
        if status:
            print(status)
        self.q.put(bytes(indata))

    def listen(self):
        with sd.RawInputStream(samplerate=16000, blocksize=8000, dtype='int16',
                               channels=1, callback=self._callback):
            rec = vosk.KaldiRecognizer(self.model, 16000)
            print("[ASR] Скажите команду...")
            while True:
                data = self.q.get()
                if rec.AcceptWaveform(data):
                    result = json.loads(rec.Result())
                    if result.get("text"):
                        return result["text"]
