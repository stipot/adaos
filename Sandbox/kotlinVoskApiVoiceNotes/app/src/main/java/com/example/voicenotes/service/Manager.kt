package com.example.voicenotes.service

import android.content.Context
import androidx.room.Room
import com.example.voicenotes.data.AppDatabase

class Manager(applicationContext: Context) {
    public val db = Room.databaseBuilder(
        applicationContext,
        AppDatabase::class.java, "note"
    ).build()
}