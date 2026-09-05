#Requires AutoHotkey v2.0

ReadPhysicalContextGeneration(generationPath) {
    if !FileExist(generationPath)
        return ""
    try generation := Trim(FileRead(generationPath, "UTF-8"), " `t`r`n")
    catch
        return ""
    if generation = "" || InStr(generation, "|")
        return ""
    return generation
}

WritePhysicalContextResetSignal(generationPath, signalPath, sequence) {
    generation := ReadPhysicalContextGeneration(generationPath)
    if generation = "" || sequence <= 0
        return false
    temporaryPath := signalPath ".tmp." DllCall("GetCurrentProcessId") "." A_TickCount
    try {
        FileAppend(generation "|" sequence "`n", temporaryPath, "UTF-8-RAW")
        FileMove(temporaryPath, signalPath, 1)
        return true
    } catch {
        try FileDelete(temporaryPath)
        return false
    }
}

WriteCaretDisplacementSignal(generationPath, signalPath, sequence, cachedGen := "") {
    generation := cachedGen != "" ? cachedGen : ReadPhysicalContextGeneration(generationPath)
    if generation = "" || sequence <= 0
        return false
    temporaryPath := signalPath ".tmp." DllCall("GetCurrentProcessId") "." A_TickCount
    try {
        FileAppend(generation "|" sequence "`n", temporaryPath, "UTF-8-RAW")
    } catch {
        try FileDelete(temporaryPath)
        return false
    }
    loop 3 {
        try {
            FileMove(temporaryPath, signalPath, 1)
            return true
        } catch {
            Sleep(1)
        }
    }
    try FileDelete(temporaryPath)
    return false
}
