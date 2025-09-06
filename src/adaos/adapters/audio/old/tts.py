import subprocess

VOICES = ["anna", "elena", "pavel"]
EMOTIONS = ["neutral", "happy", "sad", "serious", "excited"]

def tts(text, emotion="neutral", voice="anna"):
    # RHVoice с параметрами
    if voice not in VOICES:
        voice = "anna"
    if emotion not in EMOTIONS:
        emotion = "neutral"    
    cmd = ["RHVoice-client", "-s", voice, "-p", emotion]
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    process.communicate(input=text.encode('utf-8'))
