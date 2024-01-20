package com.example.voicenotes.data

import androidx.lifecycle.LiveData
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

class LineRepository(private val linesDao: LinesDao) {
    val allLines: LiveData<List<Line>> = linesDao.getAll()

    suspend fun insert(line: Line) = withContext(Dispatchers.IO) {
        linesDao.insert(line)
    }

    suspend fun update(line: Line) = withContext(Dispatchers.IO) {
        linesDao.update(line)
    }

    suspend fun clear() = withContext(Dispatchers.IO) {
        linesDao.clear()
    }
}