class CapturedHotkey {
    __New(binding, label, vk, sc) {
        this.Binding := binding
        this.Label := label
        this.VK := vk
        this.SC := sc
    }

    static FromEvent(vk, sc, ctrlHeld, altHeld, shiftHeld, winHeld) {
        if !(ctrlHeld || altHeld || shiftHeld || winHeld)
            throw Error("Hold Ctrl, Shift, Alt, or Win, then press another key.")
        if this.IsModifierVK(vk) || this.IsLockVK(vk)
            throw Error("Keep holding the modifier and press another key.")
        if vk = 0x1B
            throw Error("Escape cancels shortcut capture.")
        if vk = 0xE7
            throw Error("That keyboard event cannot be used as a shortcut.")
        if vk = 0x87
            throw Error("F24 is reserved for the internal dictation relay.")
        if winHeld && vk = 0x4C
            throw Error("Win+L is reserved by Windows.")
        if ctrlHeld && altHeld && vk = 0x2E
            throw Error("Ctrl+Alt+Delete is reserved by Windows.")

        eventName := sc ? Format("vk{:02X}sc{:03X}", vk, sc) : Format("vk{:02X}", vk)
        hotkeyName := sc ? Format("sc{:03X}", sc) : Format("vk{:02X}", vk)
        keyName := GetKeyName(eventName)
        if keyName = ""
            throw Error("That keyboard event cannot be identified.")

        prefix := this.ModifierPrefix(ctrlHeld, altHeld, shiftHeld, winHeld)
        labelPrefix := this.ModifierLabel(ctrlHeld, altHeld, shiftHeld, winHeld)
        keyLabel := StrLen(keyName) = 1 ? StrUpper(keyName) : keyName
        return CapturedHotkey(prefix hotkeyName, labelPrefix "+" keyLabel, vk, sc)
    }

    static IsModifierVK(vk) {
        return vk = 0x10 || vk = 0x11 || vk = 0x12
            || (vk >= 0xA0 && vk <= 0xA5)
            || vk = 0x5B || vk = 0x5C
    }

    static IsLockVK(vk) {
        return vk = 0x14 || vk = 0x90 || vk = 0x91
    }

    static ModifierPrefix(ctrlHeld, altHeld, shiftHeld, winHeld) {
        return (winHeld ? "#" : "")
            . (ctrlHeld ? "^" : "")
            . (altHeld ? "!" : "")
            . (shiftHeld ? "+" : "")
    }

    static ModifierLabel(ctrlHeld, altHeld, shiftHeld, winHeld) {
        labels := []
        if winHeld
            labels.Push("Win")
        if ctrlHeld
            labels.Push("Ctrl")
        if altHeld
            labels.Push("Alt")
        if shiftHeld
            labels.Push("Shift")
        return this.Join(labels, "+")
    }

    static LabelFromBinding(binding) {
        labels := []
        remaining := binding
        for pair in [["#", "Win"], ["^", "Ctrl"], ["!", "Alt"], ["+", "Shift"]] {
            if InStr(remaining, pair[1]) {
                labels.Push(pair[2])
                remaining := StrReplace(remaining, pair[1], "")
            }
        }
        keyName := GetKeyName(remaining)
        if keyName = ""
            keyName := remaining
        if StrLen(keyName) = 1
            keyName := StrUpper(keyName)
        labels.Push(keyName)
        return this.Join(labels, "+")
    }

    static IsWinH(binding) {
        return StrLower(binding) = "#h" || StrLower(binding) = "#sc023"
    }

    static Join(items, separator) {
        result := ""
        for item in items
            result .= (result = "" ? "" : separator) item
        return result
    }
}
