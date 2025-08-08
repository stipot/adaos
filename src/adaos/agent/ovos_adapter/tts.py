from ovos_plugin_manager.tts import OVOSTTSFactory
from ovos_config.config import Configuration
import tempfile
import os
import playsound


class OVOSTTSAdapter:
    def __init__(self):
        conf = Configuration()
        tts_section = conf.get("tts", {})
        tts_module = tts_section.get("module", "ovos-tts-plugin-mimic3")
        plugin_conf = tts_section.get(tts_module, {})

        self.config = {"module": tts_module, tts_module: plugin_conf}

        # 👍 ВАЖНО: не делаем никаких ручных манипуляций с lang здесь!
        factory = OVOSTTSFactory()
        self.tts = factory.create(config=self.config)

    def say(self, text: str):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
            wav_file = tmp.name
        try:
            self.tts.get_tts(text, wav_file)
            playsound.playsound(wav_file)
        finally:
            os.remove(wav_file)


""" 
// ~/.config/mycroft/mycroft.conf
{
  "tts": {
    "module": "ovos-tts-plugin-mimic3",
    "ovos-tts-plugin-mimic3": {
      "voice": "en_UK/apope_low"
    }
  }
}

pip install ovos-tts-server ovos-tts-plugin-mimic3 ovos-config ovos-plugin-manager playsound==1.2.2
"""
