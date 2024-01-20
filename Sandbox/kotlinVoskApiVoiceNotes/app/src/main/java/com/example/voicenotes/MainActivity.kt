package com.example.voicenotes

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.ActivityResultLauncher
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.imePadding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.ui.Modifier
import com.example.voicenotes.service.FgService
import com.example.voicenotes.ui.theme.VoiceNotesTheme
import org.vosk.android.StorageService


class MainActivity : ComponentActivity() {
    private lateinit var permissionLauncher: ActivityResultLauncher<String>
    private var currentPermissionCallback: (() -> Unit)? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        registerPermissionLauncher()
        registerNotificationChannels()
        mustGetPermission(android.Manifest.permission.POST_NOTIFICATIONS) {
            mustGetPermission(android.Manifest.permission.RECORD_AUDIO) {
                val intent = Intent(this, FgService::class.java)
                startForegroundService(intent)

                // Start the next activity
                val activityIntent = Intent(this, NoteActivity::class.java)
                activityIntent.putExtra("noteId", "test")
                startActivity(activityIntent)
            }
        }

        setContent {
            VoiceNotesTheme {
                // A surface container using the 'background' color from the theme
                Surface(
                    modifier = Modifier
                        .fillMaxSize()
                        .imePadding(),
                    color = MaterialTheme.colorScheme.background
                ) {}
            }
        }

    }

    private fun registerPermissionLauncher() {
        permissionLauncher =
            registerForActivityResult(ActivityResultContracts.RequestPermission()) { isGranted: Boolean ->
                if (isGranted)
                    currentPermissionCallback?.invoke()
                else
                    finish()
            }
    }

    private fun mustGetPermission(perm: String, next: () -> Unit) {
        currentPermissionCallback = next
        when (applicationContext.checkSelfPermission(perm)) {
            PackageManager.PERMISSION_GRANTED -> {
                next()
            }

            else -> permissionLauncher.launch(perm)
        }
    }

    private fun registerNotificationChannels() {
        val serviceChannel = NotificationChannel(
            "service", "Foreground Service Channel", NotificationManager.IMPORTANCE_DEFAULT
        ).apply { description = "Foreground Service Channel" }
        val manager = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        manager.createNotificationChannel(serviceChannel)
    }
}


