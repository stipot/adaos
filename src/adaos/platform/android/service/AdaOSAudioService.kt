package ai.adaos.platform.android

import android.app.Service
import android.content.Intent
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.os.IBinder
import kotlinx.coroutines.*
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress

class AdaOSAudioService : Service() {
    companion object {
        const val CHANNEL_ID = "adaos_audio_channel"
        const val NOTIF_ID = 1001

        // Настройки аудио/сокета
        const val SAMPLE_RATE = 16000
        const val CHANNEL_CONFIG = AudioFormat.CHANNEL_IN_MONO
        const val AUDIO_FORMAT = AudioFormat.ENCODING_PCM_16BIT
        const val UDP_PORT = 29100
        const val UDP_HOST = "127.0.0.1"
    }

    private var record: AudioRecord? = null
    private var job: Job? = null

    override fun onCreate() {
        super.onCreate()
        Foreground.ensureChannel(this, CHANNEL_ID, "AdaOS Audio")
        val notif = Foreground.buildNotification(
            ctx = this,
            channelId = CHANNEL_ID,
            title = "AdaOS is listening",
            text = "Voice capture active",
            notifId = NOTIF_ID
        )
        startForeground(
            NOTIF_ID, notif,
            if (Build.VERSION.SDK_INT >= 34)
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            else 0
        )
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startRecording()
        return START_STICKY
    }

    private fun startRecording() {
        if (job != null) return
        val minBuf = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL_CONFIG, AUDIO_FORMAT)
        val buffSize = maxOf(minBuf, 8192)

        record = AudioRecord(
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
            SAMPLE_RATE,
            CHANNEL_CONFIG,
            AUDIO_FORMAT,
            buffSize
        )

        val dest = InetAddress.getByName(UDP_HOST)
        val socket = DatagramSocket() // будет закрыт в корутине

        record?.startRecording()
        job = CoroutineScope(Dispatchers.Default).launch {
            val buf = ByteArray(buffSize)
            try {
                while (isActive) {
                    val read = record?.read(buf, 0, buf.size) ?: 0
                    if (read > 0) {
                        val pkt = DatagramPacket(buf, read, dest, UDP_PORT)
                        socket.send(pkt)
                    }
                }
            } catch (_: Throwable) {
            } finally {
                try { record?.stop() } catch (_: Throwable) {}
                try { record?.release() } catch (_: Throwable) {}
                try { socket.close() } catch (_: Throwable) {}
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        job?.cancel()
        job = null
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
