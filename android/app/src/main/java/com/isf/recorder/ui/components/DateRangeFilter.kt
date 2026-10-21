package com.isf.recorder.ui.components

import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.R
import com.isf.recorder.ui.theme.*
import java.time.DayOfWeek
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.util.Locale

/** Selected date filter; [from]/[to] are inclusive IST days, both null = all time. */
data class DateFilter(val key: String, val from: LocalDate?, val to: LocalDate?, val label: String) {
    val fromParam: String? get() = from?.isoDay()
    val toParam: String? get() = to?.isoDay()

    companion object {
        val ALL = DateFilter("all", null, null, "All dates")

        fun preset(key: String, today: LocalDate = LocalDate.now(IST)): DateFilter = when (key) {
            "today" -> DateFilter(key, today, today, "Today")
            "yesterday" -> today.minusDays(1).let { DateFilter(key, it, it, "Yesterday") }
            "week" -> DateFilter(key, today.with(DayOfWeek.MONDAY), today, "This week")
            "month" -> DateFilter(key, today.withDayOfMonth(1), today, "This month")
            else -> ALL
        }

        fun custom(from: LocalDate, to: LocalDate): DateFilter {
            val f = DateTimeFormatter.ofPattern("d MMM", Locale.ENGLISH)
            val label = if (from == to) from.format(f) else "${from.format(f)} – ${to.format(f)}"
            return DateFilter("custom", from, to, label)
        }
    }
}

/** Chips: All · Today · Yesterday · This week · This month · Custom range (Material3 DateRangePicker). */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DateRangeFilter(
    value: DateFilter,
    onChange: (DateFilter) -> Unit,
    modifier: Modifier = Modifier
) {
    var showPicker by remember { mutableStateOf(false) }
    Row(
        modifier = modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
        horizontalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        listOf("all" to "All", "today" to "Today", "yesterday" to "Yesterday", "week" to "This week", "month" to "This month")
            .forEach { (key, label) ->
                FilterChip(
                    selected = value.key == key,
                    onClick = { onChange(DateFilter.preset(key)) },
                    label = { Text(label, fontSize = 13.sp) },
                    colors = FilterChipDefaults.filterChipColors(selectedContainerColor = JomiMint, selectedLabelColor = JomiTeal)
                )
            }
        FilterChip(
            selected = value.key == "custom",
            onClick = { showPicker = true },
            leadingIcon = { Icon(painterResource(R.drawable.ic_calendar), null, modifier = Modifier.size(16.dp)) },
            label = { Text(if (value.key == "custom") value.label else "Custom range", fontSize = 13.sp) },
            colors = FilterChipDefaults.filterChipColors(selectedContainerColor = JomiMint, selectedLabelColor = JomiTeal)
        )
    }

    if (showPicker) {
        val todayUtcMillis = LocalDate.now(IST).atStartOfDay(ZoneOffset.UTC).toInstant().toEpochMilli()
        val state = rememberDateRangePickerState(
            initialSelectedStartDateMillis = value.from?.atStartOfDay(ZoneOffset.UTC)?.toInstant()?.toEpochMilli(),
            initialSelectedEndDateMillis = value.to?.atStartOfDay(ZoneOffset.UTC)?.toInstant()?.toEpochMilli(),
            selectableDates = object : SelectableDates {
                override fun isSelectableDate(utcTimeMillis: Long) = utcTimeMillis <= todayUtcMillis
            }
        )
        DatePickerDialog(
            onDismissRequest = { showPicker = false },
            confirmButton = {
                TextButton(
                    enabled = state.selectedStartDateMillis != null,
                    onClick = {
                        val start = state.selectedStartDateMillis
                        if (start != null) {
                            val from = Instant.ofEpochMilli(start).atZone(ZoneOffset.UTC).toLocalDate()
                            val to = state.selectedEndDateMillis?.let { Instant.ofEpochMilli(it).atZone(ZoneOffset.UTC).toLocalDate() } ?: from
                            onChange(DateFilter.custom(from, to))
                        }
                        showPicker = false
                    }
                ) { Text("Apply") }
            },
            dismissButton = { TextButton(onClick = { showPicker = false }) { Text("Cancel") } }
        ) {
            DateRangePicker(
                state = state,
                title = { Text("Choose dates", modifier = Modifier.padding(start = 24.dp, top = 16.dp)) },
                showModeToggle = false,
                modifier = Modifier.weight(1f)
            )
        }
    }
}
