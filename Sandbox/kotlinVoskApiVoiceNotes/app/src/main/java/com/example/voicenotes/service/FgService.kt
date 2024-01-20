package com.example.voicenotes.service

import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Binder
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.Message
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import com.example.voicenotes.data.AppDatabase
import com.example.voicenotes.data.Line
import org.vosk.Model
import org.vosk.Recognizer
import org.vosk.android.RecognitionListener
import org.vosk.android.SpeechService
import org.vosk.android.StorageService


class FgService : Service() {
    private val binder = LocalBinder()
    private lateinit var handler: Handler
    var isCallbackRegistered = false
    var dbInstance: AppDatabase? = null

    private var speechService: SpeechService? = null

    private fun startForeground() {
        val notification = NotificationCompat.Builder(this, "service")
            .setContentTitle("Foreground Service")
            .setContentText("Service is running...")
            .setSmallIcon(com.example.voicenotes.R.drawable.ic_launcher_foreground)
            .setOngoing(true)
            .build()
        ServiceCompat.startForeground(
            this, 1, notification,
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            } else {
                0
            },
        )

    }

    override fun onBind(intent: Intent?): IBinder {
        return binder
    }

    inner class LocalBinder : Binder() {
        fun getService(): FgService = this@FgService
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        this.startForeground()
        this.initSpeechService()
        this.registerHandler()
        return START_STICKY
    }

    private fun registerHandler() {
        val c = this
        this.handler = Handler(Looper.myLooper()!!) {
            when (it.what) {
                HandlerMessages.StartRecognition -> {
                    // Initialize everything && start recognizer
                    val rec = Recognizer(model, 16000.0f)

                    speechService = SpeechService(rec, 16000.0f)
                    speechService!!.startListening(it.obj as RecognitionListener)
                    c.isCallbackRegistered = true
                }

                HandlerMessages.StopRecognition -> {
                    speechService?.stop()
                    speechService?.shutdown()
                    c.isCallbackRegistered = false
                }
            }
            true
        };
    }

    private lateinit var model: Model

    private fun initSpeechService() {
        StorageService.unpack(applicationContext, "model-ru-RU", "model",
            {
                model = it
            },
            {
                it.printStackTrace()
            }
        )
    }

    fun register(cb: RecognitionListener) {
        this.handler.sendMessage(Message().apply {
            what = HandlerMessages.StartRecognition; obj = cb
        })
    }

    fun unregister() {
        this.handler.sendMessage(Message().apply { what = HandlerMessages.StopRecognition })
    }
}

// SubscriptionCallbacks are called from a non-ui thread.
interface SubscriptionCallbacks {
    fun onLinesUpdate(lines: List<Line>)
}

object HandlerMessages {
    val StartRecognition = 1
    val StopRecognition = 2
}