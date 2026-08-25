# Creating a public export

Run the export tool from the private source workspace:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\export-public.ps1 -Destination "$env:TEMP\fresh-export"
```

The destination must not be inside this repository and must be empty. The tool copies
only its explicit allowlist, then runs the public-tree verifier. Review the resulting
file list and verifier output before creating an independent Git repository there.

If publication is authorized later, initialize Git only inside the verified export,
configure a public-safe author identity, and create one root commit. Never add a
public remote to the private source workspace and never copy its `.git` directory.
