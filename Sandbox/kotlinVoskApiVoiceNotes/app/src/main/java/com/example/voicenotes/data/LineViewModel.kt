package com.example.voicenotes.data

import androidx.lifecycle.LiveData
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.launch

class LineViewModel(private val repository: LineRepository) : ViewModel() {

    val allLines: LiveData<List<Line>> = repository.allLines

    fun insert(line: Line) = viewModelScope.launch {
        repository.insert(line)
    }

    fun update(line: Line) = viewModelScope.launch {
        repository.update(line)
    }

    fun clear() = viewModelScope.launch {
        repository.clear()
    }
}