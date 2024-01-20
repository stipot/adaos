package com.example.voicenotes.data

import androidx.lifecycle.LiveData
import androidx.room.ColumnInfo
import androidx.room.Dao
import androidx.room.Delete
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Update
import java.util.Date

@Entity(tableName = "lines")
data class Line(
    @PrimaryKey val id: String,
    @ColumnInfo(name = "lcText") val lcText: String,
    @ColumnInfo(name = "text") var text: String,
    @ColumnInfo(name = "date") val date: Date
)

@Dao
interface LinesDao {
    @Insert
    fun insert(v: Line)

    @Query("SELECT * FROM lines")
    fun getAll(): LiveData<List<Line>>

    @Update
    fun update(v: Line)

    @Delete
    fun delete(v: Line)

    @Query("DELETE FROM lines")
    fun clear()
}
