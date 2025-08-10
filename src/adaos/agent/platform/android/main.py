from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.textinput import TextInput
from kivy.uix.button import Button
from kivy.clock import mainthread


def say_text(text: str):
    try:
        from adaos.platform.android.android_tts import AndroidTTS

        AndroidTTS(lang_hint="en-US").say(text)
    except Exception:
        from adaos.agent.audio.tts.native_tts import NativeTTS

        NativeTTS(lang_hint="en").say(text)


class Root(BoxLayout):
    def __init__(self, **kw):
        super().__init__(orientation="vertical", padding=12, spacing=8, **kw)
        self.input = TextInput(text="Hello world!", multiline=False, size_hint_y=None, height=48)
        self.btn = Button(text="Say", size_hint_y=None, height=48, on_press=self.on_say)
        self.add_widget(self.input)
        self.add_widget(self.btn)

    @mainthread
    def on_say(self, *_):
        txt = self.input.text.strip()
        if txt:
            say_text(txt)


class AdaOSApp(App):
    def build(self):
        return Root()


if __name__ == "__main__":
    AdaOSApp().run()
