package com.isf.recorder.ui.components

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneOffset

/**
 * Date dropdown (round 2): All dates / Today / Yesterday / Custom range… (Material3 DateRangePicker).
 * Same field look as the other dropdowns.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DateDropdown(
    value: DateFilter,
    onChange: (DateFilter) -> Unit,
    modifier: Modifier = Modifier,
    label: String = "Date"
) {
    var showPicker by remember { mutableStateOf(false) }
    val presets = listOf(
        NameOption("All dates", "all"),
        NameOption("Today", "today"),
        NameOption("Yesterday", "yesterday"),
        NameOption("Custom range…", "custom")
    )
    SelectDropdown(
        label = label,
        options = presets,
        selected = NameOption(value.label, value.key),
        onSelect = { opt ->
            when (opt.userId) {
                "custom" -> showPicker = true
                else -> onChange(DateFilter.preset(opt.userId ?: "all"))
            }
        },
        modifier = modifier
    )

    if (showPicker) DateRangeDialog(value, onDismiss = { showPicker = false }) { onChange(it); showPicker = false }
}

/** Material3 date-range picker (only past days); used by DateDropdown and the compact Date filter button. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DateRangeDialog(value: DateFilter, onDismiss: () -> Unit, onApply: (DateFilter) -> Unit) {
        val todayUtc = LocalDate.now(IST).atStartOfDay(ZoneOffset.UTC).toInstant().toEpochMilli()
        val state = rememberDateRangePickerState(
            initialSelectedStartDateMillis = value.from?.atStartOfDay(ZoneOffset.UTC)?.toInstant()?.toEpochMilli(),
            initialSelectedEndDateMillis = value.to?.atStartOfDay(ZoneOffset.UTC)?.toInstant()?.toEpochMilli(),
            selectableDates = object : SelectableDates {
                override fun isSelectableDate(utcTimeMillis: Long) = utcTimeMillis <= todayUtc
            }
        )
        DatePickerDialog(
            onDismissRequest = onDismiss,
            confirmButton = {
                TextButton(
                    enabled = state.selectedStartDateMillis != null,
                    onClick = {
                        state.selectedStartDateMillis?.let { start ->
                            val from = Instant.ofEpochMilli(start).atZone(ZoneOffset.UTC).toLocalDate()
                            val to = state.selectedEndDateMillis?.let { Instant.ofEpochMilli(it).atZone(ZoneOffset.UTC).toLocalDate() } ?: from
                            onApply(DateFilter.custom(from, to))
                        }
                        onDismiss()
                    }
                ) { Text("Apply") }
            },
            dismissButton = { TextButton(onClick = { onDismiss() }) { Text("Cancel") } }
        ) {
            DateRangePicker(
                state = state,
                title = { Text("Choose dates", modifier = Modifier.padding(start = 24.dp, top = 16.dp)) },
                showModeToggle = false,
                modifier = Modifier.weight(1f)
            )
        }
}
