package com.isf.recorder.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.data.prefs.UserPreferences
import com.isf.recorder.ui.theme.*
import kotlinx.coroutines.launch

@Composable
fun LoginScreen(onLoginSuccess: () -> Unit) {
    val context = LocalContext.current
    val colors = MaterialTheme.colorScheme
    val ext = LocalExtendedColors.current
    val prefs = remember { UserPreferences(context) }
    val apiClient = remember { ISFApiClient(context) }
    val scope = rememberCoroutineScope()

    var phone by remember { mutableStateOf("") }
    var otpCode by remember { mutableStateOf("") }
    var step by remember { mutableIntStateOf(1) } // 1: enter phone, 2: enter otp
    var isLoading by remember { mutableStateOf(false) }
    var errorMessage by remember { mutableStateOf<String?>(null) }

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(colors.background)
            .padding(24.dp),
        contentAlignment = Alignment.Center
    ) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            modifier = Modifier
                .fillMaxWidth()
                .verticalScroll(rememberScrollState())
                .imePadding()
        ) {
            // App Badge
            Surface(
                color = colors.surfaceVariant,
                shape = RoundedCornerShape(16.dp),
                modifier = Modifier.size(64.dp)
            ) {
                Box(contentAlignment = Alignment.Center) {
                    Text("🎙️", fontSize = 32.sp)
                }
            }

            Spacer(modifier = Modifier.height(16.dp))

            Text(
                text = "ISF Recorder",
                color = colors.onBackground,
                fontSize = 28.sp,
                fontWeight = FontWeight.Bold
            )

            Text(
                text = "Enterprise Audio Intelligence & Transcription",
                color = colors.onSurfaceVariant,
                fontSize = 14.sp,
                textAlign = TextAlign.Center,
                modifier = Modifier.padding(top = 4.dp, bottom = 32.dp)
            )

            if (step == 1) {
                // Phone Number Input
                // Only a real 10-digit Indian mobile can get an OTP: +91 is fixed, the box keeps digits only
                // (a pasted "+91 98765 43210" or "098765..." is trimmed to the 10 digits).
                val phoneValid = phone.length == 10 && phone.first() in '6'..'9'
                OutlinedTextField(
                    value = phone,
                    onValueChange = { raw ->
                        var d = raw.filter { it.isDigit() }
                        if (d.length > 10 && d.startsWith("91")) d = d.substring(2)
                        if (d.length > 10 && d.startsWith("0")) d = d.substring(1)
                        phone = d.take(10)
                        errorMessage = null
                    },
                    label = { Text("WhatsApp mobile number", color = colors.onSurfaceVariant) },
                    placeholder = { Text("98765 43210", color = ext.textMuted) },
                    prefix = { Text("+91 ", color = colors.onBackground, fontWeight = FontWeight.SemiBold) },
                    supportingText = {
                        when {
                            phone.isEmpty() -> Text("Enter your 10-digit mobile number", color = ext.textMuted)
                            phone.first() !in '6'..'9' -> Text("A mobile number starts with 6, 7, 8 or 9", color = MaterialTheme.colorScheme.error)
                            phone.length < 10 -> Text("${10 - phone.length} more digit${if (10 - phone.length == 1) "" else "s"} needed", color = ext.textMuted)
                            else -> Text("✓ Looks good", color = EmeraldPrimary)
                        }
                    },
                    isError = phone.isNotEmpty() && phone.first() !in '6'..'9',
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Phone),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedTextColor = colors.onBackground,
                        unfocusedTextColor = colors.onBackground,
                        focusedBorderColor = EmeraldPrimary,
                        unfocusedBorderColor = colors.outline,
                        focusedLabelColor = EmeraldPrimary
                    ),
                    shape = RoundedCornerShape(12.dp),
                    modifier = Modifier.fillMaxWidth()
                )

                Spacer(modifier = Modifier.height(16.dp))

                Button(
                    onClick = {
                        if (!phoneValid) {
                            errorMessage = "Please enter a valid 10-digit mobile number"
                            return@Button
                        }
                        val cleanPhone = "91$phone"
                        isLoading = true
                        errorMessage = null
                        scope.launch {
                            val res = apiClient.requestOtp(cleanPhone)
                            isLoading = false
                            if (res.isSuccess) {
                                step = 2
                            } else {
                                errorMessage = res.exceptionOrNull()?.message ?: "Failed to send OTP"
                            }
                        }
                    },
                    enabled = !isLoading && phoneValid,
                    colors = ButtonDefaults.buttonColors(containerColor = EmeraldPrimary),
                    shape = RoundedCornerShape(12.dp),
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(52.dp)
                ) {
                    if (isLoading) {
                        CircularProgressIndicator(color = ext.bgDark, modifier = Modifier.size(24.dp))
                    } else {
                        Text("Send WhatsApp OTP", color = ext.bgDark, fontWeight = FontWeight.Bold, fontSize = 16.sp)
                    }
                }
            } else {
                // OTP Verification Step
                Text(
                    text = "Enter 6-digit OTP sent to +91 $phone on WhatsApp",
                    color = colors.onSurfaceVariant,
                    fontSize = 14.sp,
                    textAlign = TextAlign.Center
                )

                Spacer(modifier = Modifier.height(16.dp))

                OutlinedTextField(
                    value = otpCode,
                    onValueChange = { if (it.length <= 6) otpCode = it },
                    label = { Text("6-Digit OTP Code", color = colors.onSurfaceVariant) },
                    placeholder = { Text("123456", color = ext.textMuted) },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedTextColor = colors.onBackground,
                        unfocusedTextColor = colors.onBackground,
                        focusedBorderColor = EmeraldPrimary,
                        unfocusedBorderColor = colors.outline
                    ),
                    shape = RoundedCornerShape(12.dp),
                    modifier = Modifier.fillMaxWidth()
                )

                Spacer(modifier = Modifier.height(16.dp))

                Button(
                    onClick = {
                        if (otpCode.length != 6) {
                            errorMessage = "OTP must be 6 digits"
                            return@Button
                        }
                        isLoading = true
                        errorMessage = null
                        scope.launch {
                            val cleanPhone = "91$phone"
                            val res = apiClient.verifyOtp(cleanPhone, otpCode.trim())
                            isLoading = false
                            if (res.isSuccess) {
                                onLoginSuccess()
                            } else {
                                errorMessage = res.exceptionOrNull()?.message ?: "Verification failed"
                            }
                        }
                    },
                    enabled = !isLoading && otpCode.length == 6,
                    colors = ButtonDefaults.buttonColors(containerColor = EmeraldPrimary),
                    shape = RoundedCornerShape(12.dp),
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(52.dp)
                ) {
                    if (isLoading) {
                        CircularProgressIndicator(color = ext.bgDark, modifier = Modifier.size(24.dp))
                    } else {
                        Text("Verify & Sign In", color = ext.bgDark, fontWeight = FontWeight.Bold, fontSize = 16.sp)
                    }
                }

                TextButton(onClick = { step = 1 }) {
                    Text("Change Phone Number", color = colors.onSurfaceVariant)
                }
            }

            if (errorMessage != null) {
                Spacer(modifier = Modifier.height(12.dp))
                Text(
                    text = errorMessage ?: "",
                    color = RecordRed,
                    fontSize = 13.sp,
                    textAlign = TextAlign.Center
                )
            }

            Spacer(modifier = Modifier.height(32.dp))

        }
    }
}
