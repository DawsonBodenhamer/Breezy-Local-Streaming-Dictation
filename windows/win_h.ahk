#Requires AutoHotkey v2.0
#SingleInstance Force
#Include hotkey_capture.ahk
#Include physical_context_signal.ahk

InstallKeybdHook()
InstallMouseHook()

; Suppress Windows Voice Typing and relay one non-repeating toggle through the
; dictation client's synthetic-only Ctrl+Alt+Shift+F24 hotkey.
Runtime := EnvGet("LOCALAPPDATA") "\breezy_dictation"
SupervisorPath := Runtime "\supervisor.ps1"
HotkeyApplyPath := Runtime "\hotkey_apply.ps1"
HotkeyReadyPath := Runtime "\hotkey.ready"
ClientReadyPath := Runtime "\client.ready"
ClientFailedPath := Runtime "\client_failed.flag"
HotkeyPendingPath := Runtime "\hotkey_change.pending"
HotkeyResultPath := Runtime "\hotkey_change.result.json"
ManagerPythonw := Runtime "\.venv\Scripts\pythonw.exe"
ManagerScript := Runtime "\text_conversion_manager.py"
ConfigPath := Runtime "\config.toml"
AutomaticPunctuationEnabled := ReadAutomaticPunctuation(ConfigPath)
CapitalizeNewParagraphsEnabled := ReadFormattingSetting(ConfigPath, "capitalize_new_paragraphs", true)
CapitalizeNewLinesEnabled := ReadFormattingSetting(ConfigPath, "capitalize_new_lines", true)
RecordingStatePath := Runtime "\recording.active"
ManualLineBreakSignalPath := Runtime "\manual_line_break.signal"
PhysicalContextResetSignalPath := Runtime "\physical_context_reset.signal"
PhysicalContextGenerationPath := Runtime "\physical_context.generation"
PhysicalContextSequence := 0
LastPhysicalIdle := A_TimeIdlePhysical
IdleIconPath := Runtime "\assets\dictation_idle.ico"
RecordingIconPath := Runtime "\assets\dictation_recording.ico"
LastRecordingState := -1
MicrophoneMenu := Menu()
CapitalizationMenu := Menu()
CaptureSession := 0
UserHotkey := EnvGet("BREEZY_DICTATION_HOTKEY")
if (UserHotkey = "")
    UserHotkey := EnvGet("BREEZY_LOCAL_STREAMING_DICTATION_HOTKEY")
if (UserHotkey = "")
    UserHotkey := "#h"
HotkeyLabel := FormatHotkeyLabel(UserHotkey)

A_TrayMenu.Delete()
A_TrayMenu.Add("Toggle dictation`t" HotkeyLabel, ToggleDictation)
A_TrayMenu.Add()
A_TrayMenu.Add("Microphone", MicrophoneMenu)
A_TrayMenu.Add("Refresh microphone list", RefreshMicrophones)
A_TrayMenu.Add("Change activation hotkey…", ChangeActivationHotkey)
A_TrayMenu.Add("Automatic punctuation", ToggleAutomaticPunctuation)
if AutomaticPunctuationEnabled
    A_TrayMenu.Check("Automatic punctuation")
CapitalizationMenu.Add("Capitalize new paragraphs", ToggleCapitalizeNewParagraphs)
CapitalizationMenu.Add("Capitalize new lines", ToggleCapitalizeNewLines)
if CapitalizeNewParagraphsEnabled
    CapitalizationMenu.Check("Capitalize new paragraphs")
if CapitalizeNewLinesEnabled
    CapitalizationMenu.Check("Capitalize new lines")
A_TrayMenu.Add("Capitalization", CapitalizationMenu)
A_TrayMenu.Add("Dictation corrections…", ManageTextConversions)
A_TrayMenu.Add()
A_TrayMenu.Add("Disable dictation & AutoHotkey (until restart)", DisableDictation)
A_TrayMenu.Add("Enable dictation", EnableDictation)
A_TrayMenu.Add("Restart dictation", RestartDictation)
A_TrayMenu.Add("Open logs", OpenLogs)
A_IconTip := "Breezy Dictation"

RefreshMicrophones()
UpdateRecordingIcon()
SetTimer(UpdateRecordingIcon, 150)

Hotkey UserHotkey, HandleDictationHotkey
~*Enter::RecordManualLineBreak()
PublishHotkeyReady()
if FileExist(HotkeyPendingPath)
    SetTimer(CheckHotkeyChangeResult, 250)
SetTimer(DetectPhysicalContextReset, 15)

DetectPhysicalContextReset(*) {
    global LastPhysicalIdle
    currentIdle := A_TimeIdlePhysical
    if currentIdle < LastPhysicalIdle || (currentIdle = 0 && LastPhysicalIdle != 0)
        RecordPhysicalContextReset()
    LastPhysicalIdle := currentIdle
}

RecordPhysicalContextReset() {
    global PhysicalContextResetSignalPath, PhysicalContextGenerationPath
    global PhysicalContextSequence
    nextSequence := PhysicalContextSequence + 1
    if WritePhysicalContextResetSignal(
        PhysicalContextGenerationPath,
        PhysicalContextResetSignalPath,
        nextSequence
    )
        PhysicalContextSequence := nextSequence
}

HandleDictationHotkey(*) {
    global UserHotkey
    if CapturedHotkey.IsWinH(UserHotkey) {
        KeyWait "h"
        KeyWait "LWin"
        KeyWait "RWin"
    }
    ToggleDictation()
}

ToggleDictation(*) {
    global ClientReadyPath, ClientFailedPath
    if FileExist(ClientFailedPath) {
        TrayTip("Dictation client failed to start. Open logs or restart dictation.", "Breezy Dictation")
        return
    }
    if !FileExist(ClientReadyPath) {
        TrayTip("Dictation is still loading.", "Breezy Dictation")
        return
    }
    SendEvent "^!+{F24}"
}

RecordManualLineBreak(*) {
    global RecordingStatePath, ManualLineBreakSignalPath
    if !FileExist(RecordingStatePath) {
        return
    }
    if GetKeyState("Ctrl", "P") || GetKeyState("Alt", "P")
        || GetKeyState("LWin", "P") || GetKeyState("RWin", "P")
        return

    foregroundWindow := WinExist("A")
    if !foregroundWindow
        return

    breakCount := 0
    if FileExist(ManualLineBreakSignalPath) {
        try existingSignal := Trim(FileRead(ManualLineBreakSignalPath, "UTF-8"))
        catch
            existingSignal := ""
        if RegExMatch(existingSignal, "^(\d+):([12])$", &existing)
            && Integer(existing[1]) = foregroundWindow
            breakCount := Integer(existing[2])
    }
    breakCount := Min(breakCount + 1, 2)
    temporaryPath := ManualLineBreakSignalPath ".tmp." DllCall("GetCurrentProcessId") "." A_TickCount
    try {
        FileAppend(foregroundWindow ":" breakCount "`n", temporaryPath, "UTF-8-RAW")
        FileMove(temporaryPath, ManualLineBreakSignalPath, 1)
    } catch {
        try FileDelete(temporaryPath)
    }
    KeyWait "Enter"
}

RefreshMicrophones(*) {
    global MicrophoneMenu, SupervisorPath, ConfigPath

    MicrophoneMenu.Delete()
    currentDevice := ReadCurrentDeviceIndex(ConfigPath)
    seenNames := Map()
    foundDevices := 0

    try {
        shell := ComObject("WScript.Shell")
        command := "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"" SupervisorPath "`" tray-devices"
        process := shell.Exec(command)
        output := process.StdOut.ReadAll()

        for line in StrSplit(output, "`n", "`r") {
            if !RegExMatch(line, "^\s*(\d+)\t(.+?)\s*$", &device)
                continue

            deviceIndex := Integer(device[1])
            deviceName := Trim(device[2])
            nameKey := StrLower(deviceName)
            if seenNames.Has(nameKey)
                continue
            seenNames[nameKey] := true
            foundDevices += 1

            safeName := StrReplace(deviceName, "&", "&&")
            label := deviceIndex " — " safeName
            MicrophoneMenu.Add(label, SelectMicrophone.Bind(deviceIndex, deviceName))
            if deviceIndex = currentDevice
                MicrophoneMenu.Check(label)
        }
    } catch {
        MicrophoneMenu.Add("Unable to list microphones")
        MicrophoneMenu.Disable("Unable to list microphones")
    }

    if foundDevices = 0 {
        MicrophoneMenu.Add("No microphones found")
        MicrophoneMenu.Disable("No microphones found")
    }
}

ManageTextConversions(*) {
    global ManagerPythonw, ManagerScript, Runtime
    Run('"' ManagerPythonw '" "' ManagerScript '"', Runtime, "Hide")
}

ChangeActivationHotkey(*) {
    global UserHotkey, CaptureSession
    if IsObject(CaptureSession) {
        CaptureSession.Window.Show()
        return
    }

    currentLabel := FormatHotkeyLabel(UserHotkey)
    captureWindow := Gui("+AlwaysOnTop -MinimizeBox -MaximizeBox", "Change activation hotkey")
    captureWindow.MarginX := 22
    captureWindow.MarginY := 18
    captureWindow.SetFont("s12 w600", "Segoe UI")
    captureWindow.AddText("w390", "Waiting for a keyboard shortcut…")
    captureWindow.SetFont("s10 w400", "Segoe UI")
    status := captureWindow.AddText("y+12 w390 h42", "Hold Ctrl, Shift, Alt, or Win, then press another key.")
    captureWindow.SetFont("s9", "Segoe UI")
    captureWindow.AddText("y+8 w390 c555555", "Current shortcut: " currentLabel "`nPress Escape or choose Cancel to keep it.")
    cancelButton := captureWindow.AddButton("y+18 w92 h30", "Cancel")

    input := InputHook("L0")
    input.KeyOpt("{All}", "NS")
    input.OnKeyDown := CaptureKeyDown
    input.OnKeyUp := CaptureKeyUp
    CaptureSession := {
        Window: captureWindow,
        Status: status,
        Hook: input,
        Captured: 0,
        SuffixVK: 0,
        SuffixSC: 0,
        Cancelling: false
    }

    cancelButton.OnEvent("Click", CancelHotkeyCapture)
    captureWindow.OnEvent("Close", CancelHotkeyCapture)
    Hotkey UserHotkey, "Off"
    input.Start()
    captureWindow.Show("AutoSize Center")
}

CaptureKeyDown(input, vk, sc) {
    global CaptureSession
    if !IsObject(CaptureSession)
        return
    if vk = 0x1B {
        CaptureSession.Cancelling := true
        CaptureSession.SuffixVK := vk
        CaptureSession.SuffixSC := sc
        CaptureSession.Status.Text := "Cancelling… release Escape."
        return
    }
    if CapturedHotkey.IsModifierVK(vk) {
        CaptureSession.Status.Text := HeldModifierStatus()
        return
    }
    if IsObject(CaptureSession.Captured)
        return

    try {
        captured := CapturedHotkey.FromEvent(
            vk,
            sc,
            GetKeyState("Ctrl", "P"),
            GetKeyState("Alt", "P"),
            GetKeyState("Shift", "P"),
            GetKeyState("LWin", "P") || GetKeyState("RWin", "P")
        )
        if captured.Binding != UserHotkey {
            try {
                Hotkey captured.Binding, ProbeHotkey, "On"
                Hotkey captured.Binding, "Off"
            } catch as registrationProblem {
                CaptureSession.Status.Text := "That shortcut is unavailable. Try another combination."
                return
            }
        }
        CaptureSession.Captured := captured
        CaptureSession.SuffixVK := vk
        CaptureSession.SuffixSC := sc
        CaptureSession.Status.Text := "Recorded " captured.Label ". Release the keys to apply it."
    } catch as problem {
        CaptureSession.Status.Text := problem.Message
    }
}

CaptureKeyUp(input, vk, sc) {
    global CaptureSession
    if !IsObject(CaptureSession)
        return
    if CaptureSession.Cancelling {
        if CapturedChordReleased()
            CancelHotkeyCapture()
        return
    }
    if !IsObject(CaptureSession.Captured) {
        if CapturedHotkey.IsModifierVK(vk)
            CaptureSession.Status.Text := "Hold Ctrl, Shift, Alt, or Win, then press another key."
        return
    }
    if CapturedChordReleased()
        ApplyCapturedHotkey()
}

HeldModifierStatus() {
    labels := CapturedHotkey.ModifierLabel(
        GetKeyState("Ctrl", "P"),
        GetKeyState("Alt", "P"),
        GetKeyState("Shift", "P"),
        GetKeyState("LWin", "P") || GetKeyState("RWin", "P")
    )
    return labels = ""
        ? "Hold Ctrl, Shift, Alt, or Win, then press another key."
        : labels " held — now press another key."
}

CapturedChordReleased() {
    global CaptureSession
    if GetKeyState("Ctrl", "P") || GetKeyState("Alt", "P") || GetKeyState("Shift", "P")
        || GetKeyState("LWin", "P") || GetKeyState("RWin", "P")
        return false
    if CaptureSession.SuffixVK {
        suffix := CaptureSession.SuffixSC
            ? Format("sc{:03X}", CaptureSession.SuffixSC)
            : Format("vk{:02X}", CaptureSession.SuffixVK)
        if GetKeyState(suffix, "P")
            return false
    }
    return true
}

ApplyCapturedHotkey() {
    global CaptureSession, UserHotkey, HotkeyApplyPath, Runtime
    captured := CaptureSession.Captured
    CaptureSession.Hook.Stop()
    CaptureSession.Status.Text := "Applying " captured.Label "… Dictation will restart."
    Hotkey UserHotkey, "On"
    try {
        command := "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"" HotkeyApplyPath "`" -HotkeyBinding `"" captured.Binding "`""
        Run(command, Runtime, "Hide")
        SetTimer(CheckHotkeyChangeResult, 250)
    } catch as problem {
        CaptureSession.Status.Text := "The shortcut change could not start. " FormatHotkeyLabel(UserHotkey) " is still active."
        return
    }
}

CancelHotkeyCapture(*) {
    global CaptureSession, UserHotkey
    if !IsObject(CaptureSession)
        return
    if GetKeyState("Ctrl", "P") || GetKeyState("Alt", "P") || GetKeyState("Shift", "P")
        || GetKeyState("LWin", "P") || GetKeyState("RWin", "P") {
        CaptureSession.Cancelling := true
        CaptureSession.SuffixVK := 0
        CaptureSession.SuffixSC := 0
        CaptureSession.Status.Text := "Release the held keys to cancel."
        return
    }
    try CaptureSession.Hook.Stop()
    try CaptureSession.Window.Destroy()
    try Hotkey UserHotkey, "On"
    CaptureSession := 0
}

ProbeHotkey(*) {
}

PublishHotkeyReady() {
    global HotkeyReadyPath, UserHotkey
    try FileDelete(HotkeyReadyPath)
    FileAppend(UserHotkey, HotkeyReadyPath, "UTF-8-RAW")
}

CheckHotkeyChangeResult() {
    global HotkeyResultPath, HotkeyPendingPath, UserHotkey, CaptureSession
    if !FileExist(HotkeyResultPath)
        return
    try result := FileRead(HotkeyResultPath, "UTF-8")
    catch
        return
    SetTimer(CheckHotkeyChangeResult, 0)
    if IsObject(CaptureSession)
        CancelHotkeyCapture()
    try FileDelete(HotkeyResultPath)
    try FileDelete(HotkeyPendingPath)
    if InStr(result, '"status":"success"') {
        TrayTip("Activation shortcut changed to " FormatHotkeyLabel(UserHotkey) ".", "Breezy Dictation")
    } else if InStr(result, '"status":"rolled_back"') {
        MsgBox("The shortcut was not changed. " FormatHotkeyLabel(UserHotkey) " is still active.", "Shortcut restored", "Icon!")
    } else {
        MsgBox("The previous shortcut settings were restored, but dictation did not become healthy. Open the logs or restart dictation.", "Shortcut recovery needed", "Iconx")
    }
}

FormatHotkeyLabel(binding) {
    return CapturedHotkey.LabelFromBinding(binding)
}

ReadCurrentDeviceIndex(configPath) {
    try {
        configText := FileRead(configPath, "UTF-8")
        if RegExMatch(configText, "ms)^\[audio\].*?^device\s*=\s*(\d+)", &device)
            return Integer(device[1])
    }
    return -1
}

ReadAutomaticPunctuation(configPath) {
    if !FileExist(configPath)
        return false
    return ReadFormattingSetting(configPath, "automatic_punctuation", true)
}

ReadFormattingSetting(configPath, key, missingDefault) {
    if !FileExist(configPath)
        return missingDefault
    try {
        configText := FileRead(configPath, "UTF-8")
        if RegExMatch(configText, "ims)^\[formatting\].*?^" key "\s*=\s*(true|false)", &setting)
            return StrLower(setting[1]) = "true"
        return missingDefault
    }
    return missingDefault
}

SetFormattingValue(key, nextValue) {
    global SupervisorPath, Runtime
    enabledText := nextValue ? "true" : "false"
    command := "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"" SupervisorPath "`" set-formatting -FormattingKey " key " -Enabled " enabledText
    return RunWait(command, Runtime, "Hide") = 0
}

ToggleAutomaticPunctuation(*) {
    global AutomaticPunctuationEnabled
    nextValue := !AutomaticPunctuationEnabled
    if !SetFormattingValue("automatic_punctuation", nextValue) {
        TrayTip("Automatic punctuation could not be changed.", "Breezy Dictation")
        return
    }
    AutomaticPunctuationEnabled := nextValue
    if nextValue
        A_TrayMenu.Check("Automatic punctuation")
    else
        A_TrayMenu.Uncheck("Automatic punctuation")
    TrayTip("Automatic punctuation " (nextValue ? "enabled" : "disabled") ". Applies the next time you start recording.", "Breezy Dictation")
}

ToggleCapitalizeNewParagraphs(*) {
    global CapitalizeNewParagraphsEnabled, CapitalizationMenu
    nextValue := !CapitalizeNewParagraphsEnabled
    if !SetFormattingValue("capitalize_new_paragraphs", nextValue) {
        TrayTip("Paragraph capitalization could not be changed.", "Breezy Dictation")
        return
    }
    CapitalizeNewParagraphsEnabled := nextValue
    if nextValue
        CapitalizationMenu.Check("Capitalize new paragraphs")
    else
        CapitalizationMenu.Uncheck("Capitalize new paragraphs")
    TrayTip(
        nextValue
            ? "New paragraphs and empty documents will begin with a capital letter. Applies to the next utterance."
            : "New paragraphs and empty documents will keep the recognized capitalization. Applies to the next utterance.",
        "Breezy Dictation"
    )
}

ToggleCapitalizeNewLines(*) {
    global CapitalizeNewLinesEnabled, CapitalizationMenu
    nextValue := !CapitalizeNewLinesEnabled
    if !SetFormattingValue("capitalize_new_lines", nextValue) {
        TrayTip("Line capitalization could not be changed.", "Breezy Dictation")
        return
    }
    CapitalizeNewLinesEnabled := nextValue
    if nextValue
        CapitalizationMenu.Check("Capitalize new lines")
    else
        CapitalizationMenu.Uncheck("Capitalize new lines")
    TrayTip(
        nextValue
            ? "Text after one line break will begin with a capital letter. Applies to the next utterance."
            : "Text after one line break will keep the recognized capitalization. Applies to the next utterance.",
        "Breezy Dictation"
    )
}

SelectMicrophone(deviceIndex, deviceName, *) {
    global SupervisorPath, Runtime
    TrayTip("Switching to " deviceName ". Dictation will restart.", "Breezy Dictation")
    command := "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"" SupervisorPath "`" switch-microphone -Device " deviceIndex
    Run(command, Runtime, "Hide")
}

DisableDictation(*) {
    global SupervisorPath, Runtime
    TrayTip("Stopping dictation and releasing AutoHotkey. Will return on next restart.", "Breezy Dictation")
    command := "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"" SupervisorPath "`" stop"
    Run(command, Runtime, "Hide")
}

EnableDictation(*) {
    global SupervisorPath, Runtime
    TrayTip("Starting dictation.", "Breezy Dictation")
    command := "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"" SupervisorPath "`" resume-client"
    Run(command, Runtime, "Hide")
}

RestartDictation(*) {
    global SupervisorPath, Runtime
    TrayTip("Restarting dictation.", "Breezy Dictation")
    command := "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"" SupervisorPath "`" restart"
    Run(command, Runtime, "Hide")
}

OpenLogs(*) {
    global Runtime
    Run(Runtime "\logs")
}

UpdateRecordingIcon(*) {
    global RecordingStatePath, IdleIconPath, RecordingIconPath
    global LastRecordingState
    recording := FileExist(RecordingStatePath) ? 1 : 0
    if recording = LastRecordingState
        return
    iconPath := recording ? RecordingIconPath : IdleIconPath
    try TraySetIcon(iconPath)
    LastRecordingState := recording
}
