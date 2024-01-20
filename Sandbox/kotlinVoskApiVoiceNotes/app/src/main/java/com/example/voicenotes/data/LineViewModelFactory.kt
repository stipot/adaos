package com.example.voicenotes.data

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider

class LineViewModelFactory(private val repository: LineRepository) : ViewModelProvider.Factory {
    override fun <T : ViewModel> create(modelClass: Class<T>): T {
        if (modelClass.isAssignableFrom(LineViewModel::class.java)) {
            @Suppress("UNCHECKED_CAST")
            return LineViewModel(repository) as T
        }
        throw IllegalArgumentException("Unknown ViewModel class")
    }
}