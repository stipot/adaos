package com.example.voicenotes

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.Bundle
import android.os.IBinder
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.em
import androidx.compose.ui.unit.sp
import androidx.lifecycle.MutableLiveData
import androidx.lifecycle.ViewModelProvider
import androidx.room.Room
import com.example.voicenotes.data.AppDatabase
import com.example.voicenotes.data.Line
import com.example.voicenotes.data.LineRepository
import com.example.voicenotes.data.LineViewModel
import com.example.voicenotes.data.LineViewModelFactory
import com.example.voicenotes.data.LinesDao
import com.example.voicenotes.service.FgService
import com.example.voicenotes.ui.theme.VoiceNotesTheme
import com.google.gson.Gson
import org.vosk.android.RecognitionListener
import java.lang.Exception
import java.time.Duration
import java.util.Date
import java.util.UUID
import kotlin.concurrent.thread


class NoteActivity : ComponentActivity() {
    private lateinit var viewModel: LineViewModel

    private object ServiceBindings {
        lateinit var mService: FgService
        var mBound: Boolean = false
        var mServiceAvailable = MutableLiveData<FgService>()

        val connection = object : ServiceConnection {
            override fun onServiceConnected(className: ComponentName, service: IBinder) {
                val binder = service as FgService.LocalBinder
                mService = binder.getService()
                mBound = true
                mServiceAvailable.value = mService
            }

            override fun onServiceDisconnected(arg0: ComponentName) {
                mBound = false
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val noteId = intent.getStringExtra("noteId")
        if (noteId == "" || noteId == null)
            throw RuntimeException("noteId is empty (intent)")

        val lines = mutableStateOf(listOf(Line("", "", "", Date())))
        var db: AppDatabase? = null

        val isLive = mutableStateOf(false)
        ServiceBindings.mServiceAvailable.observe(this) {
            isLive.value = it.isCallbackRegistered

            db = it.dbInstance
            if (db == null) {
                db = Room.databaseBuilder(
                    applicationContext,
                    AppDatabase::class.java, noteId
                ).build()
                it.dbInstance = db
            }

            val repository = LineRepository(db!!.listItemDao())
            val viewModelFactory = LineViewModelFactory(repository)
            viewModel = ViewModelProvider(this, viewModelFactory)[LineViewModel::class.java]
            viewModel.allLines.observe(this) {
                lines.value = it
            }
        }

        setContent {
            VoiceNotesTheme {
                Surface(
                    modifier = Modifier
                        .fillMaxSize()
                        .imePadding(),
                    color = MaterialTheme.colorScheme.background
                ) {
                    Column {
                        Controls(
                            isLive = isLive.value,
                            onBackClick = { finish() },
                            onLiveClick = {
                                isLive.value = it; setLive(
                                it,
                                db!!.listItemDao(),
                                lines.value
                            )
                            },
                            onClearClick = { viewModel.clear() }
                        )
                        CodeEditorWithLineNumbers(data = lines.value, readOnly = isLive.value) {
                            viewModel.update(it)
                        }
                    }
                }
            }
        }
    }

    override fun onStart() {
        super.onStart()
        Intent(this, FgService::class.java).also { intent ->
            bindService(intent, ServiceBindings.connection, Context.BIND_AUTO_CREATE)
        }
    }

    override fun onStop() {
        super.onStop()
        unbindService(ServiceBindings.connection)
        ServiceBindings.mBound = false
    }

    private fun setLive(isLive: Boolean, linesDao: LinesDao, lines: List<Line>) {
        val firstDate = if (lines.isEmpty()) {
            null
        } else {
            lines[0].date
        }

        if (isLive) {
            ServiceBindings.mService.register(RecognizerCallbacks(linesDao, firstDate))
        } else {
            ServiceBindings.mService.unregister()
        }
    }

    inner class RecognizerCallbacks(
        private val lines: LinesDao,
        private var firstEntry: Date?
    ) : RecognitionListener {
        private var line: Line? = null
        private val gson = Gson()

        override fun onPartialResult(hypothesisJson: String?) {
            if (hypothesisJson == null || hypothesisJson == "") {
                line = null
                return
            }
            val json = gson.fromJson(hypothesisJson, VoskCallbackData::class.java)
            if (json.partial == "") {
                line = null
                return
            }

            if (firstEntry == null)
                firstEntry = Date()
            if (line == null) {
                line =
                    Line(
                        UUID.randomUUID().toString(),
                        dateDiffString(Date(), firstEntry!!),
                        "",
                        Date()
                    )
                thread { lines.insert(line!!) }
            }
            line!!.text = json.partial
            thread { lines.update(line!!) }
        }

        override fun onResult(hypothesis: String?) {
//            println("onResult: $hypothesis")
        }

        override fun onFinalResult(hypothesis: String?) {
//            println("onFinalResult: $hypothesis")
        }

        override fun onError(exception: Exception?) {
            println("on exception")
            exception?.printStackTrace()
        }

        override fun onTimeout() {
            println("on timeout")
        }

        // tools

        private fun dateDiffString(date0: Date, date1: Date): String {
            val diffInMilliSeconds = kotlin.math.abs(date1.time - date0.time)
            val minutes = (diffInMilliSeconds / 1000) / 60
            val seconds = (diffInMilliSeconds / 1000) % 60
            return String.format("%02d:%02d", minutes, seconds)
        }
    }


    @Composable
    fun Controls(
        isLive: Boolean,
        onBackClick: () -> Unit,
        onLiveClick: (Boolean) -> Unit,
        onClearClick: () -> Unit
    ) {
        Row(
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier
                .fillMaxWidth()
                .padding(8.dp)
        ) {
//            IconButton(onClick = onBackClick) {
//                Icon(Icons.Default.ArrowBack, contentDescription = "Back")
//            }
            SwitchWithLabel(label = "LIVE", checked = isLive, onCheckedChange = onLiveClick)
            Button(onClick = onClearClick) {
                Text(text = "Clear")
            }
        }
    }

    @Composable
    private fun SwitchWithLabel(
        label: String,
        checked: Boolean,
        onCheckedChange: (Boolean) -> Unit
    ) {
        val interactionSource = remember { MutableInteractionSource() }
        Row(
            modifier = Modifier
                .clickable(
                    interactionSource = interactionSource,
                    indication = null,
                    role = Role.Switch,
                    onClick = { onCheckedChange(!checked) }
                )
                .padding(8.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Switch(checked = checked, onCheckedChange = { onCheckedChange(it) })
            Text(text = label)
        }
    }

    // Code editor impl.

    @Composable
    fun CodeEditorWithLineNumbers(
        data: List<Line>,
        readOnly: Boolean,
        onChange: (new: Line) -> Unit
    ) {
        LazyColumn() {
            itemsIndexed(data) { _, line ->
                CodeEditorLine(
                    line = line,
                    readOnly = readOnly,
                    onChange = { onChange(Line(line.id, line.lcText, it, line.date)) },
                )
            }
            item {
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(vertical = 8.dp), horizontalArrangement = Arrangement.Center
                ) {
                    Text(
                        text = "---- END ----",
                        fontWeight = FontWeight.Bold,
                        fontSize = 16.sp
                    )
                }
            }
        }
    }

    @Composable
    fun CodeEditorLine(
        line: Line,
        readOnly: Boolean,
        onChange: (String) -> Unit,
    ) {
        Row {
            Text(
                text = line.lcText,
                color = Color.Gray,
                fontSize = 16.sp,
//                modifier = Modifier.widthIn(min = 20.dp)
            )
            Spacer(modifier = Modifier.width(8.dp))
            BasicTextField(
                value = line.text,
                onValueChange = onChange,
                textStyle = TextStyle(fontSize = 16.sp, color = Color.LightGray),
                modifier = Modifier
                    .weight(10f),
                readOnly = readOnly,
            )
        }
    }
}

data class VoskCallbackData(val partial: String)